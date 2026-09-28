"""역추적 (services/llm/audit_trace.py, GET /audit?trace=<actionId>)

변경 작업 하나를 놓고 효과 → 유출 → 체류 → 판단 → 유입 → 경계 → 매개 순서로 예·아니오를 답한다.
- 실제 흐름(질문 → 도구 → 승인 요청 → 승인 → 실행)이 남긴 기록만으로 답한다
- 요청자와 승인자의 기록, 자정을 넘긴 기록도 모은다
- 기록이 어긋나면(승인 없이 실행, 요청 없이 결정) 그 층이 실패로 보인다
- 관리자만 본다
"""
import json
import uuid

import pytest

from conftest import load_service_module
from test_approvals import ORIGIN, decide, env  # noqa: F401 (env는 fixture)
from test_audit_locus import CLEAN_LOG, LOG_TOOL, log_call, run, write_call
from test_injection import ATTACK_KO


def trace(env, action_id, day=None, groups="admins"):
    from audit_trace import query_trace
    params = {"trace": action_id}
    if day:
        params["day"] = day
    return query_trace(env["audit"], "carol", {"sub": "carol", "cognito:groups": groups}, params)


def statuses(result):
    return {step["layer"]: step["status"] for step in result["steps"]}


def ask(env, monkeypatch, log_text=ATTACK_KO, days=1):
    body = run(env, monkeypatch, [
        {"content": [log_call("toolu_1")]},
        {"content": [write_call("toolu_2", days=days)]},
        {"content": [{"type": "text", "text": "요청했습니다."}]},
    ], log_text=log_text)
    return body["inference"]["pendingActions"][0]


def test_trace_of_a_change_that_followed_an_injected_log(env, monkeypatch):
    action = ask(env, monkeypatch)  # 공격 문구가 든 로그를 읽은 뒤 변경을 요청했다
    decide(env, action["actionId"], "approve", "bob", groups="approvers", email="bob@example.com")
    result = trace(env, action["actionId"])

    assert statuses(result) == {"effect": "ok", "egress": "ok", "residence": "warn", "deliberation": "warn",
                                "ingress": "warn", "interface": "ok", "mediation": "info"}
    steps = {step["layer"]: step for step in result["steps"]}
    assert "bob@example.com" in steps["effect"]["answer"]  # 결정한 사람 (승인자의 기록)
    assert "최근 오류 로그 보여줘" in steps["deliberation"]["answer"]  # 사용자의 질문과 비교하라고
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


def test_trace_of_an_ordinary_change_is_clean(env, monkeypatch):
    action = ask(env, monkeypatch, log_text=CLEAN_LOG, days=14)
    decide(env, action["actionId"], "approve", "alice", groups="approvers")
    result = trace(env, action["actionId"])
    assert {layer for layer, status in statuses(result).items() if status != "ok"} == {"mediation"}
    assert result["verdict"].startswith("모든 단계가 정상")


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
