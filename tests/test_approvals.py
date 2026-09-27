"""변경 작업 승인 (services/llm/approvals.py, mcp/lambda_mcp/risk.py · approval.py, mcp/app.py의 변경 도구)

LLM Lambda 코드가 실제 MCP 서버 코드(mcp/app.py의 lambda_handler)를 부르도록 이어서, 처음부터 끝까지 확인한다
(AWS는 moto, Anthropic API는 가짜 응답).

- 위험도: tools/list의 모든 도구에 위험도가 있고, 목록에 없는 도구는 변경 도구로 본다
- 승인 없이 실행되지 않는다: 모델이 변경 도구를 부르면 미리 보기만 하고 승인 요청을 만든다
- MCP 재확인: 작업 ID가 없거나, 승인 전이거나, 인자가 다르거나, 만료됐거나, 이미 실행했으면 거절한다
- 승인 규칙: 어느 환경이든 결정자(approvers)와 관리자(admins)만. dev는 그룹이면 본인 요청도, prod는 다른 사람만. 거절은 본인도 된다
- 감사 로그를 남기지 못하면 승인·실행하지 않는다. Slack 경로는 변경 작업을 요청할 수 없다
"""
import copy
import json
import time

import boto3
import pytest
import yaml

from conftest import ROOT, load_service_module

PENDING_TABLE = "wga-pending-actions-test"
AUDIT_TABLE = "wga-audit-test"
SESSION_TABLE = "wga-mcp-sessions-test"
LOG_GROUP = "/aws/lambda/wga-llm-test"  # conftest의 ENV=test
ALARM = "wga-test-api-5xx"
ORIGIN = "https://test.abc.amplifyapp.com"
REQUEST_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def create_tables():
    dynamodb = boto3.client("dynamodb")
    dynamodb.create_table(TableName=PENDING_TABLE, BillingMode="PAY_PER_REQUEST",
                          AttributeDefinitions=[{"AttributeName": "actionId", "AttributeType": "S"}],
                          KeySchema=[{"AttributeName": "actionId", "KeyType": "HASH"}])
    dynamodb.create_table(TableName=SESSION_TABLE, BillingMode="PAY_PER_REQUEST",
                          AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
                          KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}])
    dynamodb.create_table(
        TableName=AUDIT_TABLE, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("userId", "at", "day")],
        KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}, {"AttributeName": "at", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": "by-day", "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": "day", "KeyType": "HASH"},
                                               {"AttributeName": "at", "KeyType": "RANGE"}]}])


class McpBridge:
    """LLM Lambda의 MCPClient 대신 MCP 서버의 lambda_handler를 바로 부른다 (Function URL로 오는 것과 같은 요청)."""

    def __init__(self, app):
        self.app = app
        self.calls = []
        response = self._rpc("initialize")
        self.session_id = response["headers"]["MCP-Session-Id"]

    def _rpc(self, method, params=None):
        headers = {"content-type": "application/json"}
        if getattr(self, "session_id", None):
            headers["mcp-session-id"] = self.session_id
        event = {"httpMethod": "POST", "headers": headers,
                 "body": json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}})}
        return self.app.lambda_handler(event, None)

    def call_tool(self, name, args=None, meta=None):
        self.calls.append((name, copy.deepcopy(args), meta))
        params = {"name": name, "arguments": args or {}}
        if meta:
            params["_meta"] = meta
        body = json.loads(self._rpc("tools/call", params)["body"])
        if "error" in body and body["error"]:
            raise Exception(body["error"]["message"])
        return body["result"]


