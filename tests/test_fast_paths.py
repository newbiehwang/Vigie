"""대화 속도 2단계: 인사는 도구·MCP·사고 없이 바로 답하고, 한 응답의 조회 도구는 함께 부른다
(services/llm/llm_service.is_small_talk, mcp_anthropic_client use_tools·_run_tools). 실제 API·AWS는 부르지 않는다."""
import copy
import json
import threading
import time

import pytest

from conftest import load_service_module


class JsonResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


@pytest.fixture
def llm(aws):
    return load_service_module("services/llm", "llm_service")


@pytest.fixture
def client(llm, monkeypatch):
    import mcp_anthropic_client
    from llm_progress import ProgressReporter
    from redaction import Redactor
    from timing import Stopwatch

    made = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                   model_id="claude-sonnet-5",
                                                   thinking={"type": "adaptive", "display": "summarized"})
    made.tool_search = False
    made.redactor = Redactor()
    made.progress = ProgressReporter()
    made.timer = Stopwatch()
    return made, mcp_anthropic_client


# ---------------------------------------------------------------- 인사

@pytest.mark.parametrize("text", ["안녕", "안녕하세요~", "고마워요 ㅎㅎ", "감사합니다.", "Hello!", "thank you", "수고하세요"])
def test_greetings_are_small_talk(llm, text):
    assert llm.is_small_talk(text)


@pytest.mark.parametrize("text", ["안녕, 로그 봐줘", "안녕하세요 비용 알려주세요", "지난주 오류 알려줘", "hi there how much",
                                  "", "안녕" * 20])
def test_questions_are_not_small_talk(llm, text):
    assert not llm.is_small_talk(text)


def test_small_talk_skips_tools_mcp_and_thinking(client, monkeypatch):
    made, module = client
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return JsonResponse({"content": [{"type": "text", "text": "안녕하세요! 무엇을 도와드릴까요?"}],
                             "usage": {"input_tokens": 10, "output_tokens": 12}, "stop_reason": "end_turn"})

    monkeypatch.setattr(module.HTTP, "post", fake_post)
    monkeypatch.setattr(made.mcp_client, "initialize", lambda: pytest.fail("인사에는 MCP를 부르지 않는다"))

    answer = made.process_user_input("안녕", "시스템", use_tools=False)

    assert answer == "안녕하세요! 무엇을 도와드릴까요?"
    assert len(sent) == 1
    payload = sent[0]
    assert "tools" not in payload and "tool_choice" not in payload and "thinking" not in payload
    assert payload["max_tokens"] == module.SMALL_TALK_MAX_TOKENS
    assert "mcp_init" not in made.timer.steps


