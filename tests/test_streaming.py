"""대화 속도 2단계: Messages API 스트리밍 (services/llm/mcp_anthropic_client.py _read_stream, llm_progress.thinking_live).
- 스트리밍 이벤트를 스트리밍하지 않은 응답과 같은 모양의 메시지로 다시 조립한다 (도구 루프는 그대로)
- 사고 요약은 받는 대로 진행 상황에 보인다 (가린 뒤). 다 받으면 한 단계로 마무리하고 두 번 넣지 않는다
- 스트림 도중의 오류 이벤트는 API 오류로 끝낸다
실제 Anthropic API는 부르지 않는다 (가짜 SSE 응답)."""
import copy
import json

import pytest

from conftest import load_service_module

ACCOUNT = "123456789012"


class StreamResponse:
    """requests의 스트리밍 응답 흉내: 머리 + SSE 줄."""
    status_code = 200
    headers = {"content-type": "text/event-stream; charset=utf-8"}

    def __init__(self, events):
        self._lines = []
        for event in events:
            self._lines += [f"event: {event['type']}", f"data: {json.dumps(event, ensure_ascii=False)}", ""]
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        self.closed = True


def sse_message(blocks, stop_reason="end_turn", usage=None):
    """블록 목록 → 스트리밍 이벤트 (글자·사고·도구 입력을 조각내어 보낸다)."""
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 40}}}]
    for index, block in enumerate(blocks):
        kind = block["type"]
        if kind == "text":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            for piece in (block["text"][:3], block["text"][3:]):
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "text_delta", "text": piece}})
        elif kind == "thinking":
            events.append({"type": "content_block_start", "index": index,
                           "content_block": {"type": "thinking", "thinking": ""}})
            for piece in block["pieces"]:
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "thinking_delta", "thinking": piece}})
            events.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "signature_delta", "signature": "sig-abc"}})
        elif kind == "tool_use":
            events.append({"type": "content_block_start", "index": index,
                           "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            raw = json.dumps(block["input"])
            for piece in (raw[:5], raw[5:]):
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "input_json_delta", "partial_json": piece}})
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason},
                   "usage": usage or {"output_tokens": 55}})
    events.append({"type": "message_stop"})
    return events


@pytest.fixture
def client(aws):
    load_service_module("services/llm", "llm_service")
    import mcp_anthropic_client
    from llm_progress import ProgressReporter
    from redaction import Redactor
    from timing import Stopwatch

    made = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                   model_id="claude-sonnet-5")
    made.tools = [{"name": "describe_log_groups", "description": "로그 그룹", "inputSchema": {"type": "object"},
                   "_meta": {"vigie/risk": "read", "vigie/access": "all"}}]
    made.tool_search = False
    made.redactor = Redactor([ACCOUNT])
    made.progress = ProgressReporter()  # 표 없이 단계만 모은다
    made.timer = Stopwatch()
    return made, mcp_anthropic_client


def test_stream_is_rebuilt_into_the_same_message_shape(client):
    made, module = client
    response = StreamResponse(sse_message([
        {"type": "thinking", "pieces": ["로그를 ", "봐야 한다."]},
        {"type": "text", "text": "확인해 보겠습니다."},
        {"type": "tool_use", "id": "toolu_1", "name": "describe_log_groups", "input": {"prefix": "/aws/lambda"}},
    ], stop_reason="tool_use"))

    message, streamed = made._read_message(response, started=0.0)

    assert streamed and response.closed
    assert message["content"] == [
        {"type": "thinking", "thinking": "로그를 봐야 한다.", "signature": "sig-abc"},
        {"type": "text", "text": "확인해 보겠습니다."},
        {"type": "tool_use", "id": "toolu_1", "name": "describe_log_groups", "input": {"prefix": "/aws/lambda"}},
    ]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 100, "cache_read_input_tokens": 40, "output_tokens": 55}
    assert made.timer.steps["model_first_token"]["n"] == 1


