"""EC2 조회 도구 (mcp/app.py): 인스턴스 목록, CPU 사용률 순위, 상태 검사, 비용 낭비 찾기

- 사용자 데이터·콘솔 출력·Windows 암호는 읽지 않는다 (코드에서 부르지 않고 IAM도 명시적으로 거부)
moto로 실제 EC2·CloudWatch API까지, LLM Lambda가 부르는 것과 같은 길(JSON-RPC → lambda_handler)로 확인한다.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
import yaml

from conftest import ROOT
from test_approvals import env  # noqa: F401 (fixture)


@pytest.fixture
def ec2_env(env):  # noqa: F811
    ec2 = boto3.client("ec2")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    ids = {}
    for name in ("web", "batch", "old-worker"):
        instance = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="t3.micro",
                                     UserData="export DB_PASSWORD=hunter2",  # 읽히면 안 되는 값
                                     TagSpecifications=[{"ResourceType": "instance",
                                                         "Tags": [{"Key": "Name", "Value": name}]}])
        ids[name] = instance["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[ids["old-worker"]])
    zone = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
    ids["volume"] = ec2.create_volume(AvailabilityZone=zone, Size=100, VolumeType="gp3")["VolumeId"]
    ids["eip"] = ec2.allocate_address(Domain="vpc")["AllocationId"]
    return env, ids


def call(env, name, args=None):  # noqa: F811
    body = json.loads(env["mcp"].call_tool(name, args or {})["content"][0]["text"])
    assert body["status"] == "success", body
    return body


def test_ec2_tools_are_registered_as_read(ec2_env):
    env, _ = ec2_env
    tools = {t["name"]: t for t in json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}
    for name in ("listEc2Instances", "getEc2CpuRanking", "getEc2StatusChecks", "findEc2Waste"):
        assert tools[name]["_meta"]["vigie/risk"] == "read" and tools[name]["inputSchema"]["required"] == []


def test_list_instances_by_state_without_user_data(ec2_env):
    env, ids = ec2_env
    body = call(env, "listEc2Instances")
    assert body["instance_count"] == 3 and body["by_state"] == {"running": 2, "stopped": 1}
    names = {i["name"]: i for i in body["instances"]}
    assert names["web"]["type"] == "t3.micro" and names["web"]["availability_zone"]
    assert [i["name"] for i in call(env, "listEc2Instances", {"state": "stopped"})["instances"]] == ["old-worker"]
    assert "hunter2" not in json.dumps(body)


def test_cpu_ranking_in_one_query(ec2_env):
    env, ids = ec2_env
    now = datetime.now(timezone.utc)
    cloudwatch = boto3.client("cloudwatch")
    for name, values in (("web", [20, 90, 40]), ("batch", [50, 60, 55])):
        cloudwatch.put_metric_data(Namespace="AWS/EC2", MetricData=[
            {"MetricName": "CPUUtilization", "Timestamp": now - timedelta(hours=hour), "Value": value,
             "Unit": "Percent", "Dimensions": [{"Name": "InstanceId", "Value": ids[name]}]}
            for hour, value in enumerate(values, start=1)])
    body = call(env, "getEc2CpuRanking", {"hours": 24})
    ranking = [(i["name"], i["max_cpu_percent"]) for i in body["instances"]]
    assert ranking == [("web", 90.0), ("batch", 60.0)]  # 최대 CPU 순 (멈춘 인스턴스는 빠진다)
    assert body["instances"][1]["average_cpu_percent"] == 55.0 and body["running_instances"] == 2


def test_status_checks(ec2_env):
    env, ids = ec2_env
    one = call(env, "getEc2StatusChecks", {"instance_id": ids["web"]})
    assert one["instances"][0]["instance_id"] == ids["web"] and one["instances"][0]["state"] == "running"
    everything = call(env, "getEc2StatusChecks")
    assert everything["checked"] == 3 and "note" in everything  # 전체 점검은 문제 있는 것만 보여 준다


def test_find_waste(ec2_env):
    env, ids = ec2_env
    body = call(env, "findEc2Waste", {"stopped_days": 0})
    assert [v["volume_id"] for v in body["unattached_volumes"]] == [ids["volume"]]
    assert body["unattached_volume_gib"] == 100
    assert [i["name"] for i in body["long_stopped_instances"]] == ["old-worker"]
    assert [a["allocation_id"] for a in body["unassociated_elastic_ips"]] == [ids["eip"]]


def test_stopped_date_is_read_from_the_state_transition_reason(ec2_env):
    app = ec2_env[0]["app"]
    reason = {"StateTransitionReason": "User initiated (2026-09-01 10:00:00 GMT)"}
    assert app._stopped_at(reason) == datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    assert app._stopped_at({"StateTransitionReason": ""}) is None


def test_code_never_reads_user_data_or_console_output():
    source = (Path(ROOT) / "mcp" / "app.py").read_text(encoding="utf-8")
    for call_name in ("describe_instance_attribute", "get_console_output", "get_console_screenshot",
                      "get_password_data", "get_launch_template_data", "describe_launch_template_versions"):
        assert call_name not in source, call_name


def test_iam_denies_user_data_console_and_passwords():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=Loader)
    statements = template["Resources"]["McpLambdaExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"]
    denied = {a for s in statements if s["Effect"] == "Deny" for a in s["Action"] if a.startswith("ec2:")}
    assert {"ec2:DescribeInstanceAttribute", "ec2:GetConsoleOutput", "ec2:GetConsoleScreenshot",
            "ec2:GetPasswordData", "ec2:GetLaunchTemplateData", "ec2:DescribeLaunchTemplateVersions"} <= denied
    allowed = {a for s in statements if s["Effect"] == "Allow" for a in s["Action"] if a.startswith("ec2:")}
    assert {"ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeVolumes",
            "ec2:DescribeAddresses"} <= allowed
    assert not (denied & allowed)
