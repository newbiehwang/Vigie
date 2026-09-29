"""서비스별 진단 절차 채점 (mcp/lambda_mcp/diagnose.py, mcp/app.py의 diagnoseService, services/llm/audit.py)

moto 위에 정상인 환경(ALB → EC2 두 대, Lambda 함수, 비공개 버킷, RDS, IAM 사용자와 키, 평소 비용)을 만들고,
장애를 하나씩 심은 뒤 절차가 원인 층을 맞히는지 본다. 원인 층이 기대와 정확히 같아야 하고(더 많이 짚어도 틀림),
기대한 증상 층도 있어야 한다. 정상 환경과 시나리오 43개(심은 장애 · 기대 원인 층 · 기대 증상 층)는 local/world.py에 있고,
로컬 실행(local/stack.py)이 대화창에서 재현할 때도 같은 코드를 쓴다.

CloudTrail 조회(LookupEvents)는 moto가 지원하지 않아 diagnose.recent_changes·key_activity·account_changes를 가짜로 바꿔
변경 기록과 키 사용 기록을 심는다. Cost Explorer의 날짜별 비용도 moto에 넣을 수 없어 _daily_costs를 가짜로 바꾸고,
응답을 읽는 부분은 test_daily_costs_reads_both_group_keys가 따로 본다.
가짜는 절차가 넘긴 관련 자원 이름·ID에 든 기록만 돌려주므로, 절차가 어떤 자원을 관련 있다고 보는지도 함께 확인한다.
"""
import copy
import json

import boto3
import pytest

from conftest import load_service_module
from local import world as w
from local.world import (SCENARIOS, block_target_sg, change, gpu_instances, leak_evasion, leak_persistence,
                         log_ingestion_spike, stop_targets)
from test_approvals import ORIGIN, FakeResponse, env  # noqa: F401 (env는 fixture)

REGION = boto3.session.Session().region_name


@pytest.fixture
def diagnose(aws):
    return load_service_module("mcp", "lambda_mcp.diagnose")


# ---------------------------------------------------------------- 정상인 환경 (local/world.py)
@pytest.fixture
def world(diagnose, monkeypatch):
    world = w.build_world(REGION)
    w.install_fakes(diagnose, world, monkeypatch.setattr)
    return world


def layer(result, layer_id):
    return next(item for item in result["layers"] if item["id"] == layer_id)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_procedure_finds_the_planted_cause(diagnose, world, scenario):
    if scenario.fault:
        scenario.fault(world)
    # EC2는 Name 태그로도, ID로도 확인한다 (ec2-healthy는 web-1, 나머지는 {id:web-1})
    result = diagnose.run(scenario.service, w.fill(scenario.resource, world), region=REGION,
                          target=w.fill(scenario.target, world))
    got, name = set(result["causes"]), scenario.name
    assert got == scenario.causes, f"{name}: 원인 층 {sorted(got)} (기대 {sorted(scenario.causes)})\n{result['summary']}"
    assert scenario.symptoms <= set(result["symptoms"]), (
        f"{name}: 증상 층 {result['symptoms']} (기대 {sorted(scenario.symptoms)})")
    # 요약은 원인이 있으면 원인부터, 없으면 이상 없음이나 증상
    assert result["summary"].startswith("원인 —") == bool(scenario.causes)
    # 모든 층이 판정과 근거를 가진다 (해당 없음 말고는 어느 API·지표를 봤는지)
    assert [item["id"] for item in result["layers"]] == ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
    for item in result["layers"]:
        assert item["status"] in diagnose.WEIGHT and item["component"] and item["checks"]
        for check in item["checks"]:
            assert check["status"] == "skip" or check["evidence"], (item["id"], check)


def test_scenarios_have_unique_names_and_filled_questions(world):
    """로컬 재현(local/scenario.py)이 이름으로 고르고, 질문의 자리 표시를 채워 보여 준다."""
    assert len(SCENARIOS) == len(w.BY_NAME) == 43
    for scenario in SCENARIOS:
        assert "{" not in w.fill(scenario.ask, world) and "{" not in w.fill(scenario.resource, world)


