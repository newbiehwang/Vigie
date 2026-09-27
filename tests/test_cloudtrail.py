"""AWS 공식 CloudTrail MCP 서버 (mcp/lambda_mcp/official.py)와 감사 로그 ↔ CloudTrail 연결

- 최근 90일 관리 이벤트 조회(lookup_events)만 붙이고, 유료인 CloudTrail Lake 도구 4개는 뺀다
- region을 생략하면 서버 기본값(us-east-1)이 아니라 이 배포의 리전을 쓴다
- 승인해 실행한 변경은 AWS API 요청 ID를 남긴다: CloudTrail 이벤트의 requestID와 같아 두 기록을 잇는다
"""
import json

import pytest
import yaml

from conftest import ROOT
from test_approvals import ORIGIN, audit_events, env, make_action  # noqa: F401 (env는 fixture)

LAKE_TOOLS = {"lake_query", "get_query_status", "get_query_results", "list_event_data_stores"}


@pytest.fixture
def ct_env(env, monkeypatch):  # noqa: F811
    # 테스트 기본 리전(us-east-1)은 서버 기본값과 같아 구분되지 않으므로 다른 리전으로 둔다
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    return env


def tools_of(env):  # noqa: F811
    return {tool["name"]: tool for tool in json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}


def test_only_event_lookup_is_attached(ct_env):
    tools = tools_of(ct_env)
    assert "lookup_events" in tools and not (LAKE_TOOLS & set(tools))
    assert tools["lookup_events"]["_meta"]["vigie/risk"] == "read"
    region = tools["lookup_events"]["inputSchema"]["properties"]["region"]
    assert region["default"] == "ap-southeast-2" and "us-east-1" not in region["description"]


def test_lookup_uses_this_deployments_region_when_omitted(ct_env, monkeypatch):
    from awslabs.cloudtrail_mcp_server.tools import CloudTrailTools
    regions = []

    class FakeCloudTrail:
        def lookup_events(self, **params):
            return {"Events": [{"EventId": "e1", "EventName": "PutRetentionPolicy", "EventTime": "2026-09-25T00:00:00Z",
                                "CloudTrailEvent": json.dumps({"requestID": "req-1"})}]}

    monkeypatch.setattr(CloudTrailTools, "_get_cloudtrail_client",
                        lambda self, region: regions.append(region) or FakeCloudTrail())
    tools_of(ct_env)  # 공식 서버를 불러온다

    result = ct_env["mcp"].call_admin("lookup_events", {"attribute_key": "EventName",
                                                        "attribute_value": "PutRetentionPolicy"})
    assert not result.get("isError"), result
    assert "PutRetentionPolicy" in json.dumps(result)
    ct_env["mcp"].call_admin("lookup_events", {"region": "us-west-2"})  # 직접 준 리전은 그대로
    assert regions == ["ap-southeast-2", "us-west-2"]


def test_executed_change_carries_its_cloudtrail_request_id(ct_env, monkeypatch):
    llm = ct_env["llm"]
    action = make_action(ct_env)
    response = llm.handle_action(action["actionId"], "approve", {"sub": "alice", "cognito:groups": "approvers"}, ORIGIN)
    view = json.loads(response["body"])
    assert view["status"] == "executed"
    trail = view["cloudtrail"]
    assert trail["event_source"] == "logs.amazonaws.com" and trail["event_name"] == "PutRetentionPolicy"
    assert trail["request_id"]  # AWS API 응답의 요청 ID (moto도 돌려준다)

    # 감사 로그의 실행 기록에 같은 요청 ID가 남는다
    items = ct_env["audit"].query(KeyConditionExpression="userId = :u",
                                  ExpressionAttributeValues={":u": "alice"})["Items"]
    executed = next(i for i in items if i.get("event") == "executed")
    assert executed["awsRequestId"] == trail["request_id"]
    assert executed["cloudTrailEvent"] == "logs.amazonaws.com:PutRetentionPolicy"
    assert audit_events("alice") == ["approved", "executed"]

    # 결과 설명 질문에도 들어가 모델이 CloudTrail에서 찾아볼 수 있다
    import approvals
    prompt = approvals.follow_up_prompt(ct_env["pending"].get_item(Key={"actionId": action["actionId"]})["Item"])
    assert trail["request_id"] in prompt and "PutRetentionPolicy" in prompt


def test_alarm_change_names_its_cloudtrail_event(ct_env):
    from test_approvals import ALARM
    action = make_action(ct_env, tool="setAlarmActions", args={"alarm_name": ALARM, "enabled": False})
    view = json.loads(ct_env["llm"].handle_action(action["actionId"], "approve", {"sub": "alice", "cognito:groups": "approvers"}, ORIGIN)["body"])
    assert view["cloudtrail"]["event_source"] == "monitoring.amazonaws.com"
    assert view["cloudtrail"]["event_name"] == "DisableAlarmActions"


def test_mcp_role_can_only_look_up_events():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=Loader)
    statements = template["Resources"]["McpLambdaExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"]
    actions = {a for s in statements for a in s["Action"] if a.startswith("cloudtrail:")}
    assert actions == {"cloudtrail:LookupEvents"}  # Lake 쿼리(StartQuery 등)는 주지 않는다
