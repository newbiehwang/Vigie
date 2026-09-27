"""감사 로그 (audit.py, mcp_anthropic_client.py, llm_service.py, lambda_function.py, llm.yaml)

- 도구 호출 한 번, 질문 하나마다 누가·무엇을·어떤 입력으로 했는지 남긴다
- 감사 로그에는 도구가 실제로 받은 값(계정 ID 원래 값)을 남기고, 비밀 값은 남기지 않는다
- 추가만 한다 (덮어쓰지 않는다). 기록에 실패해도 답변은 만든다
- 조회: 일반 사용자는 자기 기록만, admins 그룹은 모든 사람의 기록

키 모양 값은 저장소 비밀 값 검사(test_secret_patterns.py)에 걸리지 않도록 실행할 때 조각을 이어 붙여 만든다.
"""
import copy
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
import yaml

from conftest import ROOT, load_service_module

AUDIT_TABLE = "vigie-audit-test"
AUDIT_LOG_GROUP = "/vigie/test/audit"
ACCOUNT = "111122223333"
ACCESS_KEY = "AK" + "IA" + "Z7QW4ERTY6UIOP2A"
ORIGIN = "https://test.abc.amplifyapp.com"
REQUEST_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def create_audit_table():
    boto3.client("dynamodb").create_table(
        TableName=AUDIT_TABLE,
        AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name in ("userId", "at", "day")],
        KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}, {"AttributeName": "at", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "by-day",
            "KeySchema": [{"AttributeName": "day", "KeyType": "HASH"}, {"AttributeName": "at", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb").Table(AUDIT_TABLE)


@pytest.fixture
def audit_env(aws, monkeypatch):
    monkeypatch.setenv("AUDIT_TABLE", AUDIT_TABLE)
    monkeypatch.setenv("AUDIT_LOG_GROUP", AUDIT_LOG_GROUP)
    table = create_audit_table()
    boto3.client("logs").create_log_group(logGroupName=AUDIT_LOG_GROUP)
    return table


def log_records():
    logs = boto3.client("logs")
    records = []
    for stream in logs.describe_log_streams(logGroupName=AUDIT_LOG_GROUP)["logStreams"]:
        events = logs.get_log_events(logGroupName=AUDIT_LOG_GROUP, logStreamName=stream["logStreamName"])["events"]
        records += [json.loads(event["message"]) for event in events]
    return records


# ---------------------------------------------------------------- 도구 반복에서 기록

class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


ALIAS_ARN = "arn:aws:logs:us-east-1:********3333:log-group:/aws/lambda/vigie-llm-dev"
REAL_ARN = f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/vigie-llm-dev"


@pytest.fixture
def client_run(audit_env, monkeypatch):
    load_service_module("services/llm", "llm_service")
    import mcp_anthropic_client
    from audit import AuditLog, CloudWatchSink
    from llm_progress import ProgressReporter
    from redaction import Redactor

    replies = [
        {"content": [{"type": "tool_use", "id": "toolu_1", "name": "describe_log_groups",
                      "input": {"log_group_name_prefix": "/aws/lambda"}}], "usage": {}},
        {"content": [{"type": "tool_use", "id": "toolu_2", "name": "analyze_log_group",
                      "input": {"log_group_arn": ALIAS_ARN, "note": f"key {ACCESS_KEY}"}}], "usage": {}},
        {"content": [{"type": "text", "text": "오류가 없습니다."}], "usage": {}},
    ]
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse(replies[len(sent) - 1])

    def fake_call_tool(name, args):
        if name == "describe_log_groups":
            return {"content": [{"type": "text", "text": REAL_ARN}]}
        return {"isError": True, "content": [{"type": "text", "text": f"AccessDenied for {ACCESS_KEY}"}]}

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(
        mcp_url="https://example.invalid", api_key="k", model_id="claude-sonnet-5")
    read = {"vigie/risk": "read", "vigie/access": "all"}  # MCP tools/list가 붙이는 위험도·쓸 수 있는 사람
    client.tools = [{"name": "describe_log_groups", "description": "", "inputSchema": {}, "_meta": read},
                    {"name": "analyze_log_group", "description": "", "inputSchema": {}, "_meta": read}]
    monkeypatch.setattr(client.mcp_client, "call_tool", fake_call_tool)
    client.progress = ProgressReporter()
    client.redactor = Redactor([ACCOUNT])
    client.audit = AuditLog(audit_env, CloudWatchSink(AUDIT_LOG_GROUP), client.redactor, user_id="alice",
                            email="alice@example.com", source="web", request_id=REQUEST_ID, session_id="s1",
                            model_id="claude-sonnet-5", question="로그 봐줘")
    client.process_user_input("로그 봐줘", "system")
    return audit_env, client


def test_each_tool_call_is_recorded_with_the_value_the_tool_received(client_run):
    table, _ = client_run
    items = sorted(table.query(KeyConditionExpression="userId = :u",
                               ExpressionAttributeValues={":u": "alice"})["Items"], key=lambda i: i["at"])
    assert [i["tool"] for i in items] == ["describe_log_groups", "analyze_log_group"]
    first, second = items
    assert first["status"] == "ok" and first["resultChars"] > 0 and first["ms"] >= 0
    assert first["requestId"] == REQUEST_ID and first["sessionId"] == "s1" and first["email"] == "alice@example.com"
    assert first["at"].endswith("#toolu_1") and first["day"] == first["at"][:10]
    # 모델은 가명(********3333)을 줬지만, 감사 로그에는 도구가 실제로 받은 ARN을 남긴다. 비밀 값은 남기지 않는다
    assert json.loads(second["input"]) == {"log_group_arn": REAL_ARN, "note": "key [REDACTED:aws_access_key]"}
    assert second["status"] == "error" and second["error"] == "AccessDenied for [REDACTED:aws_access_key]"


def test_audit_records_are_also_written_to_cloudwatch_logs(client_run):
    records = log_records()
    assert [r["tool"] for r in records] == ["describe_log_groups", "analyze_log_group"]
    assert all(r["vigie_audit"] is True and r["userId"] == "alice" for r in records)
    assert all("expiresAt" not in r for r in records)
    assert ACCESS_KEY not in json.dumps(records)
    # 실행 환경마다 로그 스트림 하나
    assert len(boto3.client("logs").describe_log_streams(logGroupName=AUDIT_LOG_GROUP)["logStreams"]) == 1


def test_progress_still_gets_redacted_values(client_run):
    # 진행 상황(화면)은 가명, 감사 로그는 원래 값: 같은 지점에서 받지만 가리는 정도가 다르다
    _, client = client_run
    assert ACCOUNT not in json.dumps(client.progress.steps)


# ---------------------------------------------------------------- 질문 단위 기록 (/llm1)

class FakeClient:
    progress = None
    redactor = None
    audit = None
    model_id = "claude-sonnet-5"

    def process_user_input(self, text, system_prompt):
        self.progress.tool_started("toolu_1", "get_active_alarms", {})
        self.progress.tool_finished("toolu_1", True)
        self.audit.tool_started("toolu_1", "get_active_alarms", {})
        self.audit.tool_finished("toolu_1", True, None, 10)
        self.redactor.text(ACCESS_KEY)  # Claude로 보내기 전에 가린 값 하나
        return "답변"

    def process_user_input_with_history(self, text, system_prompt, previous_messages):
        return self.process_user_input(text, system_prompt)

    def get_debug_log(self):
        return []


def items_of(table, user_id):
    return table.query(KeyConditionExpression="userId = :u", ExpressionAttributeValues={":u": user_id})["Items"]


def test_llm1_records_the_request(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "get_client", lambda *_: FakeClient())

    body = {"text": f"계정 {ACCOUNT}의 키 {ACCESS_KEY} 확인", "requestId": REQUEST_ID, "sessionId": "s1"}
    llm.handle_llm1_with_mcp(body, ORIGIN, caller_id="alice", caller_email="alice@example.com")

    request = next(i for i in items_of(audit_env, "alice") if i["kind"] == "request")
    assert request["at"].endswith(f"#request#{REQUEST_ID}")
    assert request["status"] == "ok" and request["toolCount"] == 1 and request["source"] == "web"
    assert request["model"] == "claude-sonnet-5" and request["email"] == "alice@example.com"
    # 질문: 계정 ID는 남기고 비밀 값은 가린다
    assert request["question"] == f"계정 {ACCOUNT}의 키 [REDACTED:aws_access_key] 확인"
    assert request["redacted"] == {"aws_access_key": 1}


def test_slack_requests_are_recorded_by_slack_user(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "get_client", lambda *_: FakeClient())
    monkeypatch.setattr(llm, "send_slack_dm", lambda user, text: None)

    body = {"text": "알람", "user_id": "U123", "previous_questions": [{"role": "user", "content": "x"}]}
    llm.handle_llm1_with_mcp(body, ORIGIN)  # Slack 봇은 API Gateway를 거치지 않아 caller_id가 없다
    request = next(i for i in items_of(audit_env, "slack:U123") if i["kind"] == "request")
    assert request["source"] == "slack" and "email" not in request


def test_failed_request_is_recorded(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")

    class Broken(FakeClient):
        def process_user_input(self, text, system_prompt):
            raise RuntimeError(f"Anthropic 오류 {ACCESS_KEY}")

    monkeypatch.setattr(llm, "get_client", lambda *_: Broken())
    response = llm.handle_llm1_with_mcp({"text": "알람"}, ORIGIN, caller_id="alice")
    assert response["statusCode"] == 500
    request = items_of(audit_env, "alice")[0]
    assert request["status"] == "error" and request["error"] == "Anthropic 오류 [REDACTED:aws_access_key]"


def test_audit_failure_does_not_stop_the_answer(aws, monkeypatch):
    # 테이블·로그 그룹이 없어 기록에 실패해도 답변은 만든다 (지금 도구는 모두 조회용)
    monkeypatch.setenv("AUDIT_TABLE", "missing-table")
    monkeypatch.setenv("AUDIT_LOG_GROUP", "/missing/group")
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "get_client", lambda *_: FakeClient())
    response = llm.handle_llm1_with_mcp({"text": "알람"}, ORIGIN, caller_id="alice")
    assert response["statusCode"] == 200 and json.loads(response["body"])["answer"] == "답변"


def test_records_are_append_only(audit_env):
    load_service_module("services/llm", "llm_service")
    from audit import AuditLog
    from redaction import Redactor

    audit = AuditLog(audit_env, None, Redactor(), user_id="alice", email=None, source="web",
                     request_id=REQUEST_ID, session_id=None, model_id="m", question="q")
    audit.request_finished(True)
    item = items_of(audit_env, "alice")[0]
    # 같은 키로 다시 쓰면 조건부 쓰기가 막는다 (기존 기록이 바뀌지 않는다)
    with pytest.raises(Exception, match="ConditionalCheckFailed"):
        audit_env.put_item(Item={**item, "status": "changed"}, ConditionExpression="attribute_not_exists(userId)")
    assert items_of(audit_env, "alice")[0]["status"] == "ok"


# ---------------------------------------------------------------- 답변 (질문 항목의 미리보기 + 따로 둔 답변 전체)

def run_llm1(llm, monkeypatch, answer):
    class Answering(FakeClient):
        def process_user_input(self, text, system_prompt):
            super().process_user_input(text, system_prompt)
            return answer

    monkeypatch.setattr(llm, "get_client", lambda *_: Answering())
    body = {"text": "알람", "requestId": REQUEST_ID, "sessionId": "s1"}
    return llm.handle_llm1_with_mcp(body, ORIGIN, caller_id="alice", caller_email="alice@example.com")


def test_llm1_records_the_answer_the_user_received(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    answer = f"계정 {ACCOUNT}의 키 {ACCESS_KEY}는 지난주에 만들었습니다. " + "가" * 300
    response = run_llm1(llm, monkeypatch, answer)
    shown = json.loads(response["body"])["answer"]  # 사용자가 받은 답변 (가명·가림 적용 뒤)
    assert ACCESS_KEY not in shown

    items = items_of(audit_env, "alice")
    request = next(i for i in items if i["kind"] == "request")
    saved = next(i for i in items if i["kind"] == "answer")
    # 답변 항목: 사용자가 받은 글자 그대로 전체, 질문 항목과 같은 시각·질문 ID
    assert saved["answer"] == shown and saved["answerChars"] == len(shown)
    assert saved["at"] == request["at"].replace("#request#", "#answer#")
    assert saved["requestId"] == REQUEST_ID and saved["sessionId"] == "s1"
    # 날짜 인덱스에 넣지 않는다 (day가 없다)
    assert "day" not in saved and "day" in request
    # 질문 항목에는 앞부분만
    assert request["answerPreview"] == shown[:199] + "…" and request["answerChars"] == len(shown)


def test_answer_goes_to_cloudwatch_on_the_request_line(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    response = run_llm1(llm, monkeypatch, "오류가 없습니다.")
    records = log_records()
    # 답변 항목을 따로 쓰지 않고, 질문 줄에 답변 전체를 함께 쓴다
    assert [r["kind"] for r in records] == ["tool", "request"]
    assert records[1]["answer"] == json.loads(response["body"])["answer"] == "오류가 없습니다."


def test_very_long_answer_is_clipped_to_the_safety_limit(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    from audit import ANSWER_LIMIT
    run_llm1(llm, monkeypatch, "가" * (ANSWER_LIMIT + 10))
    saved = next(i for i in items_of(audit_env, "alice") if i["kind"] == "answer")
    assert len(saved["answer"]) == ANSWER_LIMIT and saved["answerChars"] == ANSWER_LIMIT + 10


def test_failed_request_has_no_answer(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")

    class Broken(FakeClient):
        def process_user_input(self, text, system_prompt):
            raise RuntimeError("Anthropic 오류")

    monkeypatch.setattr(llm, "get_client", lambda *_: Broken())
    llm.handle_llm1_with_mcp({"text": "알람"}, ORIGIN, caller_id="alice")
    (request,) = items_of(audit_env, "alice")
    assert request["kind"] == "request" and "answerPreview" not in request


@pytest.fixture
def answered(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    run_llm1(llm, monkeypatch, "오류가 없습니다.")
    request = next(i for i in items_of(audit_env, "alice") if i["kind"] == "request")
    return audit_env, request


def run_answer(table, groups="admins", **params):
    from audit import query_answer
    claims = {"sub": "admin-1", "cognito:groups": groups}
    return query_answer(table, "admin-1", claims, params)


def test_answers_are_not_in_the_list(answered):
    table, _ = answered
    # 모든 사람(날짜 인덱스)도, 한 사람(기본 키)도 답변 항목은 목록에 없다
    for params in ({"scope": "all"}, {"scope": "all", "user": "alice"}):
        kinds = [item["kind"] for item in run_query(table, caller="admin-1", groups="admins", **params)["items"]]
        assert sorted(kinds) == ["request", "tool"]


def test_admins_read_the_full_answer_by_the_request_row(answered):
    table, request = answered
    result = run_answer(table, answer=request["at"], user="alice")
    assert result == {"answer": "오류가 없습니다.", "answerChars": 9}


@pytest.mark.parametrize("groups, params, status", [
    ("", {"answer": "x", "user": "alice"}, 403),  # 관리자만
    ("admins", {"answer": "2026-09-26T10:00:00.000Z#toolu_1", "user": "alice"}, 400),  # 질문 행이 아니다
    ("admins", {"answer": "2026-09-26T10:00:00.000Z#request#r1"}, 400),  # 요청자 없음
    ("admins", {"answer": "2026-09-26T10:00:00.000Z#request#r1", "user": "alice"}, 404),  # 답변 기록 없음
])
def test_bad_answer_queries_are_rejected(answered, groups, params, status):
    from audit import AuditQueryError
    with pytest.raises(AuditQueryError) as error:
        run_answer(answered[0], groups=groups, **params)
    assert error.value.status == status


def test_answer_route(answered):
    table, request = answered
    lambda_function = load_service_module("services/llm", "lambda_function")
    event = {"path": "/audit", "httpMethod": "GET", "headers": {"origin": ORIGIN},
             "queryStringParameters": {"answer": request["at"], "user": "alice"},
             "requestContext": {"authorizer": {"claims": {"sub": "bob"}}}}
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 403
    event["requestContext"]["authorizer"]["claims"]["cognito:groups"] = "admins"
    response = lambda_function.lambda_handler(event, None)
    assert response["statusCode"] == 200 and json.loads(response["body"])["answer"] == "오류가 없습니다."


# ---------------------------------------------------------------- 토큰과 예상 비용 (질문 행, llm_cost.py)

USAGE = {"tokens": {"input": 1000, "output": 500, "cacheWrite": 2000, "cacheRead": 10000},
         "calls": [{"input": 600, "output": 200, "cacheWrite": 2000, "cacheRead": 0},
                   {"input": 400, "output": 300, "cacheWrite": 0, "cacheRead": 10000}]}


class CountingClient(FakeClient):
    def usage_summary(self):
        return copy.deepcopy(USAGE)


def test_llm1_records_tokens_and_estimated_cost(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "get_client", lambda *_: CountingClient())
    llm.handle_llm1_with_mcp({"text": "알람", "requestId": REQUEST_ID}, ORIGIN, caller_id="alice")
    request = next(i for i in items_of(audit_env, "alice") if i["kind"] == "request")
    assert request["tokens"] == USAGE["tokens"] and request["modelCalls"] == USAGE["calls"]
    # Sonnet 5: 1000×2 + 500×10 + 2000×2.5 + 10000×0.2 = 14,000 마이크로달러, 계산에 쓴 단가도 함께
    assert request["costMicroUsd"] == 14000
    assert request["price"] == {"input": "2", "output": "10", "cacheWrite": "2.50", "cacheRead": "0.20"}
    # CloudWatch Logs에도 같은 값 (정수라 Logs Insights에서 바로 더할 수 있다)
    logged = next(r for r in log_records() if r["kind"] == "request")
    assert logged["costMicroUsd"] == 14000 and logged["tokens"]["cacheRead"] == 10000


def test_unknown_model_keeps_tokens_without_cost(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")

    class NewModel(CountingClient):
        model_id = "claude-sonnet-9"

    monkeypatch.setattr(llm, "get_client", lambda *_: NewModel())
    llm.handle_llm1_with_mcp({"text": "알람"}, ORIGIN, caller_id="alice")
    request = next(i for i in items_of(audit_env, "alice") if i["kind"] == "request")
    assert request["tokens"] == USAGE["tokens"]
    assert "costMicroUsd" not in request and "price" not in request


def test_failed_request_keeps_the_tokens_it_used(audit_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")

    class Broken(CountingClient):
        def process_user_input(self, text, system_prompt):
            raise RuntimeError("Anthropic 오류")

    monkeypatch.setattr(llm, "get_client", lambda *_: Broken())
    llm.handle_llm1_with_mcp({"text": "알람"}, ORIGIN, caller_id="alice")
    (request,) = items_of(audit_env, "alice")
    assert request["status"] == "error" and request["costMicroUsd"] == 14000


def test_clients_without_usage_leave_no_tokens(audit_env, monkeypatch):
    # usage_summary가 없는 클라이언트(Bedrock)는 토큰·비용 없이 남긴다
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "get_client", lambda *_: FakeClient())
    llm.handle_llm1_with_mcp({"text": "알람"}, ORIGIN, caller_id="alice")
    request = next(i for i in items_of(audit_env, "alice") if i["kind"] == "request")
    assert "tokens" not in request and "costMicroUsd" not in request


def test_client_counts_tokens_per_model_call(client_run):
    _, client = client_run
    # 도구 반복의 모델 호출 세 번 (usage가 빈 응답은 0으로 센다)
    summary = client.usage_summary()
    assert len(summary["calls"]) == 3
    client._add_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7,
                       "cache_creation_input_tokens": 3})
    summary = client.usage_summary()
    assert summary["calls"][-1] == {"input": 10, "output": 5, "cacheWrite": 3, "cacheRead": 7}
    assert summary["tokens"] == {"input": 10, "output": 5, "cacheWrite": 3, "cacheRead": 7}


# ---------------------------------------------------------------- 조회: GET /audit

def put(table, user, at, tool="get_active_alarms", status="ok", kind="tool"):
    table.put_item(Item={"userId": user, "at": at, "day": at[:10], "kind": kind, "tool": tool, "status": status,
                         "input": json.dumps({"n": 1}), "expiresAt": 1})


@pytest.fixture
def seeded(audit_env):
    load_service_module("services/llm", "llm_service")
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=offset)).isoformat() for offset in range(3)]
    for index, day in enumerate(days):
        put(audit_env, "alice", f"{day}T10:00:00.000Z#toolu_a{index}")
        put(audit_env, "alice", f"{day}T11:00:00.000Z#toolu_b{index}", tool="describe_log_groups", status="error")
        put(audit_env, "bob", f"{day}T12:00:00.000Z#toolu_c{index}")
    return audit_env, days


def run_query(table, caller="alice", groups=None, **params):
    from audit import query_audit
    claims = {"sub": caller}
    if groups is not None:
        claims["cognito:groups"] = groups
    return query_audit(table, caller, claims, {k: str(v) for k, v in params.items()})


def test_admins_see_their_own_records_by_default(seeded):
    table, days = seeded
    result = run_query(table, groups="admins", **{"from": days[-1]})
    assert {i["userId"] for i in result["items"]} == {"alice"} and len(result["items"]) == 6
    assert result["scope"] == "mine" and result["isAdmin"] is True
    # 최신 기록부터, 입력은 객체로, TTL 값은 빼고
    assert [i["at"] for i in result["items"]] == sorted((i["at"] for i in result["items"]), reverse=True)
    assert result["items"][0]["input"] == {"n": 1} and "expiresAt" not in result["items"][0]


# 감사 로그는 관리자만: 일반 사용자는 자기 기록도, 남의 기록도 못 본다. 승인자(approvers)만으로도 안 된다.
# 잘못된 조건(scope=everyone)에도 400이 아니라 403: 조건을 검사하기 전에 막는다
@pytest.mark.parametrize("groups", [None, "approvers", "[approvers readers]"])
@pytest.mark.parametrize("params", [{}, {"scope": "mine"}, {"user": "alice"}, {"scope": "all"}, {"user": "bob"},
                                    {"scope": "everyone"}])
def test_users_cannot_read_audit_records(seeded, groups, params):
    from audit import AuditQueryError
    with pytest.raises(AuditQueryError) as error:
        run_query(seeded[0], groups=groups, **params)
    assert error.value.status == 403


def test_admins_see_everyone_across_days_with_paging(seeded):
    table, days = seeded
    seen, cursor = [], None
    for _ in range(10):
        params = {"scope": "all", "from": days[-1], "limit": 4}
        if cursor:
            params["cursor"] = cursor
        page = run_query(table, groups="admins", **params)
        assert len(page["items"]) <= 4
        seen += page["items"]
        cursor = page["cursor"]
        if not cursor:
            break
    assert len(seen) == 9 and {i["userId"] for i in seen} == {"alice", "bob"}
    assert len({i["at"] for i in seen}) == 9  # 겹치거나 빠진 기록이 없다
    assert [i["day"] for i in seen] == sorted((i["day"] for i in seen), reverse=True)


def test_admins_can_read_one_user_and_filter(seeded):
    table, days = seeded
    result = run_query(table, caller="carol", groups="[approvers admins]", user="alice", tool="describe_log_groups",
                       status="error", **{"from": days[-1]})
    assert result["scope"] == "user" and len(result["items"]) == 3
    assert {i["tool"] for i in result["items"]} == {"describe_log_groups"}


def test_filtered_paging_does_not_skip_records(seeded):
    # 거르기 때문에 페이지가 비어도 cursor로 이어 읽으면 빠짐없이 나온다
    table, days = seeded
    seen, cursor = [], None
    for _ in range(20):
        params = {"status": "error", "from": days[-1], "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = run_query(table, groups="admins", **params)
        seen += page["items"]
        cursor = page["cursor"]
        if not cursor:
            break
    assert len(seen) == 3


@pytest.mark.parametrize("params, status", [
    ({"from": "2026-13-01"}, 400),
    ({"from": "2026-09-10", "to": "2026-09-01"}, 400),
    ({"from": "2026-01-01", "to": "2026-03-01"}, 400),  # 31일 넘는 기간
    ({"scope": "everyone"}, 400),
    ({"limit": "many"}, 400),
    ({"cursor": "not-base64!"}, 400),
])
def test_bad_queries_are_rejected(seeded, params, status):
    from audit import AuditQueryError
    with pytest.raises(AuditQueryError) as error:
        run_query(seeded[0], groups="admins", **params)
    assert error.value.status == status


def test_cursor_for_another_user_is_rejected(seeded):
    from audit import AuditQueryError, _encode_cursor
    cursor = _encode_cursor({"key": {"userId": "bob", "at": "2026-09-25T00:00:00.000Z#x"}})
    with pytest.raises(AuditQueryError) as error:
        run_query(seeded[0], groups="admins", cursor=cursor)
    assert error.value.status == 400


@pytest.mark.parametrize("raw, groups", [
    ("admins", ["admins"]),
    ("[admins approvers]", ["admins", "approvers"]),
    ("admins,approvers", ["admins", "approvers"]),
    (["admins"], ["admins"]),
    (None, []),
])
def test_cognito_groups_claim_formats(aws, raw, groups):
    load_service_module("services/llm", "llm_service")
    from audit import groups_of
    assert groups_of({"cognito:groups": raw}) == groups


def test_audit_route(seeded):
    lambda_function = load_service_module("services/llm", "lambda_function")
    event = {"path": "/audit", "httpMethod": "GET", "headers": {"origin": ORIGIN},
             "queryStringParameters": {"scope": "all"},
             "requestContext": {"authorizer": {"claims": {"sub": "alice"}}}}
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 403
    # 일반 사용자는 자기 기록(조건 없음 = scope mine)도 못 본다
    event["queryStringParameters"] = None
    response = lambda_function.lambda_handler(event, None)
    assert response["statusCode"] == 403 and "관리자" in json.loads(response["body"])["error"]
    event["queryStringParameters"] = {"scope": "all"}

    event["requestContext"]["authorizer"]["claims"]["cognito:groups"] = "admins"
    response = lambda_function.lambda_handler(event, None)
    assert response["statusCode"] == 200 and len(json.loads(response["body"])["items"]) == 9  # 기본 기간(최근 7일)

    event["queryStringParameters"] = None
    event["requestContext"] = {}
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 401


def test_llm1_route_passes_the_email_claim(audit_env, monkeypatch):
    lambda_function = load_service_module("services/llm", "lambda_function")
    captured = {}
    monkeypatch.setattr(lambda_function, "handle_llm1_with_mcp",
                        lambda body, origin, caller_id, email, groups: captured.update(
                            sub=caller_id, email=email, groups=groups) or {})
    lambda_function.lambda_handler({"path": "/llm1", "httpMethod": "POST", "headers": {}, "body": "{}",
                                    "requestContext": {"authorizer": {"claims": {
                                        "sub": "alice", "email": "alice@example.com",
                                        "cognito:groups": "admins"}}}}, None)
    # 그룹도 넘긴다: 관리자 전용 도구를 쓸 수 있는지 정한다 (tool_access.py)
    assert captured == {"sub": "alice", "email": "alice@example.com", "groups": ["admins"]}


# ---------------------------------------------------------------- CloudFormation

class CfnLoader(yaml.SafeLoader):
    pass


# !GetAtt·!Sub 등은 글자 그대로 둔다 (어떤 리소스를 가리키는지 확인할 수 있게)
CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                                if isinstance(node, yaml.ScalarNode) else None)


def template(name):
    return yaml.load((ROOT / "cloudformation" / name).read_text(encoding="utf-8"), Loader=CfnLoader)


def test_audit_resources_in_template():
    resources = template("llm.yaml")["Resources"]
    table = resources["AuditTable"]["Properties"]
    assert table["TimeToLiveSpecification"] == {"AttributeName": "expiresAt", "Enabled": True}
    assert table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert table["GlobalSecondaryIndexes"][0]["IndexName"] == "by-day"
    assert resources["AuditLogGroup"]["Properties"]["RetentionInDays"] == 365
    assert resources["AuditMethod"]["Properties"]["AuthorizationType"] == "COGNITO_USER_POOLS"


def test_llm_role_can_only_append_and_read_audit_records():
    statements = template("llm.yaml")["Resources"]["LlmLambdaExecutionRole"]["Properties"]["Policies"][0][
        "PolicyDocument"]["Statement"]
    # 감사 테이블에 대한 권한만 본다 (승인 테이블에는 UpdateItem이 있다)
    audit = [s for s in statements if "AuditTable" in json.dumps(s.get("Resource"), default=str)]
    actions = {action for statement in audit for action in statement["Action"] if action.startswith("dynamodb:")}
    assert actions == {"dynamodb:PutItem", "dynamodb:Query"}


def test_admins_group_exists():
    group = template("base.yaml")["Resources"]["AdminsGroup"]
    assert group["Type"] == "AWS::Cognito::UserPoolGroup" and group["Properties"]["GroupName"] == "admins"


# ---------------------------------------------------------------- 화면 (정적 확인: 프런트엔드 테스트 도구가 없다)

def test_screen_shows_audit_only_to_admins():
    frontend = ROOT / "frontend" / "src"
    # 탭: 감사 로그는 관리자에게만 보인다
    navigation = (frontend / "components" / "layout" / "Navigation.tsx").read_text(encoding="utf-8")
    assert "{ label: '감사 로그', to: '/audit', adminOnly: true }" in navigation
    assert "{ label: '사용자 관리', to: '/users', adminOnly: true }" in navigation
    assert "!item.adminOnly || isAdmin(user)" in navigation
    # 경로: 주소로 바로 들어와도 관리자가 아니면 홈으로
    app = (frontend / "App.tsx").read_text(encoding="utf-8")
    assert '<Route path="/audit" element={isAdmin(user) ? <AuditPage /> : <Navigate to="/" replace />} />' in app
    assert '<Route path="/users" element={isAdmin(user) ? <UsersPage /> : <Navigate to="/" replace />} />' in app
    # 관리자 여부는 ID 토큰의 cognito:groups에서 읽고, 서버와 같은 그룹 이름을 쓴다
    auth = (frontend / "auth" / "authClient.ts").read_text(encoding="utf-8")
    assert "claims['cognito:groups']" in auth
    server = (ROOT / "services" / "llm" / "audit.py").read_text(encoding="utf-8")
    assert 'ADMIN_GROUP = "admins"' in server and "export const ADMIN_GROUP = 'admins';" in auth
