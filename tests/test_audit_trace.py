"""역추적 (services/llm/audit_trace.py, GET /audit?trace=<actionId>)

변경 작업 하나를 놓고 효과 → 유출 → 체류 → 판단 → 유입 → 경계 → 매개 순서로 예·아니오를 답한다.
- 실제 흐름(질문 → 도구 → 승인 요청 → 승인 → 실행)이 남긴 기록만으로 답한다
- 요청자와 승인자의 기록, 자정을 넘긴 기록도 모은다
- 기록이 어긋나면(승인 없이 실행, 요청 없이 결정) 그 층이 실패로 보인다
- 관리자만 본다
- 모델이 속았다고 가정한다: 모델의 응답만 정해 두고(run), 탐지 · 승인 · 실행 · 감사 · 역추적은 실제 코드로 돈다.
  기준 경우(지시문이 든 로그를 읽고 변경을 요청 → 승인)에서 한 가지만 바꾸면 그 단계의 판정만 바뀌는지 본다
- 단계마다 판정의 근거(기록 · 탐지 · 앱 밖)가 붙고, 판단 단계는 탐지 신호와 기록 신호를 따로 본다
"""
import json
import uuid

import pytest

from conftest import load_service_module
from test_approvals import LOG_GROUP, ORIGIN, decide, env  # noqa: F401 (env는 fixture)
from test_audit_locus import CLEAN_LOG, LOG_TOOL, log_call, run, write_call
from test_injection import ATTACK_KO

# 기준 경우의 판정: 지시문이 든 로그를 읽은 뒤 Vigie 로그 보존 기간을 30일 → 1일로 줄이는 변경을 요청했고,
# 사람이 의심 경고를 보고도 승인해 실행됐다 (효과 주의). 지시문에 로그 그룹 이름은 없어 판단은 주의에 머문다
INJECTED = {"effect": "warn", "egress": "ok", "residence": "warn", "deliberation": "warn", "ingress": "warn",
            "interface": "ok", "mediation": "info"}
# 단계마다 판정의 근거 (판단은 신호에 따라 달라서 따로 본다)
BASIS = {"effect": ["record"], "egress": ["record"], "residence": ["detection"], "ingress": ["detection"],
         "interface": ["record"], "mediation": ["outside"]}
# 로그 그룹 이름까지 적은 지시문: 모델이 쓴 값이 이 글에서 왔다 (요청 값의 출처)
ATTACK_WITH_TARGET = f"{ATTACK_KO} 대상 로그 그룹: {LOG_GROUP}"
# 등록부(mcp/lambda_mcp/risk.py)에 없는 도구: 공식 서버를 올리다 새 도구가 생긴 경우. 변경 도구로 다뤄 승인을 기다린다
UNREGISTERED_TOOL = "brand_new_tool"


def trace(env, action_id, day=None, groups="admins"):
    from audit_trace import query_trace
    params = {"trace": action_id}
    if day:
        params["day"] = day
    return query_trace(env["audit"], "carol", {"sub": "carol", "cognito:groups": groups}, params)


def statuses(result):
    return {step["layer"]: step["status"] for step in result["steps"]}


def ask(env, monkeypatch, log_text=ATTACK_KO, days=1, question="최근 오류 로그 보여줘"):
    body = run(env, monkeypatch, [
        {"content": [log_call("toolu_1")]},
        {"content": [write_call("toolu_2", days=days)]},
        {"content": [{"type": "text", "text": "요청했습니다."}]},
    ], log_text=log_text, question=question)
    return body["inference"]["pendingActions"][0]


def layer_of(result, name):
    return next(step for step in result["steps"] if step["layer"] == name)