@pytest.fixture
def env(aws, monkeypatch):
    """MCP 서버와 LLM 서비스를 같은 moto 계정 위에 띄운다."""
    for name, value in {"PENDING_ACTIONS_TABLE": PENDING_TABLE, "AUDIT_TABLE": AUDIT_TABLE,
                        "MCP_SESSION_TABLE": SESSION_TABLE}.items():
        monkeypatch.setenv(name, value)
    create_tables()
    logs = boto3.client("logs")
    logs.create_log_group(logGroupName=LOG_GROUP)
    logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
    boto3.client("cloudwatch").put_metric_alarm(
        AlarmName=ALARM, MetricName="5XXError", Namespace="AWS/ApiGateway", Statistic="Sum", Period=60,
        EvaluationPeriods=1, Threshold=1, ComparisonOperator="GreaterThanOrEqualToThreshold", ActionsEnabled=True)

    app = load_service_module("mcp", "app")  # 변경 도구·승인 재확인이 있는 MCP 서버
    import lambda_mcp.approval as mcp_approval
    import lambda_mcp.risk as risk
    llm = load_service_module("services/llm", "llm_service")  # 먼저 불러온 MCP 모듈 객체는 그대로 쓸 수 있다
    bridge = McpBridge(app)
    monkeypatch.setattr(llm, "call_mcp_tool", bridge.call_tool)
    import approvals
    return {"app": app, "llm": llm, "mcp": bridge, "approvals": approvals, "risk": risk, "mcp_approval": mcp_approval,
            "pending": boto3.resource("dynamodb").Table(PENDING_TABLE),
            "audit": boto3.resource("dynamodb").Table(AUDIT_TABLE)}


def retention():
    return boto3.client("logs").describe_log_groups(logGroupNamePrefix=LOG_GROUP)["logGroups"][0].get(
        "retentionInDays")


def alarm_actions_enabled():
    return boto3.client("cloudwatch").describe_alarms(AlarmNames=[ALARM])["MetricAlarms"][0]["ActionsEnabled"]


def audit_events(user_id):
    items = boto3.resource("dynamodb").Table(AUDIT_TABLE).query(
        KeyConditionExpression="userId = :u", ExpressionAttributeValues={":u": user_id})["Items"]
    return sorted((i["event"] for i in items if i["kind"] == "action"))


def make_action(env, *, tool="setLogRetention", args=None, status="pending", requester="alice", expires_in=600):
    """승인 요청 하나를 테이블에 직접 넣는다 (MCP 재확인·승인 API를 따로 확인할 때)."""
    approvals = env["approvals"]
    args = args if args is not None else {"log_group_name": LOG_GROUP, "retention_days": 14}
    item = approvals.ApprovalStore.new_item(
        requester_id=requester, requester_email=f"{requester}@example.com", request_id=REQUEST_ID,
        session_id="s1", tool=tool, args=args, preview={"summary": "보존 기간 30일 → 14일"})
    item["status"] = status
    item["expiresAt"] = int(time.time()) + expires_in
    env["pending"].put_item(Item=item)
    return item


# ---------------------------------------------------------------- 위험도 (MCP tools/list)

def test_every_listed_tool_has_a_known_risk(env):
    TOOL_RISK = env["risk"].TOOL_RISK
    tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    names = {tool["name"] for tool in tools}
    # 공식 서버를 올려 새 도구가 생기면 여기서 드러난다 (분류하기 전까지는 변경 도구로 막힌다)
    assert names <= set(TOOL_RISK), f"위험도가 없는 도구: {sorted(names - set(TOOL_RISK))}"
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["describe_log_groups"]["annotations"]["readOnlyHint"] is True
    assert by_name["describe_log_groups"]["_meta"]["wga/risk"] == "read"
    assert by_name["generateLineChart"]["_meta"]["wga/risk"] == "artifact"
    for name in ("setLogRetention", "setAlarmActions"):
        assert by_name[name]["_meta"]["wga/risk"] == "write"
        assert by_name[name]["annotations"] == {"readOnlyHint": False, "destructiveHint": True,
                                                "openWorldHint": True}


def test_unclassified_tools_are_treated_as_write(env, monkeypatch):
    assert env["risk"].needs_approval("brand_new_official_tool")
    assert env["approvals"].risk_of(None) == "write"
    assert env["approvals"].risk_of({"name": "x"}) == "write"  # 위험도 표시가 없는 도구
    # 공식 서버를 올렸더니 위험도 목록에 없는 도구가 생긴 상황: MCP 서버는 변경 도구로 보고 실행하지 않는다
    monkeypatch.delitem(env["risk"].TOOL_RISK, "get_active_alarms")
    result = env["mcp"].call_tool("get_active_alarms", {})
    assert result["isError"] is True and "실행하지 않았습니다" in result["content"][0]["text"]
    # 아예 없는 도구는 예전처럼 '없음'
    with pytest.raises(Exception, match="not found"):
        env["mcp"].call_tool("brand_new_official_tool", {})


