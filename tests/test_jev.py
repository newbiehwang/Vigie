"""Jev로 질문을 미리 분류하기 (services/llm/jev.py): 도구 라우팅과 권한 사전 안내. 기본 꺼짐.

실제 API는 부르지 않는다. 가짜 전송(transport)이 받은 요청을 남기고 정해 둔 확률을 돌려준다.
- 라우팅: 기준 이상인 도구를 확률 차례로 시스템 프롬프트 끝에 적는다. 도구 목록(처음부터 싣는 도구)은 바꾸지 않는다
- 권한 안내: 일반 사용자의 질문이 관리자 도구 없이는 답할 수 없어 보이면 모델을 부르지 않고 안내한다. 관리자는 안내하지 않는다
- 막는 것은 여전히 코드다: Jev가 무엇을 골라도 일반 사용자에게 관리자 전용 도구는 보이지 않고 불러도 거절된다
- 실패(오류 · 모양이 다른 답 · 키 없음 · 꺼짐)하면 아무것도 바뀌지 않는다
- 밖으로는 가린 질문과 볼 수 있는 도구의 이름 · 설명 첫 문장만 나가고, 모델 버전은 고정한다
"""
import json

import pytest

from test_approvals import FakeResponse, env  # noqa: F401 (env는 fixture)
from test_audit_locus import CLEAN_LOG, LOG_TOOL, log_call, run

ACCOUNT_ID = "123456789012"
ADMIN_TOOL = "lookup_events"  # 관리자 전용 (mcp/lambda_mcp/risk.py ADMIN_ONLY)


class FakeTransport:
    """받은 요청을 남기고, 도구 이름 → 확률과 관리자 확률로 답한다."""

    def __init__(self, probabilities=None, admin=0.0, error=None, answers=None):
        self.probabilities = probabilities or {}
        self.admin = admin
        self.error = error
        self.answers = answers
        self.sent = []

    def __call__(self, payload):
        self.sent.append(json.loads(json.dumps(payload)))
        if self.error:
            raise self.error
        if self.answers is not None:
            return {"answers": self.answers}
        answers = {}
        for name, question in payload["questions"].items():
            if name == "admin":
                answers[name] = {"noul": self.admin}
                continue
            tool = question["instructions"].split("the tool ", 1)[1].split(":", 1)[0]
            answers[name] = {"noul": self.probabilities.get(tool, 0.0)}
        return {"answers": answers, "model": payload["model"]}


def tools_of(env):
    return json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]


def plan(env, transport, role="member", question="최근 오류 로그 보여줘"):
    import jev
    return jev.Jev(transport).plan(question, tools_of(env), role)


# ---------------------------------------------------------------- 분류 (순수)
def test_routes_likely_tools_in_order_of_probability(env):
    import jev
    transport = FakeTransport({LOG_TOOL: 0.7, "describe_log_groups": 0.95, "getEc2CpuRanking": 0.2})
    result = plan(env, transport)
    assert result.routed == ["describe_log_groups", LOG_TOOL]  # 기준(0.5) 이상만, 확률이 높은 차례
    assert LOG_TOOL in result.note() and "not an instruction" in result.note()
    assert transport.sent[0]["model"] == jev.MODEL == "jev-1.13.0"  # 움직이는 이름(jev-latest)을 쓰지 않는다


def test_routing_is_capped(env):
    import jev
    names = [tool["name"] for tool in tools_of(env) if tool["name"] != ADMIN_TOOL][:20]
    result = plan(env, FakeTransport({name: 0.9 for name in names}))
    assert len(result.routed) == jev.MAX_ROUTED


def test_member_is_never_routed_to_admin_only_tools(env):
    """일반 사용자에게는 관리자 전용 도구를 묻지도 않는다: 볼 수 있는 도구만 질문이 된다."""
    transport = FakeTransport({ADMIN_TOOL: 0.99, LOG_TOOL: 0.8})
    result = plan(env, transport, role="member")
    asked = " ".join(q["instructions"] for q in transport.sent[0]["questions"].values())
    assert ADMIN_TOOL not in asked and ADMIN_TOOL not in result.routed
    assert result.routed == [LOG_TOOL]
    admin_result = plan(env, FakeTransport({ADMIN_TOOL: 0.99}), role="admin")
    assert admin_result.routed == [ADMIN_TOOL] and admin_result.admin is None  # 관리자에게는 권한을 묻지 않는다


def test_only_tool_names_and_first_sentences_go_out(env):
    import jev
    transport = FakeTransport()
    plan(env, transport)
    questions = transport.sent[0]["questions"]
    assert "admin" in questions
    assert all(len(q["instructions"]) < 400 for q in questions.values())  # 설명 전체(수천 자)가 아니다
    assert len(jev._summary("첫 문장. 둘째 문장")) <= jev.DESCRIPTION_CHARS