def test_thinking_shows_up_while_streaming_and_is_redacted(client, monkeypatch):
    made, module = client
    import llm_progress
    monkeypatch.setattr(llm_progress, "LIVE_SAVE_SECONDS", 0)  # 조각마다 저장해 중간 모습을 본다
    saved = []

    class Table:
        def put_item(self, Item, **kwargs):
            saved.append(copy.deepcopy(Item["steps"]))

    made.progress = llm_progress.ProgressReporter(Table(), "0f8fad5b-d9cb-469f-a165-70867728950e", "alice")
    response = StreamResponse(sse_message([
        {"type": "thinking", "pieces": ["계정 ", f"{ACCOUNT}의 ", "로그를 본다."]},
        {"type": "text", "text": "끝났습니다."},
    ]))

    made._read_message(response, started=0.0)

    live = [steps[-1] for steps in saved if steps and steps[-1].get("live")]
    assert [step["text"] for step in live][:2] == ["계정", "계정 ********9012의"]  # 받는 대로, 계정 ID는 가려서
    final = made.progress.steps
    assert len(final) == 1 and final[0] == {"type": "thinking", "text": "계정 ********9012의 로그를 본다."}
    assert ACCOUNT not in json.dumps(saved, ensure_ascii=False)


def test_loop_reports_each_thought_once_and_runs_tools(client, monkeypatch):
    made, module = client
    sent = []
    replies = [
        sse_message([{"type": "thinking", "pieces": ["그룹을 찾자."]},
                     {"type": "tool_use", "id": "toolu_1", "name": "describe_log_groups", "input": {}}],
                    stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "로그 그룹은 두 개입니다."}]),
    ]

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append({"payload": copy.deepcopy(json), "kwargs": kwargs})
        return StreamResponse(replies[len(sent) - 1])

    monkeypatch.setattr(module.HTTP, "post", fake_post)
    monkeypatch.setattr(made.mcp_client, "call_tool",
                        lambda name, args, meta=None: {"content": [{"type": "text", "text": "[\"/a\", \"/b\"]"}]})

    answer = made.process_user_input("로그 그룹 알려줘")

    assert answer == "로그 그룹은 두 개입니다."
    assert all(call["payload"]["stream"] is True and call["kwargs"]["stream"] is True for call in sent)
    # 두 번째 요청에 사고 블록(서명 포함)을 받은 그대로 돌려보낸다
    assistant = sent[1]["payload"]["messages"][1]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "그룹을 찾자.", "signature": "sig-abc"}
    kinds = [step["type"] for step in made.progress.steps]
    assert kinds == ["thinking", "tool"]  # 사고는 한 번만 (스트리밍 중에 알렸고, 끝나고 다시 넣지 않는다)
    assert made.timer.steps["model"]["n"] == 2 and made.timer.steps["model_first_token"]["n"] == 2


def test_error_event_in_the_middle_of_a_stream_ends_as_api_error(client, monkeypatch):
    made, module = client
    events = sse_message([{"type": "text", "text": "시작합니다"}])[:3]
    events.append({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    monkeypatch.setattr(module.HTTP, "post", lambda *args, **kwargs: StreamResponse(events))

    answer = made.process_user_input("안녕")

    assert answer.startswith("Anthropic API 오류 (응답을 받는 중): overloaded_error: Overloaded")


def test_non_streamed_json_response_still_works(client):
    made, module = client

    class JsonResponse:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": "hi"}], "usage": {}, "stop_reason": "end_turn"}

    message, streamed = made._read_message(JsonResponse(), started=0.0)
    assert message["content"][0]["text"] == "hi" and not streamed


def test_live_step_is_not_saved_into_the_conversation(client):
    made, module = client
    progress = made.progress
    progress.thinking_live("생각 중")
    assert progress.steps[-1]["live"] is True
    progress.thought("")  # 다 받은 글이 비었으면 받은 만큼으로 마무리한다
    assert progress.steps == [{"type": "thinking", "text": "생각 중"}]
    assert all("live" not in step for step in progress.saved_steps())