def test_args_hash_is_the_same_on_both_sides(env):
    mcp_hash = env["mcp_approval"].args_hash
    args = {"retention_days": 14, "log_group_name": LOG_GROUP}
    assert mcp_hash("setLogRetention", args) == env["approvals"].args_hash(
        "setLogRetention", {"log_group_name": LOG_GROUP, "retention_days": 14})


# ---------------------------------------------------------------- MCP 재확인

def test_write_tool_does_not_run_without_an_approved_action(env):
    result = env["mcp"].call_tool("setLogRetention", {"log_group_name": LOG_GROUP, "retention_days": 1})
    assert result["isError"] is True and "승인된 작업 ID가 없습니다" in result["content"][0]["text"]
    assert retention() == 30


@pytest.mark.parametrize("case, expected", [
    ("pending", "승인된 상태가 아닙니다"),
    ("other_args", "인자가 다릅니다"),
    ("expired", "시간이 지났습니다"),
    ("missing", "찾을 수 없습니다"),
])
def test_mcp_refuses_actions_that_are_not_approved_as_is(env, case, expected):
    action = make_action(env, status="pending" if case == "pending" else "approved",
                         expires_in=-1 if case == "expired" else 600)
    args = {"log_group_name": LOG_GROUP, "retention_days": 1 if case == "other_args" else 14}
    action_id = "5b9f2c1e-0000-4000-8000-000000000000" if case == "missing" else action["actionId"]
    result = env["mcp"].call_tool("setLogRetention", args, meta={"wga/actionId": action_id})
    assert result["isError"] is True and expected in result["content"][0]["text"]
    assert retention() == 30


def test_approved_action_runs_exactly_once(env):
    action = make_action(env, status="approved")
    meta = {"wga/actionId": action["actionId"]}
    args = {"log_group_name": LOG_GROUP, "retention_days": 14}

    first = env["mcp"].call_tool("setLogRetention", args, meta=meta)
    assert not first.get("isError") and retention() == 14
    stored = env["pending"].get_item(Key={"actionId": action["actionId"]})["Item"]
    assert stored["status"] == "executed" and "14일" in stored["result"]

    # 같은 작업 ID로 다시 부르면 거절 (다시 실행하지 않는다)
    boto3.client("logs").put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
    again = env["mcp"].call_tool("setLogRetention", args, meta=meta)
    assert again["isError"] is True and retention() == 30


def test_tool_rejects_resources_outside_this_environment(env):
    # 누군가 범위 밖 로그 그룹을 승인 테이블에 넣어도, 도구가 이름을 확인해 실패로 끝난다 (IAM도 같은 범위)
    other = "/aws/lambda/payments-prod"
    boto3.client("logs").create_log_group(logGroupName=other)
    action = make_action(env, status="approved", args={"log_group_name": other, "retention_days": 1})
    result = env["mcp"].call_tool("setLogRetention", {"log_group_name": other, "retention_days": 1},
                                  meta={"wga/actionId": action["actionId"]})
    assert result["isError"] is True
    assert env["pending"].get_item(Key={"actionId": action["actionId"]})["Item"]["status"] == "failed"
    assert "retentionInDays" not in boto3.client("logs").describe_log_groups(
        logGroupNamePrefix=other)["logGroups"][0]