@pytest.mark.parametrize("transport", [
    FakeTransport(error=TimeoutError("느림")),
    FakeTransport(answers="모양이 다름"),
])
def test_failure_changes_nothing(env, transport):
    assert plan(env, transport) is None


def test_odd_answers_are_ignored(env):
    result = plan(env, FakeTransport(answers={"tool_0": {"noul": 1.7}, "tool_1": "높음", "admin": True}))
    assert result.routed == [] and result.admin is None


def test_off_by_default_and_without_a_key(monkeypatch):
    import jev
    monkeypatch.delenv("JEV", raising=False)
    jev._cached.clear()
    assert jev.current(lambda: "sk-test") is None
    monkeypatch.setenv("JEV", "on")
    assert jev.current(lambda: None) is None
    jev._cached.clear()


# ---------------------------------------------------------------- 질문 하나를 처리할 때
def use(monkeypatch, transport):
    import jev
    monkeypatch.setattr(jev, "current", lambda get_key: jev.Jev(transport))


def test_member_question_that_needs_admin_tools_gets_guidance_without_the_model(env, monkeypatch):
    import jev
    transport = FakeTransport(admin=0.95)
    use(monkeypatch, transport)
    body = run(env, monkeypatch, [], question="어제 누가 보안 그룹을 열었어?")  # 모델 응답이 없다: 부르면 실패한다
    assert body["answer"] == jev.ADMIN_GUIDE and len(transport.sent) == 1
    assert body["inference"]["tools_used"] == []  # 도구도 부르지 않았다


def test_low_admin_probability_runs_the_model_with_a_routing_note(env, monkeypatch):
    transport = FakeTransport({LOG_TOOL: 0.8}, admin=0.6)  # 기준(0.9) 아래: 안내하지 않는다
    use(monkeypatch, transport)
    body = run(env, monkeypatch, [{"content": [log_call("toolu_1")]},
                                  {"content": [{"type": "text", "text": "오류가 없습니다."}]}], log_text=CLEAN_LOG)
    assert body["answer"] == "오류가 없습니다."


def test_routing_note_goes_after_the_cache_breakpoint_and_tools_list_is_unchanged(env, monkeypatch):
    """라우팅은 마지막 시스템 블록에만 적고, 도구 목록은 Jev가 없을 때와 같다 (프롬프트 캐시가 빗나가지 않게)."""
    import copy
    import mcp_anthropic_client
    from test_approvals import FakeResponse as Reply

    def payloads(transport):
        if transport:
            use(monkeypatch, transport)
        else:
            import jev
            monkeypatch.setattr(jev, "current", lambda get_key: None)
        sent = []

        def fake_post(url, headers=None, json=None, **kwargs):
            sent.append(copy.deepcopy(json))
            return Reply({"usage": {}, "content": [{"type": "text", "text": "네."}]})

        client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                         model_id="claude-sonnet-5")
        client.tools = tools_of(env)
        monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
        monkeypatch.setattr(env["llm"], "get_client", lambda *_: client)
        env["llm"].handle_llm1_with_mcp({"text": "최근 오류 로그 보여줘"}, "https://example.com", caller_id="alice")
        return sent[0]

    plain = payloads(None)
    routed = payloads(FakeTransport({LOG_TOOL: 0.8}))
    assert [t["name"] for t in routed["tools"]] == [t["name"] for t in plain["tools"]]
    assert routed["system"][:-1] == plain["system"]  # 캐시되는 앞부분은 같다
    assert "<Routing>" in routed["system"][-1]["text"] and LOG_TOOL in routed["system"][-1]["text"]


def test_jev_sees_only_the_redacted_question(env, monkeypatch):
    transport = FakeTransport()
    use(monkeypatch, transport)
    # Claude에 보내는 질문과 같은 가린 글이다 (redaction.py가 계정 ID로 알아보는 표기)
    run(env, monkeypatch, [{"content": [{"type": "text", "text": "네."}]}], question=f"account id {ACCOUNT_ID} 비용 알려줘")
    assert transport.sent and ACCOUNT_ID not in transport.sent[0]["state"] and "비용 알려줘" in transport.sent[0]["state"]


def test_admin_is_not_guided_even_if_jev_says_so(env, monkeypatch):
    """관리자 요청에는 권한 물음 자체를 보내지 않는다."""
    import jev
    transport = FakeTransport(admin=1.0)
    result = jev.Jev(transport).plan("어제 누가 보안 그룹을 열었어?", tools_of(env), "admin")
    assert "admin" not in transport.sent[0]["questions"] and not result.needs_admin()