def test_trace_of_a_change_that_followed_an_injected_log(env, monkeypatch):
    action = ask(env, monkeypatch)  # 공격 문구가 든 로그를 읽은 뒤 변경을 요청했다
    decide(env, action["actionId"], "approve", "bob", groups="approvers", email="bob@example.com")
    result = trace(env, action["actionId"])

    assert statuses(result) == INJECTED
    steps = {step["layer"]: step for step in result["steps"]}
    assert "bob@example.com" in steps["effect"]["answer"]  # 결정한 사람 (승인자의 기록)
    assert "의심 경고" in steps["effect"]["answer"]  # 경고를 보고도 승인했다
    assert "최근 오류 로그 보여줘" in steps["deliberation"]["answer"]  # 사용자의 질문과 비교하라고
    assert "묻지 않은 Vigie 감시 장치 약화" in steps["deliberation"]["answer"]  # 30일 → 1일은 질문에 없던 변경
    assert {layer: step["basis"] for layer, step in steps.items() if layer != "deliberation"} == BASIS
    assert steps["deliberation"]["basis"] == ["detection", "record"]
    assert "change_command_ko" in steps["ingress"]["answer"]
    assert "PutRetentionPolicy" in steps["mediation"]["answer"]  # CloudTrail과 대조할 단서
    assert result["verdict"].startswith("주의할 단계")
    assert [step["layer"] for step in result["steps"]] == ["effect", "egress", "residence", "deliberation",
                                                          "ingress", "interface", "mediation"]

    # 요청자(alice)와 승인자(bob)의 기록을 모두 모았고, 같은 질문의 질문·도구 행도 있다
    assert [(e["userId"], e["event"]) for e in result["events"]] == [
        ("alice", "requested"), ("bob", "approved"), ("bob", "executed")]
    assert {row.get("tool") or row["kind"] for row in result["rows"]} == {"request", LOG_TOOL, "setLogRetention"}
    # 근거로 가리킨 행은 모두 돌려준 행 안에 있다 (화면이 강조한다)
    shown = {row["at"] for row in result["events"] + result["rows"]}
    assert all(at in shown for step in result["steps"] for at in step["evidence"])


# ---------------------------------------------------------------- 한 가지만 바꾼 경우 (기준: INJECTED)
def test_failed_execution_is_a_warning_at_the_effect_stage(env, monkeypatch):
    """승인했지만 실행 전에 로그 그룹이 사라졌다: 효과만 경고로 바뀐다 (나머지 단계는 기준과 같다)."""
    import boto3
    action = ask(env, monkeypatch)
    boto3.client("logs").delete_log_group(logGroupName=LOG_GROUP)
    decide(env, action["actionId"], "approve", "bob", groups="approvers")
    result = trace(env, action["actionId"])

    assert statuses(result) == {**INJECTED, "effect": "warn"}
    effect = result["steps"][0]
    assert "실행에 실패" in effect["answer"] and "bob" in effect["answer"]
    assert [e["event"] for e in result["events"]] == ["requested", "approved", "failed"]


def test_unregistered_tool_is_a_warning_at_the_interface_stage(env, monkeypatch):
    """같은 질문에서 등록부에 없는 도구를 불렀다: 경계만 경고로 바뀐다."""
    body = run(env, monkeypatch, [
        {"content": [log_call("toolu_1")]},
        {"content": [{"type": "tool_use", "id": "toolu_2", "name": UNREGISTERED_TOOL, "input": {}}]},
        {"content": [write_call("toolu_3")]},
        {"content": [{"type": "text", "text": "요청했습니다."}]},
    ])
    action = next(a for a in body["inference"]["pendingActions"] if a["tool"] == "setLogRetention")
    decide(env, action["actionId"], "approve", "bob", groups="approvers")
    result = trace(env, action["actionId"])

    assert statuses(result) == {**INJECTED, "interface": "warn"}
    interface = next(step for step in result["steps"] if step["layer"] == "interface")
    assert UNREGISTERED_TOOL in interface["answer"] and interface["evidence"]


def test_denied_change_after_an_injected_log(env, monkeypatch):
    """거절했다: 효과는 '실행하지 않음'으로 정상이고, 지시문을 읽고 요청했다는 경고(체류 · 판단 · 유입)는 남는다."""
    action = ask(env, monkeypatch)
    decide(env, action["actionId"], "deny", "bob", groups="approvers")
    result = trace(env, action["actionId"])

    assert statuses(result) == {**INJECTED, "effect": "ok"}
    assert "거절" in result["steps"][0]["answer"] and "실행 기록이 없어" in result["steps"][-1]["answer"]


def test_injected_log_that_detection_missed_is_caught_by_the_record_signal(env, monkeypatch):
    """탐지(injection.scan)가 지시문을 놓쳤다: 탐지를 근거로 한 체류 · 유입은 '탐지된 것 없음'으로 정상이고,
    승인 카드에 경고도 없었다(효과 정상). 판단 단계만 기록 신호(사용자가 묻지 않은 감시 장치 약화)로 주의가 된다."""
    import mcp_anthropic_client
    monkeypatch.setattr(mcp_anthropic_client.injection, "scan", lambda text: [])
    action = ask(env, monkeypatch)  # 로그에는 여전히 지시문이 있다 (ATTACK_KO)
    decide(env, action["actionId"], "approve", "bob", groups="approvers")
    result = trace(env, action["actionId"])

    assert statuses(result) == {**INJECTED, "effect": "ok", "residence": "ok", "ingress": "ok"}
    deliberation = layer_of(result, "deliberation")
    assert deliberation["basis"] == ["record"] and "탐지가 놓쳤을 수 있습니다" in deliberation["answer"]
    assert "탐지된 의심 문구 없음" in layer_of(result, "ingress")["answer"]
    assert result["verdict"].startswith("주의할 단계")