def test_scenarios_with_details(diagnose, world):
    """채점 말고 찾은 것의 내용도 본다: 사람이 읽을 근거가 맞는 자원을 가리키나."""
    stop_targets(world)
    result = diagnose.run("alb", "web-alb", region=REGION)
    assert "2대 모두" in layer(result, "L5")["finding"] and "web-1" in layer(result, "L5")["finding"]
    assert "Target.InvalidState" in layer(result, "L4")["finding"]
    assert "StopInstances" in layer(result, "L2")["finding"] and "alice" in layer(result, "L2")["finding"]
    assert result["summary"].index("L5") < result["summary"].index("계기 L2")  # 무엇이 고장 났나 → 무엇이 계기였나
    assert layer(result, "L1")["status"] == "ok"  # 시스템 상태 검사 정상 (Health API는 확인 불가 항목으로 남는다)
    assert any(c["status"] == "unknown" and "Health" in c["finding"] for c in layer(result, "L1")["checks"])


def test_blocked_security_group_names_the_target_and_port(diagnose, world):
    block_target_sg(world)
    finding = layer(diagnose.run("alb", "web-alb", region=REGION), "L3")["finding"]
    assert "80" in finding and "web-1" in finding


def test_changes_without_trouble_are_not_blamed(diagnose, world):
    """장애가 없으면 변경이 있어도 L2는 정상이다 (계기로 보지 않는다)."""
    change(world, "ModifyInstanceAttribute", world["instances"]["web-1"])
    result = diagnose.run("ec2", world["instances"]["web-1"], region=REGION)
    assert result["causes"] == [] and layer(result, "L2")["status"] == "ok" and "1건" in layer(result, "L2")["finding"]


def test_cloudtrail_unavailable_is_unknown_not_ok(diagnose, world, monkeypatch):
    def fail(*_):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(diagnose, "recent_changes", fail)
    assert layer(diagnose.run("s3", world["bucket"], region=REGION), "L2")["status"] == "unknown"


def test_ec2_grace_period_zero_is_a_warning(diagnose, world, monkeypatch):
    ec2, autoscaling = boto3.client("ec2"), boto3.client("autoscaling")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    autoscaling.create_launch_configuration(LaunchConfigurationName="web-lc", ImageId=image, InstanceType="t3.micro")
    autoscaling.create_auto_scaling_group(AutoScalingGroupName="web-asg", LaunchConfigurationName="web-lc",
                                          MinSize=1, MaxSize=2, VPCZoneIdentifier=world["subnets"][0],
                                          HealthCheckType="ELB", HealthCheckGracePeriod=0)
    instance_id = autoscaling.describe_auto_scaling_groups()["AutoScalingGroups"][0]["Instances"][0]["InstanceId"]
    # moto는 명시한 0초를 무시하고 300초로 저장한다. 실제 API처럼 0초를 돌려주도록 응답만 고친다
    clients = diagnose.Clients(REGION)
    groups = clients("autoscaling")
    original = groups.describe_auto_scaling_groups

    def zero_grace(**kwargs):
        response = original(**kwargs)
        for group in response["AutoScalingGroups"]:
            group["HealthCheckGracePeriod"] = 0
        return response

    monkeypatch.setattr(groups, "describe_auto_scaling_groups", zero_grace)
    result = diagnose.run("ec2", instance_id, region=REGION, clients=clients)
    assert any(c["status"] == "warn" and "유예" in c["finding"] for c in layer(result, "L5")["checks"])
    assert layer(result, "L6")["status"] == "ok"  # 그룹에 실패한 활동이 없다


@pytest.mark.parametrize("service,resource,message", [
    ("mq", "broker-1", "진단할 수 있는 서비스"),
    ("alb", "no-such-alb", "로드 밸런서가 없습니다"),
    ("ec2", "i-0123456789abcdef0", "인스턴스가 없습니다"),
    ("lambda", "no-such-function", "함수가 없습니다"),
    ("ec2", "", "resource"),
])
def test_bad_input_is_a_clear_error(diagnose, world, service, resource, message):
    with pytest.raises(ValueError, match=message):
        diagnose.run(service, resource, region=REGION)


def test_logging_stopped_alone_reads_as_the_cause(diagnose, world):
    leak_evasion(world)
    assert diagnose.run("credential", world["access_key"], region=REGION)["summary"] == (
        "원인 — L2 리소스 변경 기록: 감사 기록·탐지를 끄려 했습니다: StopLogging 1건")


