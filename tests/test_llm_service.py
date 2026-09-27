"""LLM 서비스: 웹 요청의 Slack 전용 필드 제거, 세션 히스토리 소유자 확인, CORS 허용 목록"""
import json

import boto3
import pytest

from conftest import load_service_module


@pytest.fixture
def llm_lambda(monkeypatch):
    module = load_service_module("services/llm", "lambda_function")
    calls = []
    monkeypatch.setattr(module, "handle_llm1_with_mcp",
                        lambda body, origin, caller_id=None, caller_email=None, caller_groups=None:
                        calls.append((body, caller_id)) or {"statusCode": 200})
    module.calls = calls
    return module


def llm1_event(sub=None):
    ev = {"path": "/llm1", "httpMethod": "POST", "headers": {"content-type": "application/json"},
          "body": json.dumps({"question": "q", "user_id": "U_VICTIM", "previous_questions": ["x"]})}
    if sub:
        ev["requestContext"] = {"authorizer": {"claims": {"sub": sub}}}
    return ev


def test_web_request_cannot_use_slack_fields(llm_lambda):
    llm_lambda.lambda_handler(llm1_event(sub="alice"), None)
    body, caller = llm_lambda.calls[-1]
    assert caller == "alice"
    assert "user_id" not in body and "previous_questions" not in body


def test_direct_invoke_from_slackbot_keeps_slack_fields(llm_lambda):
    llm_lambda.lambda_handler(llm1_event(), None)
    body, caller = llm_lambda.calls[-1]
    assert caller is None and body["user_id"] == "U_VICTIM"


@pytest.fixture
def llm_service(aws):
    boto3.client("dynamodb").create_table(
        TableName="vigie-chat-history-test",
        AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.resource("dynamodb").Table("vigie-chat-history-test").put_item(Item={
        "sessionId": "s1", "userId": "alice",
        "messages": [{"sender": "user", "text": "비밀 질문", "timestamp": "1"},
                     {"sender": "assistant", "text": "비밀 답변", "timestamp": "2"}],
    })
    return load_service_module("services/llm", "llm_service")


def test_session_history_loaded_for_owner(llm_service):
    messages = llm_service.get_session_messages_as_array("s1", "alice")
    assert [m["content"] for m in messages] == ["비밀 질문", "비밀 답변"]


@pytest.mark.parametrize("caller", ["mallory", None])
def test_session_history_hidden_from_others(llm_service, caller):
    assert llm_service.get_session_messages_as_array("s1", caller) == []


@pytest.mark.parametrize("origin,expected", [
    ("https://test.abc.amplifyapp.com", "https://test.abc.amplifyapp.com"),
    ("https://evil.example.com", "https://test.abc.amplifyapp.com"),
    ("http://localhost:5173", "https://test.abc.amplifyapp.com"),  # dev 환경에서만 허용
])
def test_cors_reflects_only_allowed_origins(origin, expected):
    from common.utils import cors_headers
    assert cors_headers(origin)["Access-Control-Allow-Origin"] == expected


def test_cors_allows_localhost_in_dev(test_config, monkeypatch):
    from common.utils import cors_headers
    monkeypatch.setitem(test_config, "env", "dev")
    assert cors_headers("http://localhost:5173")["Access-Control-Allow-Origin"] == "http://localhost:5173"


# --- 도구 호출 기록 (화면의 도구 목록) --------------------------------------------------------------

def test_tool_call_is_one_step_not_two(llm_service):
    # 클라이언트는 호출 직전과 끝난 뒤에 tool_result를 한 번씩 남긴다. 끝난 쪽만 한 건으로 센다
    log = [
        {"type": "tool_result", "tool_name": "list_log_groups", "input": {"prefix": "/aws"}},
        {"type": "tool_result", "tool_name": "list_log_groups", "input": {"prefix": "/aws"},
         "output": {"content": [{"type": "text", "text": "..."}]}},
        {"type": "tool_result", "tool_name": "analyze_log_group", "input": {"log_group_name": "/aws/x"}},
        {"type": "tool_error", "tool_name": "analyze_log_group", "input": {"log_group_name": "/aws/x"},
         "error": "도구 호출 오류: AccessDenied"},
        {"type": "tool_result", "tool_name": "get_dashboard_summary", "input": {"dashboard_name": "d"},
         "output": {"content": [], "isError": True}},
        {"type": "model_reasoning", "content": "..."},
    ]
    steps = [s for s in (llm_service.tool_step(e) for e in log) if s]
    assert steps == [
        {"tool_name": "list_log_groups", "input": {"prefix": "/aws"}, "status": "ok"},
        {"tool_name": "analyze_log_group", "input": {"log_group_name": "/aws/x"}, "status": "error",
         "error": "도구 호출 오류: AccessDenied"},
        {"tool_name": "get_dashboard_summary", "input": {"dashboard_name": "d"}, "status": "error"},
    ]


def test_anthropic_tools_keep_the_full_input_schema(aws):
    # AWS 공식 MCP 도구는 배열 안의 객체(items), 선택 인자(anyOf), 선택지(enum)를 쓴다.
    # 예전처럼 속성마다 type·description만 남기면 모델이 인자를 엉뚱한 모양으로 보낸다
    load_service_module("services/llm", "llm_service")
    from mcp_anthropic_client import AnthropicMCPClient

    client = AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k", model_id="claude-sonnet-5")
    schema = {
        "type": "object",
        "properties": {
            "dimensions": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
            "statistic": {"anyOf": [{"enum": ["Sum", "Average"]}, {"type": "null"}], "default": None},
        },
        "required": ["dimensions"],
    }
    everyone = {"vigie/risk": "read", "vigie/access": "all"}  # MCP tools/list가 붙이는 표시 (없으면 관리자 전용)
    client.tools = [{"name": "get_metric_data", "description": "메트릭 조회", "inputSchema": schema, "_meta": everyone},
                    {"name": "listCloudwatchDashboards", "description": "대시보드 목록", "inputSchema": {},
                     "_meta": everyone}]
    client.tool_search = False  # 모든 도구를 싣는 경우 (도구 검색을 켠 경우는 test_tool_search.py)

    converted = {tool["name"]: tool for tool in client._convert_tools_format()}

    assert converted["get_metric_data"]["input_schema"] == schema
    # 인자가 없는 도구도 Anthropic이 받는 모양(object + properties)이 된다
    assert converted["listCloudwatchDashboards"]["input_schema"] == {"type": "object", "properties": {}}
    # 도구 목록은 프롬프트 캐시에 올린다 (마지막 도구에만 표시)
    assert converted["listCloudwatchDashboards"]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in converted["get_metric_data"]