def test_preview_shows_the_change_without_running_it(env):
    preview = env["mcp"].call_tool("setLogRetention", {"log_group_name": LOG_GROUP, "retention_days": 7},
                                   meta={"wga/preview": True})
    data = json.loads(preview["content"][0]["text"])
    assert data["before"] == "30일" and data["after"] == "7일" and "지워질 수 있습니다" in data["summary"]
    # 카드에 따로 보일 대상과 영향 (요약에는 영향을 괄호로 붙인다)
    assert data["target"] == LOG_GROUP and data["warning"] == "지난 로그 일부가 지워질 수 있습니다"
    assert data["summary"].endswith("(지난 로그 일부가 지워질 수 있습니다)")
    assert retention() == 30
    longer = env["mcp"].call_tool("setLogRetention", {"log_group_name": LOG_GROUP, "retention_days": 60},
                                  meta={"wga/preview": True})
    assert "warning" not in json.loads(longer["content"][0]["text"])  # 늘리면 지워지는 로그가 없다

    alarm = env["mcp"].call_tool("setAlarmActions", {"alarm_name": ALARM, "enabled": False},
                                 meta={"wga/preview": True})
    assert json.loads(alarm["content"][0]["text"])["after"] == "알림 꺼짐" and alarm_actions_enabled()

    bad = env["mcp"].call_tool("setAlarmActions", {"alarm_name": "billing-prod-alarm", "enabled": False},
                               meta={"wga/preview": True})
    assert bad["isError"] is True and "wga-test-" in bad["content"][0]["text"]


def test_alarm_actions_tool(env):
    action = make_action(env, status="approved", tool="setAlarmActions",
                         args={"alarm_name": ALARM, "enabled": False})
    result = env["mcp"].call_tool("setAlarmActions", {"alarm_name": ALARM, "enabled": False},
                                  meta={"wga/actionId": action["actionId"]})
    assert not result.get("isError") and alarm_actions_enabled() is False


# ---------------------------------------------------------------- 도구 반복: 변경 도구는 승인 요청이 된다

class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def run_loop(env, monkeypatch, tool_name="setLogRetention", tool_input=None, with_approvals=True, audit_ok=True):
    import mcp_anthropic_client
    from audit import AuditLog, CloudWatchSink
    from llm_progress import ProgressReporter
    from redaction import Redactor

    tool_input = tool_input or {"log_group_name": LOG_GROUP, "retention_days": 14}
    replies = [
        {"content": [{"type": "text", "text": "보존 기간을 바꾸겠습니다."},
                     {"type": "tool_use", "id": "toolu_1", "name": tool_name, "input": tool_input}], "usage": {}},
        {"content": [{"type": "text", "text": "승인이 필요합니다."}], "usage": {}},
    ]
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse(replies[len(sent) - 1])

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    monkeypatch.setattr(client.mcp_client, "call_tool", env["mcp"].call_tool)
    client.progress = ProgressReporter()
    client.redactor = Redactor()
    audit_table = env["audit"] if audit_ok else boto3.resource("dynamodb").Table("missing-table")
    client.audit = AuditLog(audit_table, CloudWatchSink(None), client.redactor, user_id="alice", email=None,
                            source="web", request_id=REQUEST_ID, session_id="s1", model_id="m", question="q")
    approvals = env["approvals"]
    client.approvals = approvals.ApprovalRequester(
        approvals.ApprovalStore(env["pending"]), requester_id="alice", requester_email="alice@example.com",
        request_id=REQUEST_ID, session_id="s1", audit=client.audit) if with_approvals else None
    answer = client.process_user_input("로그 보존 기간 14일로 줄여줘", "system")
    tool_result = sent[1]["messages"][-1]["content"][0]
    return answer, tool_result, client


def test_model_calling_a_write_tool_creates_an_approval_request(env, monkeypatch):
    answer, tool_result, client = run_loop(env, monkeypatch)

    assert answer == "승인이 필요합니다." and retention() == 30  # 바뀌지 않았다
    # MCP에는 미리 보기만 요청했다
    assert [(name, meta) for name, _, meta in env["mcp"].calls if name == "setLogRetention"] == [
        ("setLogRetention", {"wga/preview": True})]
    # 도구 결과는 MCP 결과(content 목록)를 JSON으로 바꿔 데이터 영역(<tool_result_data>)에 넣어 보낸다
    wrapped = tool_result["content"]
    assert wrapped.startswith('<tool_result_data tool="setLogRetention">')
    inner = wrapped.split(">\n", 1)[1].rsplit("\n</tool_result_data>", 1)[0]
    content = json.loads(json.loads(inner)["content"][0]["text"])
    assert content["status"] == "approval_required"
    stored = env["pending"].get_item(Key={"actionId": content["actionId"]})["Item"]
    assert stored["status"] == "pending" and stored["requesterId"] == "alice"
    assert json.loads(stored["args"]) == {"log_group_name": LOG_GROUP, "retention_days": 14}
    assert "30일 → 14일" in stored["summary"] and stored["expiresAt"] - stored["createdAt"] == 600
    assert stored["target"] == LOG_GROUP and stored["warning"] == "지난 로그 일부가 지워질 수 있습니다"
    view = env["approvals"].public_view(stored)
    assert view["target"] == LOG_GROUP and view["warning"] == stored["warning"]
    assert client.approvals.created[0]["actionId"] == content["actionId"]
    assert audit_events("alice") == ["requested"]