def test_credential_findings_name_what_to_do(diagnose, world):
    leak_persistence(world)
    result = diagnose.run("credential", world["access_key"], region=REGION)
    assert result["hours"] == 24  # 자격 증명은 하루를 본다
    l6 = layer(result, "L6")
    assert "CreateUser" in l6["finding"] and "CreateAccessKey" in l6["finding"]
    assert any("비활성화" in c["finding"] for c in l6["checks"])  # 지우지 말고 비활성화
    assert any(c["status"] == "unknown" and "GetObject" in c["finding"] for c in layer(result, "L7")["checks"])


def test_vpc_needs_a_target_and_explains_same_subnet(diagnose, world):
    with pytest.raises(ValueError, match="target"):
        diagnose.run("vpc", "web-1", region=REGION)
    with pytest.raises(ValueError, match="DNS"):
        diagnose.run("vpc", "web-1", region=REGION, target="db.internal:5432")
    result = diagnose.run("vpc", "web-1", region=REGION, target=w.fill("{web-2}:22", world))
    assert result["target"].endswith(":22") and layer(result, "L6")["status"] == "skip"


def test_cost_ignores_hours_and_filters_by_service(diagnose, world):
    gpu_instances(world)
    log_ingestion_spike(world)
    result = diagnose.run("cost", "Compute", hours=1, region=REGION)
    assert result["hours"] == 72 and set(result["causes"]) == {"L2", "L5"}  # CloudWatch 급증은 거른 서비스 밖
    assert "p3.2xlarge" in layer(result, "L5")["finding"]
    with pytest.raises(ValueError, match="account"):
        diagnose.run("cost", "Amazon Bedrock", region=REGION)


@pytest.mark.parametrize("service,usage,expected", [
    ("EC2 - Other", "APN2-NatGateway-Bytes", "L3"),
    ("Amazon Elastic Compute Cloud - Compute", "APN2-DataTransfer-Out-Bytes", "L3"),
    ("Amazon Elastic Load Balancing", "APN2-LCUUsage", "L4"),
    ("Amazon Elastic Compute Cloud - Compute", "APN2-BoxUsage:c5.large", "L5"),
    ("AWS Lambda", "APN2-Lambda-GB-Second", "L5"),
    ("EC2 - Other", "APN2-EBS:VolumeUsage.gp3", "L7"),
    ("AmazonCloudWatch", "APN2-DataProcessing-Bytes", "L7"),
    ("Amazon Simple Storage Service", "APN2-Requests-Tier1", "L7"),
])
def test_cost_layer_of_usage_types(diagnose, service, usage, expected):
    assert diagnose.cost_layer(service, usage) == expected


def test_daily_costs_reads_both_group_keys(diagnose):
    class FakeCostExplorer:
        def __init__(self):
            self.calls = []

        def get_cost_and_usage(self, **kwargs):
            self.calls.append(kwargs)
            if "NextPageToken" not in kwargs:
                return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-20"}, "Groups": [
                    {"Keys": ["AWS Lambda", "APN2-Request"], "Metrics": {"UnblendedCost": {"Amount": "1.25"}}}]}],
                        "NextPageToken": "p2"}
            return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-21"}, "Groups": []}]}

    ce = FakeCostExplorer()
    assert diagnose._daily_costs(ce, "2026-09-10", "2026-09-28") == [("2026-09-20", "AWS Lambda", "APN2-Request", 1.25)]
    assert len(ce.calls) == 2 and [g["Key"] for g in ce.calls[0]["GroupBy"]] == ["SERVICE", "USAGE_TYPE"]


def test_nacl_rules_are_evaluated_in_order(diagnose):
    acl = {"Entries": [
        {"RuleNumber": 100, "Protocol": "-1", "RuleAction": "allow", "Egress": False, "CidrBlock": "0.0.0.0/0"},
        {"RuleNumber": 90, "Protocol": "6", "RuleAction": "deny", "Egress": False, "CidrBlock": "10.0.0.0/8",
         "PortRange": {"From": 443, "To": 443}},
        {"RuleNumber": 32767, "Protocol": "-1", "RuleAction": "deny", "Egress": False, "CidrBlock": "0.0.0.0/0"}]}
    assert diagnose.nacl_decision(acl, False, 443, "10.1.2.3") == (False, 90)
    assert diagnose.nacl_decision(acl, False, 443, "203.0.113.10") == (True, 100)
    assert diagnose.nacl_decision(acl, True, 443, "10.1.2.3") == (False, None)  # 나가는 규칙이 없으면 거부


