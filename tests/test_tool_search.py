"""도구 검색 (services/llm/tool_search.py, mcp_anthropic_client.py, scripts/measure_tool_search.py)

실제 MCP 서버 코드(mcp/app.py)의 tools/list로, LLM Lambda가 Anthropic에 보내는 요청 모양을 확인한다
(Anthropic API는 가짜 응답이라 돈이 들지 않는다).

- 모든 도구 정의는 보내되, 자주 쓰는 몇 개만 처음부터 싣고 나머지는 지연한다. 검색 도구 자체는 지연하지 않는다
- 캐시 표시는 지연하지 않은 도구와 마지막 사용자 메시지에만 (지연한 도구에 붙이면 400)
- 검색 블록은 받은 그대로 돌려보내고, 검색에는 tool_result를 보내지 않는다. 화면에는 '도구 찾기'로 보인다
- 모델이 도구 검색을 받지 않으면 모든 도구를 실어 다시 보내고, 그 모델에서는 계속 끈다
- TOOL_SEARCH=off면 예전과 같다
"""
import copy
import importlib.util
import json

import pytest

from conftest import ROOT
from test_approvals import ORIGIN, FakeResponse, env  # noqa: F401 (env는 fixture)


def real_tools(env):  # noqa: F811
    return json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]


def make_client(monkeypatch, env, replies, calls=None):  # noqa: F811
    """가짜 Anthropic 응답을 차례로 돌려주는 클라이언트. 보낸 요청은 sent에 쌓인다."""
    llm = env["llm"]
    import mcp_anthropic_client

    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        reply = replies[len(sent) - 1]
        return reply if isinstance(reply, ErrorResponse) else FakeResponse(reply)

    def call_tool(name, args=None, meta=None):
        (calls if calls is not None else []).append(name)
        return {"content": [{"type": "text", "text": '{"events": []}'}]}

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = real_tools(env)
    monkeypatch.setattr(client.mcp_client, "call_tool", call_tool)
    monkeypatch.setattr(llm, "get_client", lambda *_: client)
    return llm, client, sent


class ErrorResponse:
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.text = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}})

    def json(self):
        return json.loads(self.text)


SEARCH = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "tool_search_tool_regex",
          "input": {"pattern": "lookup_events"}}
SEARCH_RESULT = {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_1",
                 "content": {"type": "tool_search_tool_search_result",
                             "tool_references": [{"type": "tool_reference", "tool_name": "lookup_events"}]}}
CALL = {"type": "tool_use", "id": "toolu_1", "name": "lookup_events",
        "input": {"lookup_attributes": [{"AttributeKey": "EventName", "AttributeValue": "AuthorizeSecurityGroupIngress"}]}}
USAGE = {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 0}
ANSWER = {"content": [{"type": "text", "text": "어제 보안 그룹 규칙을 바꾼 기록은 없습니다."}], "usage": USAGE,
          "stop_reason": "end_turn"}


# ---------------------------------------------------------------- 요청 모양

def test_real_tool_list_is_split_into_loaded_and_deferred(env, monkeypatch):  # noqa: F811
    import tool_search
    llm, client, _ = make_client(monkeypatch, env, [])
    names = {tool["name"] for tool in client.tools}
    tools = client._convert_tools_format()

    # 검색 도구가 맨 앞에 있고 지연하지 않는다 (모두 지연하면 400)
    assert tools[0] == tool_search.SEARCH_TOOL and "defer_loading" not in tools[0]
    # 모든 도구의 정의를 보낸다 (API가 검색하고 펼치려면 필요하다)
    assert {tool["name"] for tool in tools[1:]} == names
    # 처음부터 싣는 도구는 실제로 있는 이름이다 (공식 서버가 이름을 바꾸면 여기서 알 수 있다)
    loaded = [tool["name"] for tool in tools[1:] if not tool.get("defer_loading")]
    assert set(tool_search.ALWAYS_LOADED) <= names
    assert sorted(loaded) == sorted(tool_search.ALWAYS_LOADED)
    # 캐시 표시는 하나, 지연하지 않은 도구에만
    cached = [tool for tool in tools if "cache_control" in tool]
    assert len(cached) == 1 and not cached[0].get("defer_loading") and cached[0]["name"] in tool_search.ALWAYS_LOADED

    # 처음부터 모델에 보이는 정의가 크게 준다 (지금 약 90%)
    def size(items):
        return len(json.dumps([t for t in items if not t.get("defer_loading")], ensure_ascii=False))
    client.tool_search = False
    assert size(tools) < 0.2 * size(client._convert_tools_format())