def test_out_of_scope_request_is_refused_before_asking_for_approval(env, monkeypatch):
    _, tool_result, client = run_loop(env, monkeypatch, tool_input={"log_group_name": "/aws/lambda/other",
                                                                     "retention_days": 1})
    assert "wga-" in tool_result["content"]
    assert client.approvals.created == [] and env["pending"].scan()["Items"] == []


def test_slack_path_cannot_request_changes(env, monkeypatch):
    _, tool_result, _ = run_loop(env, monkeypatch, with_approvals=False)
    assert "Slack" in tool_result["content"] and env["pending"].scan()["Items"] == []
    assert not any(meta for _, _, meta in env["mcp"].calls)  # 미리 보기도 하지 않는다


def test_no_approval_request_without_audit_record(env, monkeypatch):
    _, tool_result, client = run_loop(env, monkeypatch, audit_ok=False)
    assert client.approvals.created == [] and env["pending"].scan()["Items"] == []
    assert "approval_required" not in tool_result["content"]


# ---------------------------------------------------------------- 승인 API

def decide(env, action_id, operation, sub, groups=None, email=None):
    claims = {"sub": sub}
    if groups:
        claims["cognito:groups"] = groups
    if email:
        claims["email"] = email
    response = env["llm"].handle_action(action_id, operation, claims, ORIGIN)
    return response["statusCode"], json.loads(response["body"])


def test_approver_can_approve_own_request_in_dev_and_it_runs(env):
    action = make_action(env)
    status, body = decide(env, action["actionId"], "approve", "alice", groups="approvers")
    assert status == 200 and body["status"] == "executed" and body["decidedBy"] == "alice"
    assert retention() == 14
    assert audit_events("alice") == ["approved", "executed"]
    # MCP에는 작업 ID를 붙여 불렀다
    assert env["mcp"].calls[-1][2] == {"wga/actionId": action["actionId"]}


def test_admins_can_decide_too(env):
    # 권한은 세 단계이고 관리자는 결정자의 일도 한다 (admins만 넣은 예전 계정도)
    action = make_action(env)
    status, body = decide(env, action["actionId"], "approve", "alice", groups="admins")
    assert status == 200 and body["status"] == "executed"


