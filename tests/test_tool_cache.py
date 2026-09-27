"""대화 속도: 저장해 둔 도구 목록으로 모델을 먼저 부르고 MCP 연결은 뒤에서 한다
(services/llm/tool_cache.py, mcp_anthropic_client._prepare_tools·_ensure_connected).
- 저장소: 진행 상황 테이블에 항목 하나로 저장·읽기, 같은 목록은 다시 쓰지 않기, 진행 상황 조회로는 읽을 수 없음
- 클라이언트: 도구가 필요 없는 답은 MCP 연결을 기다리지 않는다. 도구를 부르면 연결을 기다리고, 위험도는 새 목록으로 정한다
실제 API·AWS는 부르지 않는다 (moto, 가짜 응답)."""
import copy
import json
import threading

import boto3
import pytest

from conftest import load_service_module

TABLE = "wga-llm-progress-test"
MCP_URL = "https://abc123.lambda-url.us-east-1.on.aws/"

READ_TOOL = {"name": "describe_log_groups", "description": "로그 그룹", "inputSchema": {"type": "object"},
             "_meta": {"wga/risk": "read"}}


class JsonResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def text_reply(text):
    return {"content": [{"type": "text", "text": text}], "usage": {}, "stop_reason": "end_turn"}


@pytest.fixture
def table(aws):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    return dynamodb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                                 KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
                                 AttributeDefinitions=[{"AttributeName": "requestId", "AttributeType": "S"}])


@pytest.fixture
def cache_module(aws):
    load_service_module("services/llm", "llm_service")
    import tool_cache
    return tool_cache


@pytest.fixture
def client(table, cache_module, monkeypatch):
    import mcp_anthropic_client
    from llm_progress import ProgressReporter
    from redaction import Redactor
    from timing import Stopwatch

    made = mcp_anthropic_client.AnthropicMCPClient(mcp_url=MCP_URL, api_key="k", model_id="claude-sonnet-5",
                                                   tool_cache=cache_module.ToolCache(table, MCP_URL))
    made.tool_search = False
    made.redactor = Redactor()
    made.progress = ProgressReporter()
    made.timer = Stopwatch()
    return made, mcp_anthropic_client


def fake_mcp(made, monkeypatch, tools, gate=None, fail=False):
    """가짜 MCP: 초기화는 gate가 열릴 때까지 걸린다. 부른 도구를 모은다."""
    calls = {"initialize": 0, "call_tool": []}

    def initialize():
        calls["initialize"] += 1
        if gate is not None:
            assert gate.wait(5)
        if fail and calls["initialize"] == 1:
            raise RuntimeError("MCP Lambda 시간 초과")
        made.mcp_client.session_id = "s-1"
        return "s-1"

    def call_tool(name, args, meta=None):
        calls["call_tool"].append((name, meta))
        return {"content": [{"type": "text", "text": "[\"/a\"]"}]}

    monkeypatch.setattr(made.mcp_client, "initialize", initialize)
    monkeypatch.setattr(made.mcp_client, "list_tools", lambda: copy.deepcopy(tools))
    monkeypatch.setattr(made.mcp_client, "call_tool", call_tool)
    return calls


def fake_model(module, monkeypatch, replies):
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return JsonResponse(replies[len(sent) - 1])

    monkeypatch.setattr(module.HTTP, "post", fake_post)
    return sent


# ---------------------------------------------------------------- 저장소

def test_saved_tools_come_back_only_for_the_same_mcp_server(table, cache_module):
    cache_module.ToolCache(table, MCP_URL).save([READ_TOOL], now=1_000)

    assert cache_module.ToolCache(table, MCP_URL).load() == [READ_TOOL]
    assert cache_module.ToolCache(table, "https://other.lambda-url.us-east-1.on.aws/").load() is None
    item = table.get_item(Key={"requestId": cache_module.CACHE_KEY})["Item"]
    assert int(item["expiresAt"]) == 1_000 + cache_module.TTL_SECONDS  # 오래 쓰지 않으면 TTL로 지워진다


def test_same_tools_are_not_written_again_until_a_day_passes(table, cache_module):
    cache = cache_module.ToolCache(table, MCP_URL)
    assert cache.save([READ_TOOL], now=1_000)
    assert not cache.save([READ_TOOL], now=2_000)  # 컨테이너가 뜰 때마다 쓰지 않는다
    assert cache.save([READ_TOOL, {**READ_TOOL, "name": "get_metric_data"}], now=3_000)  # 배포로 도구가 바뀌었다
    assert cache.save([READ_TOOL], now=3_000 + cache_module.REFRESH_SECONDS)  # 하루가 지나면 TTL을 늘린다


def test_a_list_that_is_too_large_is_not_saved(table, cache_module, monkeypatch):
    monkeypatch.setattr(cache_module, "MAX_STORED_BYTES", 10)
    assert not cache_module.ToolCache(table, MCP_URL).save([READ_TOOL])
    assert cache_module.ToolCache(table, MCP_URL).load() is None


def test_storage_problems_fall_back_to_waiting_for_mcp(aws, cache_module):
    missing = boto3.resource("dynamodb", region_name="us-east-1").Table("no-such-table")
    cache = cache_module.ToolCache(missing, MCP_URL)
    assert cache.load() is None and cache.save([READ_TOOL]) is False  # 질문은 실패하지 않는다


def test_progress_api_cannot_read_the_cache_item(table, cache_module):
    from llm_progress import read_progress
    cache_module.ToolCache(table, MCP_URL).save([READ_TOOL])
    assert read_progress(table, cache_module.CACHE_KEY, "alice") is None