def test_off_keeps_the_old_request(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TOOL_SEARCH", "off")
    llm, client, sent = make_client(monkeypatch, env, [ANSWER])
    assert client.tool_search is False
    llm.handle_llm1_with_mcp({"text": "알람 있어?"}, ORIGIN, caller_id="alice")

    tools = sent[0]["tools"]
    assert all("defer_loading" not in tool and "type" not in tool for tool in tools)
    assert "cache_control" in tools[-1]
    assert "<Tool search>" not in sent[0]["system"]


def test_hint_names_the_loaded_tools():
    from conftest import load_service_module
    load_service_module("services/llm", "tool_search")
    import tool_search
    assert all(name in tool_search.SYSTEM_HINT for name in tool_search.ALWAYS_LOADED)
    assert "tool_search_tool_regex" in tool_search.SYSTEM_HINT


# ---------------------------------------------------------------- 검색하고 부르기

def test_search_blocks_are_sent_back_and_shown(env, monkeypatch):  # noqa: F811
    calls = []
    first = {"content": [SEARCH, SEARCH_RESULT, CALL], "usage": USAGE, "stop_reason": "tool_use"}
    llm, client, sent = make_client(monkeypatch, env, [first, ANSWER], calls)

    response = llm.handle_llm1_with_mcp({"text": "어제 누가 보안 그룹 규칙을 바꿨어?"}, ORIGIN, caller_id="alice")
    body = json.loads(response["body"])

    # 첫 요청: 시스템 프롬프트에 찾는 방법이 있고, 도구 목록에 검색 도구가 있다
    system = sent[0]["system"]
    # 시스템 프롬프트는 캐시되는 본문(찾는 방법 포함, 캐시 표시) + 요청 시각 두 블록이다 (system_prompt.build_system_blocks)
    assert "<Tool search>" in system[0]["text"] and system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1]["text"].startswith("The current time is UTC") and "cache_control" not in system[1]
    assert sent[0]["tools"][0]["name"] == "tool_search_tool_regex"
    # 찾은 도구만 실행했다 (검색은 Anthropic 서버에서 이미 끝났다)
    assert calls == ["lookup_events"]
    # 두 번째 요청: 검색 블록을 고치지 않고 돌려보내고, tool_result는 tool_use에만 보낸다
    assistant, results = sent[1]["messages"][-2], sent[1]["messages"][-1]
    assert assistant["content"][:2] == [SEARCH, SEARCH_RESULT]
    assert [block["tool_use_id"] for block in results["content"]] == ["toolu_1"]

    # 화면: 사고·도구 사이에 '도구 찾기' 단계가 남는다. 도구 호출 지표에는 세지 않는다
    steps = body["inference"]["steps"]
    assert {"type": "search", "query": "lookup_events", "found": ["lookup_events"]} in steps
    assert [s["name"] for s in steps if s["type"] == "tool"] == ["lookup_events"]
    # 측정: 도구 검색 횟수와 캐시 토큰이 답변의 token_usage에 남는다
    usage = body["inference"]["token_usage"]
    assert usage["tool_searches"] == 1 and usage["cache_read_input_tokens"] == 6000


def test_cache_breakpoint_is_on_the_last_user_block_only(env, monkeypatch):  # noqa: F811
    first = {"content": [SEARCH, SEARCH_RESULT, CALL], "usage": USAGE, "stop_reason": "tool_use"}
    llm, client, sent = make_client(monkeypatch, env, [first, ANSWER])
    llm.handle_llm1_with_mcp({"text": "어제 누가 보안 그룹 규칙을 바꿨어?"}, ORIGIN, caller_id="alice")

    for request in sent:
        marked = [block for message in request["messages"] if isinstance(message["content"], list)
                  for block in message["content"] if "cache_control" in block]
        # 요청마다 대화 쪽 표시는 하나(마지막 사용자 메시지의 마지막 블록). 도구 목록 표시와 합쳐 4개를 넘지 않는다
        assert marked == [request["messages"][-1]["content"][-1]]
        assert sum("cache_control" in tool for tool in request["tools"]) == 1
        assert not any("cache_control" in tool for tool in request["tools"] if tool.get("defer_loading"))
    # 표시는 보낼 사본에만 붙인다 (다음 반복에 표시가 쌓이지 않는다)
    assert all("cache_control" not in json.dumps(message) for message in client.messages)


