"""답변을 만드는 동안의 진행 상황과 사고 과정 (llm_progress.py, mcp_anthropic_client.py, llm_service.py)

- 사고 설정은 고른 모델(최신 Sonnet)이 지원하는 방식을 따른다 (tests/test_latest_model.py)
- 도구를 쓰는 반복에서 사고 블록을 받은 그대로 다시 보낸다
- 진행 상황은 요청한 사람만 쓰고 읽는다
"""
import copy
import json

import boto3
import pytest

from conftest import load_service_module

PROGRESS_TABLE = "wga-llm-progress-test"
REQUEST_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture
def progress_env(aws, monkeypatch):
    monkeypatch.setenv("LLM_PROGRESS_TABLE", PROGRESS_TABLE)
    boto3.client("dynamodb").create_table(
        TableName=PROGRESS_TABLE,
        AttributeDefinitions=[{"AttributeName": "requestId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb").Table(PROGRESS_TABLE)


# ---------------------------------------------------------------- 도구 반복: 사고 블록을 그대로 돌려보낸다

class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


THINK_1 = {"type": "thinking", "thinking": "로그 그룹부터 찾아야 한다.", "signature": "sig-1"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "describe_log_groups",
            "input": {"log_group_name_prefix": "/aws/lambda", "max_items": 5, "ratio": 0.5, "filters": ["a"]}}
THINK_2 = {"type": "thinking", "thinking": "오류가 3건 있다.", "signature": "sig-2"}
ANSWER = {"type": "text", "text": "지난 7일 동안 오류는 3건입니다."}


@pytest.fixture
def client_run(aws, monkeypatch):
    load_service_module("services/llm", "llm_service")
    import mcp_anthropic_client
    from llm_progress import ProgressReporter

    sent = []
    replies = [
        {"content": [THINK_1, {"type": "text", "text": "확인해 보겠습니다."}, TOOL_USE], "usage": {}},
        {"content": [THINK_2, ANSWER], "usage": {}},
    ]

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))  # 클라이언트가 보낸 뒤에도 메시지 목록을 고치므로 보낸 순간의 모습을 남긴다
        return FakeResponse(replies[len(sent) - 1])

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(
        mcp_url="https://example.invalid", api_key="k", model_id="claude-sonnet-5",
        thinking={"type": "adaptive", "display": "summarized"})
    client.tools = [{"name": "describe_log_groups", "description": "로그 그룹", "inputSchema": {},
                     "_meta": {"wga/risk": "read"}}]  # MCP tools/list가 붙이는 위험도 (없으면 변경 도구로 본다)
    monkeypatch.setattr(client.mcp_client, "call_tool",
                        lambda name, args: {"content": [{"type": "text", "text": "3 groups"}]})
    client.progress = ProgressReporter()  # 저장 없이 단계만 모은다
    answer = client.process_user_input("지난주 Lambda 오류 알려줘", "system")
    return answer, sent, client.progress


def test_thinking_is_requested_and_answer_returned(client_run):
    answer, sent, _ = client_run
    assert answer == ANSWER["text"]
    from mcp_anthropic_client import MAX_TOKENS
    assert all(p["thinking"] == {"type": "adaptive", "display": "summarized"} for p in sent)
    assert all(p["max_tokens"] == MAX_TOKENS for p in sent)


def test_thinking_blocks_are_sent_back_unchanged_with_tool_results(client_run):
    # 사고를 켜고 도구를 쓰면 다음 요청에 사고 블록(서명 포함)을 고치지 않고 돌려보내야 한다.
    # 예전에는 글과 도구 호출만 두 메시지로 나눠 넣어 사고 블록이 빠졌다
    _, sent, _ = client_run
    second = sent[1]["messages"]
    assert second[-2] == {"role": "assistant",
                          "content": [THINK_1, {"type": "text", "text": "확인해 보겠습니다."}, TOOL_USE]}
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "toolu_1"


def test_progress_steps_follow_the_order_of_events(client_run):
    _, _, progress = client_run
    steps = progress.steps
    assert [s["type"] for s in steps] == ["thinking", "tool", "thinking"]
    assert steps[0]["text"] == THINK_1["thinking"]
    tool = steps[1]
    assert tool["name"] == "describe_log_groups" and tool["status"] == "ok" and isinstance(tool["ms"], int)
    # 입력은 짧은 값만: 배열은 빼고, 실수는 글자로 (DynamoDB는 float를 받지 않는다)
    assert tool["input"] == {"log_group_name_prefix": "/aws/lambda", "max_items": 5, "ratio": "0.5"}


def test_tool_error_result_is_reported_as_failed(aws, monkeypatch):
    load_service_module("services/llm", "llm_service")
    from llm_progress import ProgressReporter

    progress = ProgressReporter()
    progress.tool_started("toolu_1", "get_active_alarms", {})
    progress.tool_finished("toolu_1", False, "AccessDenied: cloudwatch:DescribeAlarms")
    assert progress.steps[0]["status"] == "error"
    assert progress.steps[0]["error"].startswith("AccessDenied")


# ---------------------------------------------------------------- 진행 상황 저장·조회