def test_prod_requires_another_approver(env, monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    action = make_action(env)
    # 본인은 approvers 그룹이어도 승인할 수 없다
    assert decide(env, action["actionId"], "approve", "alice", groups="approvers")[0] == 403
    # 승인자가 아닌 다른 사람은 요청이 있는지도 모른다
    assert decide(env, action["actionId"], "approve", "mallory")[0] == 404
    assert retention() == 30
    status, body = decide(env, action["actionId"], "approve", "bob", groups="approvers")
    assert status == 200 and body["status"] == "executed" and retention() == 14


def test_dev_requester_without_the_group_cannot_approve(env):
    # 예전에는 dev에서 로그인만 하면 본인 요청을 승인할 수 있었다 (docs/threat-model.md R2)
    action = make_action(env)
    status, body = decide(env, action["actionId"], "approve", "alice")
    assert status == 403 and "결정자" in body["error"]
    assert retention() == 30 and audit_events("alice") == []
    # 거절은 본인이 할 수 있다
    assert decide(env, action["actionId"], "deny", "alice")[0] == 200


def test_dev_non_approver_cannot_approve_someone_elses_request(env):
    action = make_action(env)
    assert decide(env, action["actionId"], "approve", "mallory")[0] == 404
    # 결정자·관리자가 아닌 그룹은 남의 요청을 볼 수도 없다 (관리자는 결정자의 일도 한다: test_admins_can_decide_too)
    assert decide(env, action["actionId"], "approve", "carol", groups="readers")[0] == 404


def test_deny_does_not_run_and_cannot_be_approved_later(env):
    action = make_action(env)
    status, body = decide(env, action["actionId"], "deny", "alice")
    assert status == 200 and body["status"] == "denied"
    assert decide(env, action["actionId"], "approve", "alice", groups="approvers")[0] == 409
    assert retention() == 30 and audit_events("alice") == ["denied"]
    assert not any(meta for _, _, meta in env["mcp"].calls)


def test_expired_and_repeated_approvals_are_rejected(env):
    expired = make_action(env, expires_in=-1)
    status, body = decide(env, expired["actionId"], "approve", "alice", groups="approvers")
    assert status == 409 and "10분" in body["error"]

    action = make_action(env)
    assert decide(env, action["actionId"], "approve", "alice", groups="approvers")[0] == 200
    assert decide(env, action["actionId"], "approve", "alice", groups="approvers")[0] == 409
    assert retention() == 14


def test_tampered_request_is_not_run(env):
    action = make_action(env)
    env["pending"].update_item(Key={"actionId": action["actionId"]}, UpdateExpression="SET args = :a",
                               ExpressionAttributeValues={":a": json.dumps({"log_group_name": LOG_GROUP,
                                                                             "retention_days": 1})})
    status, body = decide(env, action["actionId"], "approve", "alice", groups="approvers")
    assert status == 409 and retention() == 30


def test_nothing_runs_when_the_decision_cannot_be_audited(env, monkeypatch):
    monkeypatch.setattr(env["llm"], "audit_table", boto3.resource("dynamodb").Table("missing-table"))
    action = make_action(env)
    status, _ = decide(env, action["actionId"], "approve", "alice", groups="approvers")
    assert status == 503 and retention() == 30
    assert env["pending"].get_item(Key={"actionId": action["actionId"]})["Item"]["status"] == "pending"


def test_action_marked_failed_when_mcp_does_not_run_it(env, monkeypatch):
    monkeypatch.setattr(env["llm"], "call_mcp_tool", lambda tool, args, meta=None: (_ for _ in ()).throw(
        RuntimeError("연결 실패")))
    action = make_action(env)
    status, body = decide(env, action["actionId"], "approve", "alice", groups="approvers")
    assert status == 200 and body["status"] == "failed" and retention() == 30
    assert audit_events("alice") == ["approved", "failed"]


def test_get_action_is_visible_only_to_requester_and_approvers(env):
    action = make_action(env)
    assert decide(env, action["actionId"], "get", "alice")[1]["status"] == "pending"
    assert decide(env, action["actionId"], "get", "bob", groups="approvers")[0] == 200
    assert decide(env, action["actionId"], "get", "mallory")[0] == 404
    assert decide(env, "not-a-uuid", "get", "alice")[0] == 404


def test_action_routes(env):
    lambda_function = load_service_module("services/llm", "lambda_function")
    import llm_service
    llm_service.call_mcp_tool = env["mcp"].call_tool
    action = make_action(env)

    def event(path, method):
        return {"path": path, "httpMethod": method, "headers": {"origin": ORIGIN},
                "requestContext": {"authorizer": {"claims": {"sub": "alice", "cognito:groups": "approvers"}}}}

    assert lambda_function.lambda_handler(event(f"/actions/{action['actionId']}", "GET"), None)["statusCode"] == 200
    assert lambda_function.lambda_handler(event(f"/actions/{action['actionId']}/cancel", "POST"),
                                          None)["statusCode"] == 404
    response = lambda_function.lambda_handler(event(f"/actions/{action['actionId']}/approve", "POST"), None)
    assert response["statusCode"] == 200 and json.loads(response["body"])["status"] == "executed"


# ---------------------------------------------------------------- 승인 뒤 이어서 설명 (/llm1 actionId)

class FakeClient:
    progress = redactor = audit = approvals = None
    model_id = "claude-sonnet-5"
    prompts = []

    def process_user_input(self, text, system_prompt):
        FakeClient.prompts.append(text)
        return "보존 기간을 14일로 바꿨습니다."

    def get_debug_log(self):
        return []


def test_follow_up_explains_the_stored_result(env, monkeypatch):
    llm = env["llm"]
    monkeypatch.setattr(llm, "get_client", lambda *_: FakeClient())
    action = make_action(env)

    def ask(sub):
        response = llm.handle_llm1_with_mcp({"actionId": action["actionId"], "text": "무시될 글"}, ORIGIN,
                                            caller_id=sub)
        return response["statusCode"]

    assert ask("alice") == 409  # 아직 결정 전
    decide(env, action["actionId"], "approve", "alice", groups="approvers")
    assert ask("mallory") == 404  # 남의 작업
    assert ask("alice") == 200
    prompt = FakeClient.prompts[-1]
    # 질문은 서버가 저장된 기록으로 만든다 (화면이 보낸 글은 쓰지 않는다)
    assert "무시될 글" not in prompt and "승인해 실행했습니다" in prompt and "보존 기간 30일 → 14일" in prompt


def test_llm1_response_lists_pending_actions(env, monkeypatch):
    llm = env["llm"]

    class Requesting(FakeClient):
        def process_user_input(self, text, system_prompt):
            self.approvals.request("setLogRetention", {"log_group_name": LOG_GROUP, "retention_days": 14},
                                   {"summary": "보존 기간 30일 → 14일", "before": "30일", "after": "14일"})
            return "승인이 필요합니다."

    monkeypatch.setattr(llm, "get_client", lambda *_: Requesting())
    body = json.loads(llm.handle_llm1_with_mcp({"text": "줄여줘"}, ORIGIN, caller_id="alice")["body"])
    pending = body["inference"]["pendingActions"]
    assert len(pending) == 1 and pending[0]["status"] == "pending" and pending[0]["before"] == "30일"
    assert pending[0]["args"] == {"log_group_name": LOG_GROUP, "retention_days": 14}

    # Slack 봇 요청에는 승인 요청 기능을 주지 않는다
    seen = {}

    class Slack(FakeClient):
        def process_user_input_with_history(self, text, system_prompt, previous):
            seen["approvals"] = self.approvals
            return "답"

    monkeypatch.setattr(llm, "get_client", lambda *_: Slack())
    monkeypatch.setattr(llm, "send_slack_dm", lambda user, text: None)
    llm.handle_llm1_with_mcp({"text": "줄여줘", "user_id": "U1", "previous_questions": [{"role": "user",
                                                                                   "content": "x"}]}, ORIGIN)
    assert seen["approvals"] is None


# ---------------------------------------------------------------- CloudFormation: 권한 분리

class CfnLoader(yaml.SafeLoader):
    pass


CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                                if isinstance(node, yaml.ScalarNode) else None)


