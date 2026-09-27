"""감사 로그의 층(locus)과 체류 신호(taintedBy) (services/llm/audit.py, approvals.py, mcp_anthropic_client.py)

사고가 났을 때 "어디가 뚫렸나"를 층 하나씩 좁히려고 기록마다 도구 반복 위의 자리를 적는다
(fingate-x의 '원인의 계층'을 참고). 판단은 바꾸지 않는다: 변경은 원래 모두 승인이 필요하다.

- 경계(interface): 위험도 등록부에 없는 도구 / 유입(ingress): 도구 결과 / 유출(egress): 승인 요청 /
  효과(effect): 승인·거절·실행·실패
- 체류(residence): 의심 결과를 읽은 뒤 같은 질문에서 나온 변경 요청. 승인 카드·감사 로그에 '먼저 읽은 의심 결과'로 보인다
- 같은 응답에서 함께 부른 도구의 결과는 모델이 아직 보지 못했으므로 체류 신호가 아니다
"""
import copy
import json

import pytest

from test_approvals import LOG_GROUP, ORIGIN, FakeResponse, decide, env, retention  # noqa: F401 (env는 fixture)
from test_injection import ATTACK_KO

LOG_TOOL = "get_logs_insight_query_results"
CLEAN_LOG = "2026-09-25 ERROR Task timed out after 3.00 seconds"


def log_call(tool_id):
    return {"type": "tool_use", "id": tool_id, "name": LOG_TOOL, "input": {"query_id": "q-1"}}


def write_call(tool_id, days=1):
    return {"type": "tool_use", "id": tool_id, "name": "setLogRetention",
            "input": {"log_group_name": LOG_GROUP, "retention_days": days}}


def run(env, monkeypatch, replies, log_text=ATTACK_KO):
    """모델 응답(replies)을 차례로 돌려주며 질문 하나를 처리한다. 로그 도구는 log_text를 돌려준다."""
    llm = env["llm"]
    import mcp_anthropic_client
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse({"usage": {}, **replies[len(sent) - 1]})

    def call_tool(name, args=None, meta=None):
        if name == LOG_TOOL:
            return {"content": [{"type": "text", "text": json.dumps(
                {"results": [[{"field": "@message", "value": log_text}]]}, ensure_ascii=False)}]}
        return env["mcp"].call_tool(name, args, meta)

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    monkeypatch.setattr(client.mcp_client, "call_tool", call_tool)
    monkeypatch.setattr(llm, "get_client", lambda *_: client)
    return json.loads(llm.handle_llm1_with_mcp({"text": "최근 오류 로그 보여줘"}, ORIGIN, caller_id="alice")["body"])


def audit_items(env):
    return env["audit"].query(KeyConditionExpression="userId = :u", ExpressionAttributeValues={":u": "alice"})["Items"]


def query(env, **params):
    from audit import query_audit
    return query_audit(env["audit"], "carol", {"sub": "carol", "cognito:groups": "admins"},
                       {"scope": "all", **params})["items"]


# ---------------------------------------------------------------- 체류 신호

def test_change_requested_after_reading_an_injected_log_is_flagged(env, monkeypatch):
    body = run(env, monkeypatch, [
        {"content": [log_call("toolu_1")]},
        {"content": [write_call("toolu_2")]},
        {"content": [{"type": "text", "text": "오류 로그를 확인했습니다."}]},
    ])

    # 승인 카드: 이 요청 전에 읽은 의심 결과와 그 뒤로 몇 번째 호출인지
    [pending] = body["inference"]["pendingActions"]
    [tainted] = pending["taintedBy"]
    assert tainted["toolUseId"] == "toolu_1" and tainted["tool"] == LOG_TOOL and tainted["callsAgo"] == 1
    assert {"ignore_instructions_ko", "change_command_ko"} <= set(tainted["kinds"])
    # GET /actions/{id}도 같은 값 (승인자가 나중에 열어 볼 때)
    status, view = decide(env, pending["actionId"], "get", "alice")
    assert status == 200 and view["taintedBy"] == pending["taintedBy"]

    # 감사 로그: 조회 결과는 유입, 변경 도구 호출과 승인 요청은 유출이고 요청 행에 체류 신호가 있다
    items = audit_items(env)
    log_row = next(i for i in items if i.get("tool") == LOG_TOOL)
    write_row = next(i for i in items if i["kind"] == "tool" and i.get("tool") == "setLogRetention")
    requested = next(i for i in items if i.get("event") == "requested")
    assert log_row["locus"] == "ingress" and log_row["injectionSuspected"]
    assert write_row["locus"] == "egress"
    assert requested["locus"] == "egress" and requested["taintedBy"][0]["toolUseId"] == "toolu_1"
    assert "locus" not in next(i for i in items if i["kind"] == "request")  # 질문 행은 요약이라 층이 없다

    # 판단은 그대로: 승인자가 승인해야 실행되고, 결정·실행 행은 효과층이며 체류 신호를 되풀이하지 않는다
    assert retention() == 30
    status, done = decide(env, pending["actionId"], "approve", "alice", groups="approvers")
    assert status == 200 and done["status"] == "executed" and retention() == 1
    effects = [i for i in audit_items(env) if i.get("event") in ("approved", "executed")]
    assert len(effects) == 2 and all(i["locus"] == "effect" and "taintedBy" not in i for i in effects)

    # 관리자는 층으로 거른다: 체류는 의심 결과 뒤의 승인 요청만
    assert [i["event"] for i in query(env, locus="residence")] == ["requested"]
    assert {i["tool"] for i in query(env, locus="ingress")} == {LOG_TOOL}
    assert sorted(i["event"] for i in query(env, locus="effect")) == ["approved", "executed"]


