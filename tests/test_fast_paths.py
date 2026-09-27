"""대화 속도 2단계: 한 응답의 조회 도구는 함께 부른다 (services/llm/mcp_anthropic_client._run_tools).
실제 API·AWS는 부르지 않는다."""
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


# ---------------------------------------------------------------- 도구를 함께 부르기

def test_read_tools_in_one_response_run_together_and_keep_their_order(client, monkeypatch):
    made, module = client
    made.tools = [{"name": name, "description": "", "inputSchema": {"type": "object"},
                   "_meta": {"vigie/risk": "read"}} for name in ("slow_tool", "fast_tool", "broken_tool")]
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