def test_progress_is_saved_and_read_only_by_owner(progress_env):
    load_service_module("services/llm", "llm_service")
    from llm_progress import ProgressReporter, read_progress

    progress = ProgressReporter(progress_env, REQUEST_ID, "alice")
    # 시작하자마자 저장해 화면이 첫 조회에서 '생각하는 중'을 본다
    assert read_progress(progress_env, REQUEST_ID, "alice")["phase"] == "thinking"

    progress.thought("생각 중")
    progress.tool_started("toolu_1", "describe_log_groups", {"max_items": 5})
    seen = read_progress(progress_env, REQUEST_ID, "alice")
    assert seen["phase"] == "tool"
    assert seen["steps"][1] == {"type": "tool", "id": "toolu_1", "name": "describe_log_groups",
                                "input": {"max_items": 5}, "status": "running"}
    json.dumps(seen)  # DynamoDB 숫자(Decimal)를 정수로 바꿔 응답으로 보낼 수 있다

    item = progress_env.get_item(Key={"requestId": REQUEST_ID})["Item"]
    assert item["expiresAt"] > item["startedAt"] // 1000  # TTL로 지워진다

    # 남의 것은 읽을 수 없다 (없는 것과 같게 답한다)
    assert read_progress(progress_env, REQUEST_ID, "mallory") is None
    assert read_progress(progress_env, REQUEST_ID, None) is None
    assert read_progress(progress_env, "../other", "alice") is None


def test_other_user_cannot_overwrite_progress(progress_env):
    load_service_module("services/llm", "llm_service")
    from llm_progress import ProgressReporter, read_progress

    ProgressReporter(progress_env, REQUEST_ID, "alice").thought("alice의 생각")
    # 같은 requestId를 보내도 덮어쓰지 못하고, 답변은 계속 만든다 (예외가 나지 않는다)
    intruder = ProgressReporter(progress_env, REQUEST_ID, "mallory")
    intruder.thought("덮어쓰기")
    assert read_progress(progress_env, REQUEST_ID, "alice")["steps"] == [{"type": "thinking", "text": "alice의 생각"}]


def test_saved_steps_are_clipped_for_chat_history(aws):
    load_service_module("services/llm", "llm_service")
    from llm_progress import SAVED_STEP_LIMIT, SAVED_TEXT_LIMIT, ProgressReporter

    progress = ProgressReporter()
    for _ in range(SAVED_STEP_LIMIT + 5):
        progress.thought("가" * 5000)
    saved = progress.saved_steps()
    assert len(saved) == SAVED_STEP_LIMIT
    assert all(len(step["text"]) <= SAVED_TEXT_LIMIT for step in saved)


# ---------------------------------------------------------------- /llm1 요청과 진행 상황 API

class FakeClient:
    """도구 한 번을 부르고 답하는 가짜 클라이언트 (진행 상황만 확인한다)."""
    progress = None

    def process_user_input(self, text, system_prompt):
        self.progress.thought("생각")
        self.progress.tool_started("toolu_1", "get_active_alarms", {})
        self.progress.tool_finished("toolu_1", True)
        return "답변"

    def get_debug_log(self):
        return []


def test_llm1_records_progress_and_returns_steps(progress_env, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "get_client", lambda *_: FakeClient())

    body = {"text": "알람 알려줘", "requestId": REQUEST_ID}
    response = llm.handle_llm1_with_mcp(body, "https://test.abc.amplifyapp.com", caller_id="alice")
    result = json.loads(response["body"])

    assert result["answer"] == "답변"
    assert [s["type"] for s in result["inference"]["steps"]] == ["thinking", "tool"]
    item = progress_env.get_item(Key={"requestId": REQUEST_ID})["Item"]
    assert item["phase"] == "done" and item["ownerId"] == "alice"


def test_llm1_without_request_id_does_not_save_progress(progress_env, monkeypatch):
    # Slack 봇이나 예전 화면은 requestId를 보내지 않는다: 저장하지 않고 단계만 답변에 넣는다
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "get_client", lambda *_: FakeClient())

    response = llm.handle_llm1_with_mcp({"text": "알람 알려줘"}, "https://test.abc.amplifyapp.com", caller_id="alice")
    assert [s["type"] for s in json.loads(response["body"])["inference"]["steps"]] == ["thinking", "tool"]
    assert progress_env.scan()["Items"] == []


def progress_event(request_id, sub):
    event = {"path": f"/llm1/progress/{request_id}", "httpMethod": "GET",
             "headers": {"origin": "https://test.abc.amplifyapp.com"}}
    if sub:
        event["requestContext"] = {"authorizer": {"claims": {"sub": sub}}}
    return event


def test_progress_route_returns_only_the_callers_progress(progress_env):
    lambda_function = load_service_module("services/llm", "lambda_function")
    from llm_progress import ProgressReporter

    ProgressReporter(progress_env, REQUEST_ID, "alice").thought("생각")

    mine = lambda_function.lambda_handler(progress_event(REQUEST_ID, "alice"), None)
    assert mine["statusCode"] == 200
    assert json.loads(mine["body"])["steps"] == [{"type": "thinking", "text": "생각"}]

    for sub in ["mallory", None]:
        assert lambda_function.lambda_handler(progress_event(REQUEST_ID, sub), None)["statusCode"] == 404
    # 아직 첫 기록 전이면 404 (화면은 조금 뒤 다시 묻는다)
    other = "11111111-2222-4333-8444-555555555555"
    assert lambda_function.lambda_handler(progress_event(other, "alice"), None)["statusCode"] == 404