# ---------------------------------------------------------------- MCP 도구와 감사 로그
def admin_run(env, monkeypatch, replies):  # noqa: F811
    """관리자의 질문 하나: 가짜 모델이 replies대로 도구를 부르고, 도구는 실제 MCP 서버(moto)가 실행한다."""
    llm = env["llm"]
    import mcp_anthropic_client
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse({"usage": {}, **replies[len(sent) - 1]})

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    monkeypatch.setattr(client.mcp_client, "call_tool", env["mcp"].call_tool)
    monkeypatch.setattr(llm, "get_client", lambda *_: client)
    return json.loads(llm.handle_llm1_with_mcp({"text": "orders-api가 왜 실패해?"}, ORIGIN, caller_id="carol",
                                               caller_groups=["admins"])["body"])


def test_diagnose_tool_is_read_only_and_for_admins(env):  # noqa: F811
    tools = {t["name"]: t for t in json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}
    meta = tools["diagnoseService"]["_meta"]
    assert meta["vigie/risk"] == "read" and meta["vigie/access"] == "admin"
    assert set(tools["diagnoseService"]["inputSchema"]["required"]) == {"service", "resource"}
    refused = env["mcp"].call_tool("diagnoseService", {"service": "s3", "resource": "x"})  # 관리자 표시 없음
    assert refused.get("isError") is True


def test_diagnosis_is_kept_in_the_audit_row(env, monkeypatch):  # noqa: F811
    boto3.client("s3").create_bucket(Bucket="open-bucket", ACL="public-read")
    call = {"type": "tool_use", "id": "toolu_diag", "name": "diagnoseService",
            "input": {"service": "s3", "resource": "open-bucket"}}
    admin_run(env, monkeypatch, [{"content": [call], "stop_reason": "tool_use"},
                                 {"content": [{"type": "text", "text": "ACL이 공개입니다."}], "stop_reason": "end_turn"}])
    items = env["audit"].query(KeyConditionExpression="userId = :u", ExpressionAttributeValues={":u": "carol"})["Items"]
    row = next(i for i in items if i.get("tool") == "diagnoseService")
    diagnosis = row["diagnosis"]
    assert diagnosis["service"] == "s3" and diagnosis["resource"] == "open-bucket" and diagnosis["causes"] == ["L6"]
    assert [item["id"] for item in diagnosis["layers"]] == ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
    l6 = next(item for item in diagnosis["layers"] if item["id"] == "L6")
    assert l6["status"] == "cause" and "AllUsers" in l6["finding"] and l6["component"]
    assert all({"name", "status", "finding", "evidence"} <= set(c) for c in l6["checks"])
    # 다른 도구 행에는 없다
    assert all("diagnosis" not in i for i in items if i.get("tool") not in (None, "diagnoseService"))


def test_diagnosis_of_ignores_other_tools_and_errors():
    audit = load_service_module("services/llm", "audit")
    text = json.dumps({"status": "error", "message": "버킷이 없습니다"})
    assert audit.diagnosis_of("diagnoseService", {"content": [{"type": "text", "text": text}]}) is None
    assert audit.diagnosis_of("listS3Buckets", {"content": [{"type": "text", "text": "{}"}]}) is None
    assert audit.diagnosis_of("diagnoseService", "not json") is None
    # VPC 연결은 목적지도 남긴다
    body = {"status": "success", "service": "vpc", "service_name": "VPC 연결", "resource": "web-1",
            "target": "10.0.2.15:5432", "hours": 1, "summary": "s", "causes": ["L3"], "layers": []}
    kept = audit.diagnosis_of("diagnoseService", {"content": [{"type": "text", "text": json.dumps(body)}]})
    assert kept["target"] == "10.0.2.15:5432" and kept["serviceName"] == "VPC 연결"