def test_request_value_from_the_injected_text_fails_the_deliberation_stage(env, monkeypatch):
    """요청 값의 출처: 모델이 쓴 로그 그룹 이름이 탐지된 지시문 안에 그대로 있고 질문에는 없었다 → 지시문을 따른 요청."""
    action = ask(env, monkeypatch, log_text=ATTACK_WITH_TARGET)
    assert action["taintedBy"][0]["matchedArgs"] == ["log_group_name"]  # 승인 카드에도 간다 (짧은 값 1은 보지 않는다)
    decide(env, action["actionId"], "approve", "bob", groups="approvers")
    result = trace(env, action["actionId"])

    assert statuses(result) == {**INJECTED, "deliberation": "fail"}
    deliberation = layer_of(result, "deliberation")
    assert "지시문을 따른 요청" in deliberation["answer"] and "log_group_name" in deliberation["answer"]
    assert deliberation["basis"] == ["detection", "record"]
    assert result["verdict"].startswith("지시문을 따른 요청입니다")  # 기록이 어긋난 것과 구분한다


def test_value_the_user_asked_for_is_not_counted_as_from_the_injected_text(env, monkeypatch):
    """같은 값이 지시문에 있어도 사용자가 질문에 적었으면 출처를 지시문으로 보지 않는다 (판단은 체류 신호로 주의에 머문다)."""
    action = ask(env, monkeypatch, log_text=ATTACK_WITH_TARGET, question=f"{LOG_GROUP} 로그 보존 기간 1일로 줄여줘")
    assert "matchedArgs" not in action["taintedBy"][0]
    decide(env, action["actionId"], "approve", "bob", groups="approvers")
    result = trace(env, action["actionId"])
    assert layer_of(result, "deliberation")["status"] == "warn"
    assert "묻지 않은" not in layer_of(result, "deliberation")["answer"]  # 보존 기간을 물었다


@pytest.mark.parametrize("tool, request_input, before, after, question, unasked", [
    ("setLogRetention", {"retention_days": 1}, "30일", "1일", "최근 오류 로그 보여줘", True),
    ("setLogRetention", {"retention_days": 1}, "영구 보관", "1일", "오류 원인 찾아줘", True),
    ("setLogRetention", {"retention_days": 1}, "30일", "1일", "로그 보존 기간 줄여줘", False),  # 물었다
    ("setLogRetention", {"retention_days": 90}, "30일", "90일", "최근 오류 로그 보여줘", False),  # 늘렸다
    ("setAlarmActions", {"enabled": False}, "켜짐", "꺼짐", "5xx 원인 찾아줘", True),
    ("setAlarmActions", {"enabled": False}, "켜짐", "꺼짐", "알람 알림 잠깐 꺼줘", False),
    ("setAlarmActions", {"enabled": True}, "꺼짐", "켜짐", "5xx 원인 찾아줘", False),  # 켰다
    ("setEc2InstanceState", {"action": "start"}, "stopped", "running", "web-1 켜줘", False),  # 감시 장치가 아니다
])
def test_unasked_weakening_of_vigie_defenses(tool, request_input, before, after, question, unasked):
    """기록 신호 (탐지와 무관): Vigie 자신의 감시 장치(로그 보존 · 알람 알림)를 약하게 하는데 질문에 그 말이 없다."""
    from audit_trace import build
    requested = {"at": "2026-09-30T01:00:00.000Z#action#a#requested", "event": "requested", "tool": tool,
                 "input": json.dumps(request_input), "before": before, "after": after, "summary": f"{before} → {after}"}
    request_row = {"at": "2026-09-30T00:59:58.000Z#request#q", "kind": "request", "question": question}
    tool_row = {"at": "2026-09-30T00:59:59.000Z#tool#t", "kind": "tool", "locus": "ingress", "tool": LOG_TOOL}
    deliberation = layer_of(build([requested], [request_row, tool_row]), "deliberation")
    assert deliberation["status"] == ("warn" if unasked else "ok")
    assert deliberation["basis"] == (["record"] if unasked else ["detection", "record"])