def test_pause_turn_is_continued(env, monkeypatch):  # noqa: F811
    paused = {"content": [SEARCH, SEARCH_RESULT], "usage": USAGE, "stop_reason": "pause_turn"}
    llm, client, sent = make_client(monkeypatch, env, [paused, ANSWER])
    body = json.loads(llm.handle_llm1_with_mcp({"text": "보안 그룹 변경 기록"}, ORIGIN, caller_id="alice")["body"])

    # 멈춘 응답을 그대로 이어 붙여 다시 보냈고, 마지막 답을 돌려준다
    assert len(sent) == 2 and sent[1]["messages"][-1] == {"role": "assistant", "content": [SEARCH, SEARCH_RESULT]}
    assert body["answer"] == "어제 보안 그룹 규칙을 바꾼 기록은 없습니다."


def test_unsupported_model_falls_back_to_all_tools(env, monkeypatch):  # noqa: F811
    rejected = ErrorResponse(400, "tool_search_tool_regex_20251119 is not supported on this model")
    llm, client, sent = make_client(monkeypatch, env, [rejected, ANSWER, ANSWER])

    body = json.loads(llm.handle_llm1_with_mcp({"text": "알람 있어?"}, ORIGIN, caller_id="alice")["body"])
    assert body["answer"] == "어제 보안 그룹 규칙을 바꾼 기록은 없습니다."
    # 같은 요청을 모든 도구로 다시 보냈다: 검색 도구·지연 표시·찾는 방법 안내가 없다
    retry = sent[1]
    assert all("defer_loading" not in tool and "type" not in tool for tool in retry["tools"])
    assert "<Tool search>" not in retry["system"]
    # 이 모델(클라이언트는 모델별로 캐시된다)에서는 다음 질문부터 처음부터 끈다
    llm.handle_llm1_with_mcp({"text": "알람 있어?"}, ORIGIN, caller_id="alice")
    assert len(sent) == 3 and not any(tool.get("defer_loading") for tool in sent[2]["tools"])


def test_other_errors_are_not_retried(env, monkeypatch):  # noqa: F811
    llm, client, sent = make_client(monkeypatch, env, [ErrorResponse(429, "rate_limit_error")])
    body = json.loads(llm.handle_llm1_with_mcp({"text": "알람 있어?"}, ORIGIN, caller_id="alice")["body"])
    assert len(sent) == 1 and "429" in body["answer"] and client.tool_search is True


# ---------------------------------------------------------------- 작은 단위

def test_searches_reads_results_and_errors():
    from conftest import load_service_module
    load_service_module("services/llm", "tool_search")
    import tool_search
    failed = {"type": "server_tool_use", "id": "srvtoolu_2", "name": "tool_search_tool_regex",
              "input": {"pattern": "(("}}
    failed_result = {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_2",
                     "content": {"type": "tool_search_tool_result_error", "error_code": "invalid_tool_input"}}
    other = {"type": "server_tool_use", "id": "srvtoolu_3", "name": "web_search", "input": {"query": "x"}}
    assert tool_search.searches([SEARCH, SEARCH_RESULT, failed, failed_result, other, CALL]) == [
        {"query": "lookup_events", "found": ["lookup_events"]},
        {"query": "((", "found": [], "error": "invalid_tool_input"},
    ]


@pytest.mark.parametrize("value, enabled", [(None, True), ("on", True), ("off", False), ("OFF", False),
                                            ("false", False), ("0", False)])
def test_env_switch(monkeypatch, value, enabled):
    from conftest import load_service_module
    load_service_module("services/llm", "tool_search")
    import tool_search
    if value is None:
        monkeypatch.delenv("TOOL_SEARCH", raising=False)
    else:
        monkeypatch.setenv("TOOL_SEARCH", value)
    assert tool_search.enabled_by_env() is enabled


def test_measure_script_questions_use_real_tools(env):  # noqa: F811
    # 측정 질문의 정답 도구가 실제 도구 이름이어야 정확도가 의미 있다 (도구 이름이 바뀌면 여기서 알 수 있다)
    spec = importlib.util.spec_from_file_location("measure_tool_search", ROOT / "scripts" / "measure_tool_search.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    names = {tool["name"] for tool in real_tools(env)}
    for question, expected in script.EVAL_SET:
        assert expected <= names, (question, expected - names)
    # 절반 이상은 처음부터 싣지 않는 도구를 찾아야 답할 수 있는 질문이다 (검색 정확도를 재려고)
    import tool_search
    assert sum(not (expected & set(tool_search.ALWAYS_LOADED)) for _, expected in script.EVAL_SET) > len(
        script.EVAL_SET) / 2