def test_questions_still_prepare_tools_once(client, monkeypatch):
    made, module = client
    sent, initialized = [], []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return JsonResponse({"content": [{"type": "text", "text": "답"}], "usage": {}, "stop_reason": "end_turn"})

    def initialize():
        initialized.append(True)
        made.tools = [{"name": "describe_log_groups", "description": "", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(module.HTTP, "post", fake_post)
    monkeypatch.setattr(made, "initialize", initialize)

    made.process_user_input("로그 그룹 알려줘", "시스템")
    made.process_user_input("또 알려줘", "시스템")

    assert initialized == [True]  # 도구 목록은 처음 필요할 때 한 번
    assert sent[0]["tools"][0]["name"] == "describe_log_groups" and sent[0]["tool_choice"] == {"type": "auto"}
    assert sent[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert made.timer.steps["mcp_init"]["n"] == 1


def test_handler_routes_greetings_to_the_fast_path(llm, monkeypatch):
    calls = []

    class Client:
        supports_small_talk = True
        model_id = "claude-sonnet-5"

        def process_user_input(self, user_input, system_prompt, **options):
            calls.append((user_input, options))
            return "안녕하세요"

    monkeypatch.setattr(llm, "get_client", lambda *_: Client())
    llm.handle_llm1_with_mcp({"text": "안녕!"}, "https://x", caller_id="alice")
    llm.handle_llm1_with_mcp({"text": "안녕, 비용 알려줘"}, "https://x", caller_id="alice")

    assert calls == [("안녕!", {"use_tools": False}), ("안녕, 비용 알려줘", {})]


def test_clients_without_the_fast_path_get_no_new_argument(llm, monkeypatch):
    calls = []

    class OldClient:  # Bedrock 등
        def process_user_input(self, user_input, system_prompt):
            calls.append(user_input)
            return "hi"

    monkeypatch.setattr(llm, "get_client", lambda *_: OldClient())
    response = llm.handle_llm1_with_mcp({"text": "안녕"}, "https://x", caller_id="alice")
    assert response["statusCode"] == 200 and calls == ["안녕"]


def test_get_client_leaves_tool_setup_to_the_first_question(llm, monkeypatch):
    initialized = []

    class Lazy:
        lazy_tools = True

        def __init__(self, **kwargs):
            self.model_id = kwargs["model_id"]

        def initialize(self):
            initialized.append(True)

    import mcp_anthropic_client
    monkeypatch.setattr(mcp_anthropic_client, "AnthropicMCPClient", Lazy)
    monkeypatch.setattr(llm, "client_cache", {})
    monkeypatch.setattr(llm, "current_model", lambda: {"id": "claude-sonnet-5", "thinking": "adaptive"})
    llm.get_client()
    assert initialized == []


# ---------------------------------------------------------------- 도구를 함께 부르기

def test_read_tools_in_one_response_run_together_and_keep_their_order(client, monkeypatch):
    made, module = client
    made.tools = [{"name": name, "description": "", "inputSchema": {"type": "object"},
                   "_meta": {"wga/risk": "read"}} for name in ("slow_tool", "fast_tool", "broken_tool")]
    sent = []
    replies = [
        {"content": [{"type": "tool_use", "id": "t1", "name": "slow_tool", "input": {}},
                     {"type": "tool_use", "id": "t2", "name": "fast_tool", "input": {}},
                     {"type": "tool_use", "id": "t3", "name": "broken_tool", "input": {}}],
         "usage": {}, "stop_reason": "tool_use"},
        {"content": [{"type": "text", "text": "정리했습니다."}], "usage": {}, "stop_reason": "end_turn"},
    ]

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return JsonResponse(replies[len(sent) - 1])

    threads = set()

    def call_tool(name, args, meta=None):
        threads.add(threading.get_ident())
        if name == "broken_tool":
            raise RuntimeError("MCP 연결 실패")
        time.sleep(0.4 if name == "slow_tool" else 0.05)
        return {"content": [{"type": "text", "text": name}]}

    monkeypatch.setattr(module.HTTP, "post", fake_post)
    monkeypatch.setattr(made, "initialize", lambda: None)
    monkeypatch.setattr(made.mcp_client, "call_tool", call_tool)

    started = time.perf_counter()
    answer = made.process_user_input("셋 다 봐줘", "시스템")
    elapsed = time.perf_counter() - started

    assert answer == "정리했습니다."
    assert elapsed < 0.4 + 0.05 + 0.2  # 하나씩 불렀다면 0.45초 이상. 함께 부르면 가장 느린 것만큼
    assert len(threads) >= 2
    results = sent[1]["messages"][-1]["content"]
    assert [block["tool_use_id"] for block in results] == ["t1", "t2", "t3"]  # 모델에는 부른 차례대로
    assert "slow_tool" in results[0]["content"] and "MCP 연결 실패" in results[2]["content"]
    statuses = {step["id"]: step["status"] for step in made.progress.steps if step["type"] == "tool"}
    assert statuses == {"t1": "ok", "t2": "ok", "t3": "error"}
    assert made.timer.steps["tool:slow_tool"]["n"] == 1