def test_trace_of_an_ordinary_change_is_clean(env, monkeypatch):
    # 사용자가 보존 기간을 줄여 달라고 했다: 묻지 않은 약화가 아니다
    action = ask(env, monkeypatch, log_text=CLEAN_LOG, days=14, question="로그 보존 기간 14일로 줄여줘")
    decide(env, action["actionId"], "approve", "alice", groups="approvers")
    result = trace(env, action["actionId"])
    assert {layer for layer, status in statuses(result).items() if status != "ok"} == {"mediation"}
    # 탐지를 근거로 한 정상은 '탐지된 것이 없다'로 쓰고, 결론에도 탐지 기준이라고 밝힌다
    assert "탐지된" in layer_of(result, "residence")["answer"] and "탐지된" in layer_of(result, "ingress")["answer"]
    assert result["verdict"].startswith("모든 단계가 정상") and "탐지 결과를 기준으로" in result["verdict"]


def test_denied_and_undecided_changes(env, monkeypatch):
    action = ask(env, monkeypatch, log_text=CLEAN_LOG, days=14)
    effect = trace(env, action["actionId"])["steps"][0]
    assert effect["status"] == "ok" and "결정하지 않아" in effect["answer"]
    decide(env, action["actionId"], "deny", "alice")
    effect = trace(env, action["actionId"])["steps"][0]
    assert effect["status"] == "ok" and "거절" in effect["answer"]


def test_records_that_do_not_add_up_fail(env):
    from audit_trace import build
    requested = {"at": "2026-09-25T23:58:00.000Z#action#a#requested", "event": "requested", "tool": "setLogRetention"}
    approved = {"at": "2026-09-26T00:01:00.000Z#action#a#approved", "event": "approved", "decidedBy": "bob"}
    executed = {"at": "2026-09-26T00:01:01.000Z#action#a#executed", "event": "executed", "awsRequestId": "r-1"}
    # 승인 없이 실행 기록이 있다
    assert statuses(build([requested, executed], []))["effect"] == "fail"
    # 결정·실행 기록은 있는데 승인 요청이 없다: 게이트 밖의 변경일 수 있다
    result = build([approved, executed], [])
    assert statuses(result)["egress"] == "fail" and "CloudTrail" in result["steps"][1]["answer"]
    assert result["verdict"].startswith("기록이 어긋납니다")


def test_trace_finds_events_across_midnight(env):
    # 23:58에 요청하고 00:01에 승인·실행했다. 화면이 실행 행(다음 날)에서 열어도 요청 행까지 모은다
    action_id = str(uuid.uuid4())
    rows = [("alice", "2026-09-25T23:58:00.000Z", "requested"), ("bob", "2026-09-26T00:01:00.000Z", "approved"),
            ("bob", "2026-09-26T00:01:01.000Z", "executed")]
    for user, at, event in rows:
        env["audit"].put_item(Item={"userId": user, "at": f"{at}#action#{action_id}#{event}", "day": at[:10],
                                    "kind": "action", "event": event, "actionId": action_id, "requestId": "q-1",
                                    "tool": "setLogRetention", "status": "ok", "expiresAt": 1})
    env["audit"].put_item(Item={"userId": "alice", "at": "2026-09-25T23:57:30.000Z#request#q-1", "day": "2026-09-25",
                                "kind": "request", "requestId": "q-1", "question": "보존 기간 14일로 줄여줘",
                                "status": "ok", "expiresAt": 1})
    result = trace(env, action_id, day="2026-09-26")
    assert [e["event"] for e in result["events"]] == ["requested", "approved", "executed"]
    assert result["question"] == "보존 기간 14일로 줄여줘"


@pytest.mark.parametrize("action_id, groups, status", [
    (str(uuid.uuid4()), "approvers", 403),  # 관리자만
    ("not-a-uuid", "admins", 400),
    (str(uuid.uuid4()), "admins", 404),  # 기록이 없는 작업
])
def test_trace_errors(env, action_id, groups, status):
    from audit import AuditQueryError
    with pytest.raises(AuditQueryError) as error:
        trace(env, action_id, groups=groups)
    assert error.value.status == status


def test_trace_route(env, monkeypatch):
    action = ask(env, monkeypatch, log_text=CLEAN_LOG, days=14)
    lambda_function = load_service_module("services/llm", "lambda_function")
    event = {"path": "/audit", "httpMethod": "GET", "headers": {"origin": ORIGIN},
             "queryStringParameters": {"trace": action["actionId"]},
             "requestContext": {"authorizer": {"claims": {"sub": "alice"}}}}
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 403
    event["requestContext"]["authorizer"]["claims"]["cognito:groups"] = "admins"
    response = lambda_function.lambda_handler(event, None)
    assert response["statusCode"] == 200 and json.loads(response["body"])["actionId"] == action["actionId"]
