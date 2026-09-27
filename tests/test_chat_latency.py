"""대화 속도 1단계 (services/llm): 단계별 시간 기록, 대화 기록 제한, 시스템 프롬프트 캐시 블록."""
import json
from datetime import datetime, timezone

import pytest

from conftest import load_service_module


@pytest.fixture
def llm(aws):
    return load_service_module("services/llm", "llm_service")


def test_stopwatch_sums_steps_with_the_same_name(capsys):
    timing = load_service_module("services/llm", "timing")
    timer = timing.Stopwatch()
    timer.add("model", 1200)
    timer.add("model", 800.7)
    with timer.step("history"):
        pass
    timer.log(ok=True)

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["timing"]["steps"]["model"] == {"ms": 2000, "n": 2}
    assert line["timing"]["steps"]["history"]["n"] == 1 and line["timing"]["ok"] is True
    assert isinstance(line["timing"]["totalMs"], int)


def test_stopwatch_off_records_nothing(capsys):
    timing = load_service_module("services/llm", "timing")
    timer = timing.Stopwatch.off()
    with timer.step("model"):
        pass
    timer.log()
    assert timer.steps == {} and capsys.readouterr().out == ""


def test_history_keeps_recent_turns_starting_with_a_question(llm):
    messages = [{"role": "user" if n % 2 == 0 else "assistant", "content": str(n)} for n in range(31)]
    recent = llm.recent_history(messages)
    assert len(recent) <= llm.MAX_HISTORY_MESSAGES
    assert recent[0]["role"] == "user" and recent[-1]["content"] == "30"
    assert llm.recent_history(messages[:5]) == messages[:5]  # 짧으면 그대로


def test_system_prompt_blocks_keep_the_time_out_of_the_cached_part(llm):
    system_prompt = load_service_module("services/llm", "system_prompt")
    now = datetime(2026, 9, 27, 5, 6, 7, tzinfo=timezone.utc)
    later = datetime(2026, 9, 27, 9, 0, 0, tzinfo=timezone.utc)

    blocks, again = system_prompt.build_system_blocks(now), system_prompt.build_system_blocks(later)

    # 본문은 시각이 달라도 글자 하나까지 같아야 캐시가 맞는다
    assert blocks[0] == again[0] and blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "2026-09-27 05:06:07" in blocks[1]["text"] and "cache_control" not in blocks[1]
    assert "2026" not in blocks[0]["text"]
    # 측정 스크립트용 한 덩어리에도 본문과 시각이 모두 있다
    assert system_prompt.build_system_prompt(now) == blocks[0]["text"] + "\n" + blocks[1]["text"]


def test_tool_search_hint_goes_into_the_cached_block(llm):
    import mcp_anthropic_client
    import tool_search
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tool_search = True
    blocks = [{"type": "text", "text": "본문", "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": "시각"}]

    sent = client._system_prompt(blocks)

    assert sent[0]["text"] == "본문" + tool_search.SYSTEM_HINT and sent[1]["text"] == "시각"
    assert blocks[0]["text"] == "본문"  # 받은 목록은 바꾸지 않는다