def test_clean_log_leaves_no_residence_signal(env, monkeypatch):
    body = run(env, monkeypatch, [
        {"content": [log_call("toolu_1")]},
        {"content": [write_call("toolu_2", days=14)]},
        {"content": [{"type": "text", "text": "보존 기간 변경을 요청했습니다."}]},
    ], log_text=CLEAN_LOG)
    [pending] = body["inference"]["pendingActions"]
    assert "taintedBy" not in pending
    requested = next(i for i in audit_items(env) if i.get("event") == "requested")
    assert "taintedBy" not in requested and query(env, locus="residence") == []


def test_results_from_the_same_response_are_not_counted(env, monkeypatch):
    # 로그 조회와 변경을 한 응답에서 함께 불렀다: 모델은 로그 결과를 보기 전에 변경을 정했다
    body = run(env, monkeypatch, [
        {"content": [log_call("toolu_1"), write_call("toolu_2")]},
        {"content": [{"type": "text", "text": "확인했습니다."}]},
    ])
    [pending] = body["inference"]["pendingActions"]
    assert "taintedBy" not in pending


def test_signal_counts_calls_in_between_and_resets_per_question(env, monkeypatch):
    body = run(env, monkeypatch, [
        {"content": [log_call("toolu_1")]},
        {"content": [{"type": "tool_use", "id": "toolu_2", "name": "get_active_alarms", "input": {}}]},
        {"content": [write_call("toolu_3")]},
        {"content": [{"type": "text", "text": "확인했습니다."}]},
    ])
    [pending] = body["inference"]["pendingActions"]
    assert [t["callsAgo"] for t in pending["taintedBy"]] == [2]

    # 다음 질문은 새로 센다 (같은 클라이언트를 모델별로 캐시해 여러 질문이 함께 쓴다)
    body = run(env, monkeypatch, [
        {"content": [write_call("toolu_4", days=14)]},
        {"content": [{"type": "text", "text": "요청했습니다."}]},
    ], log_text=CLEAN_LOG)
    [pending] = body["inference"]["pendingActions"]
    assert "taintedBy" not in pending


# ---------------------------------------------------------------- 경계층·조회

def test_unregistered_tool_is_recorded_at_the_interface(env):
    import audit
    from redaction import Redactor
    assert env["approvals"].is_registered({"name": "x", "_meta": {"wga/risk": "read"}})
    assert not env["approvals"].is_registered({"name": "x"}) and not env["approvals"].is_registered(None)

    log = audit.AuditLog(env["audit"], None, Redactor(), user_id="alice", email=None, source="web",
                         request_id="r-1", session_id=None, model_id="m", question="q")
    log.tool_started("toolu_1", "brand_new_tool", {}, True, "interface")
    log.tool_finished("toolu_1", False, "실행하지 않았습니다")
    [row] = [i for i in audit_items(env) if i.get("tool") == "brand_new_tool"]
    assert row["locus"] == "interface"


def test_every_listed_tool_is_registered(env):
    # 지금 목록의 도구는 모두 등록부에 있다: 경계층 행은 공식 서버를 올려 새 도구가 생겼을 때만 나온다
    tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    assert tools and all(env["approvals"].is_registered(tool) for tool in tools)


def test_unknown_locus_is_rejected(env):
    from audit import AuditQueryError
    with pytest.raises(AuditQueryError) as error:
        query(env, locus="deliberation")  # 판단층은 기록할 수 없다
    assert error.value.status == 400