# ---------------------------------------------------------------- 클라이언트

def test_answer_without_tools_does_not_wait_for_mcp(client, cache_module, table, monkeypatch):
    made, module = client
    cache_module.ToolCache(table, MCP_URL).save([READ_TOOL])
    gate = threading.Event()  # MCP Lambda가 차가워 초기화가 오래 걸린다
    fresh = [READ_TOOL, {**READ_TOOL, "name": "get_metric_data"}]
    fake_mcp(made, monkeypatch, fresh, gate=gate)
    sent = fake_model(module, monkeypatch, [text_reply("안녕하세요! 무엇을 도와드릴까요?")])

    answer = made.process_user_input("안녕", "시스템")

    assert answer == "안녕하세요! 무엇을 도와드릴까요?"
    assert not gate.is_set()  # MCP 초기화가 끝나기 전에 답했다
    assert [tool["name"] for tool in sent[0]["tools"]] == ["describe_log_groups"]  # 저장해 둔 목록으로 불렀다
    assert "mcp_init" not in made.timer.steps and "mcp_wait" not in made.timer.steps

    gate.set()  # 뒤의 연결이 끝나면 새 목록을 저장한다 (배포로 도구가 늘었다)
    made._ensure_connected()
    assert [tool["name"] for tool in made.tools] == ["describe_log_groups", "get_metric_data"]
    assert [tool["name"] for tool in cache_module.ToolCache(table, MCP_URL).load()] == \
        ["describe_log_groups", "get_metric_data"]


def test_tool_call_waits_for_mcp_and_uses_the_fresh_risk(client, cache_module, table, monkeypatch):
    """저장해 둔 목록에는 조회 도구였지만 새 목록에서는 변경 도구다: 바로 부르지 않고 승인 요청 길로 간다."""
    made, module = client
    cache_module.ToolCache(table, MCP_URL).save([READ_TOOL])
    changed = [{**READ_TOOL, "_meta": {"wga/risk": "write"}}]
    calls = fake_mcp(made, monkeypatch, changed)
    sent = fake_model(module, monkeypatch, [
        {"content": [{"type": "tool_use", "id": "t1", "name": "describe_log_groups", "input": {}}],
         "usage": {}, "stop_reason": "tool_use"},
        text_reply("승인이 필요합니다."),
    ])

    made.process_user_input("로그 그룹 알려줘", "시스템")

    assert calls["initialize"] == 1 and made.timer.steps["mcp_wait"]["n"] == 1
    assert calls["call_tool"] == []  # 승인 요청을 받을 수 없는 경로라 부르지 않고 거절했다
    result = sent[1]["messages"][-1]["content"][0]
    assert "승인 요청" in result["content"] or "요청할 수 없습니다" in result["content"]


def test_read_tool_runs_after_the_connection_finishes(client, cache_module, table, monkeypatch):
    made, module = client
    cache_module.ToolCache(table, MCP_URL).save([READ_TOOL])
    calls = fake_mcp(made, monkeypatch, [READ_TOOL])
    fake_model(module, monkeypatch, [
        {"content": [{"type": "tool_use", "id": "t1", "name": "describe_log_groups", "input": {}}],
         "usage": {}, "stop_reason": "tool_use"},
        text_reply("로그 그룹은 하나입니다."),
    ])

    assert made.process_user_input("로그 그룹 알려줘", "시스템") == "로그 그룹은 하나입니다."
    assert calls["call_tool"] == [("describe_log_groups", None)] and made.mcp_client.session_id == "s-1"


def test_failed_background_connection_is_retried_before_calling_tools(client, cache_module, table, monkeypatch):
    made, module = client
    cache_module.ToolCache(table, MCP_URL).save([READ_TOOL])
    calls = fake_mcp(made, monkeypatch, [READ_TOOL], fail=True)
    fake_model(module, monkeypatch, [
        {"content": [{"type": "tool_use", "id": "t1", "name": "describe_log_groups", "input": {}}],
         "usage": {}, "stop_reason": "tool_use"},
        text_reply("끝"),
    ])

    assert made.process_user_input("로그 그룹 알려줘", "시스템") == "끝"
    assert calls["initialize"] == 2 and made.timer.steps["mcp_init"]["n"] == 1
    assert calls["call_tool"] == [("describe_log_groups", None)]


def test_without_a_saved_list_the_first_question_waits_and_saves(client, cache_module, table, monkeypatch):
    made, module = client
    calls = fake_mcp(made, monkeypatch, [READ_TOOL])
    sent = fake_model(module, monkeypatch, [text_reply("답"), text_reply("또 답")])

    made.process_user_input("로그 그룹 알려줘", "시스템")
    made.process_user_input("또 알려줘", "시스템")

    assert calls["initialize"] == 1 and made.timer.steps["mcp_init"]["n"] == 1  # 처음 한 번만 기다린다
    assert sent[0]["tools"][0]["name"] == "describe_log_groups"
    assert cache_module.ToolCache(table, MCP_URL).load() == [READ_TOOL]  # 다음 새 컨테이너는 기다리지 않는다


def test_get_client_gives_the_anthropic_client_a_tool_cache(aws, table, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "progress_table", table)
    monkeypatch.setattr(llm, "client_cache", {})
    monkeypatch.setattr(llm, "current_model", lambda: {"id": "claude-sonnet-5", "thinking": "adaptive"})

    made = llm.get_client()

    assert made.tool_cache is not None and made.tool_cache.table is table
    assert made.tools == [] and made.mcp_client.session_id is None  # MCP는 첫 질문에서 준비한다