def statements(role):
    template = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=CfnLoader)
    return template["Resources"][role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]


def test_only_the_mcp_role_can_change_resources_and_only_wga_ones():
    write_actions = {"logs:PutRetentionPolicy", "cloudwatch:EnableAlarmActions", "cloudwatch:DisableAlarmActions"}
    llm_actions = {a for s in statements("LlmLambdaExecutionRole") for a in s["Action"]}
    assert not (write_actions & llm_actions)  # LLM Lambda는 AWS를 바꿀 수 없다
    for statement in statements("McpLambdaExecutionRole"):
        if write_actions & set(statement["Action"]):
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            assert all("wga-" in r and "${Environment}" in r for r in resources), resources


def test_approvers_group_and_pending_table_exist():
    base = yaml.load((ROOT / "cloudformation" / "base.yaml").read_text(encoding="utf-8"), Loader=CfnLoader)
    assert base["Resources"]["ApproversGroup"]["Properties"]["GroupName"] == "approvers"
    llm = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=CfnLoader)
    table = llm["Resources"]["PendingActionsTable"]["Properties"]
    assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    for method in ("ActionGetMethod", "ActionApproveMethod", "ActionDenyMethod"):
        assert llm["Resources"][method]["Properties"]["AuthorizationType"] == "COGNITO_USER_POOLS"
