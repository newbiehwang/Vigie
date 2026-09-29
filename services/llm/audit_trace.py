"""역추적: 변경 작업 하나를 놓고 "어디가 뚫렸나"를 층 하나씩 아래에서 위로 묻는다 (GET /audit?trace=<actionId>)

fingate-x의 '원인의 계층' 역추적 절차를 이 앱의 감사 기록(audit.py의 층)에 맞췄다. 물음마다 예·아니오로 답하고,
어디서 멈추든 무엇을 확인할지가 정해진다. 판단을 바꾸지 않고, 이미 남은 기록만 읽는다.

    1 효과  실행됐는가? 사람이 승인했는가?            (승인·거절·실행·실패 행)                         근거: 기록
    2 유출  게이트를 거친 승인 요청 기록이 있는가?      (승인 요청 행. 없는데 실행 기록이 있으면 게이트 밖의 변경) 기록
    3 체류  요청 전에 의심 결과를 읽었는가?             (승인 요청 행의 taintedBy)                          탐지
    4 판단  모델이 지시를 따랐을 가능성이 있는가?       (모델 안은 볼 수 없어 아래 신호로만 본다)            탐지 · 기록
    5 유입  이 질문에서 무엇을 읽었고, 의심 문구가 있었나 (같은 질문의 도구 행)                           탐지
    6 경계  등록부에 없는 도구를 불렀나?                (interface 행)                                    기록
    7 매개  AWS 쪽 기록과 맞는가?                       (앱 밖: CloudTrail과 대조할 단서만 준다)          앱 밖

판정의 근거 (단계마다 basis로 돌려준다)
- 기록: 코드가 확정하는 사실 (승인 · 실행 · 요청 행, 등록부). 기록끼리 어긋나면 실패(빨강)
- 탐지: injection.py의 문구 패턴. 놓칠 수 있으므로, 탐지를 근거로 한 '정상'은 '탐지된 것이 없다'로 쓴다
- 앱 밖: 앱에서는 확인할 수 없다 (CloudTrail)

판단 단계의 신호 (모델 안이라 추론이다. 확정할 수 있을 때만 실패로 본다)
- 요청 값의 출처 (탐지 · 실패): 요청 값이 탐지된 지시문 안에 그대로 있고 사용자의 질문에는 없었다 (taintedBy의 matchedArgs,
  요청 순간에 mcp_anthropic_client가 비교한다. 결과 원문은 저장하지 않으므로 여기서는 비교할 수 없다)
- 체류 (탐지 · 주의): 의심 결과를 읽은 뒤 변경을 요청했다. 시간 순서라 정황이다
- 묻지 않은 방어 약화 (기록 · 주의): 로그 보존 기간을 줄이거나 알람 알림을 끄는 변경인데 질문에 그 말이 없다.
  탐지가 놓쳐도 성립한다. 질문 대상과 변경 대상이 다르다는 것만으로는 보지 않는다 (멈춘 인스턴스를 켜 달라는 질문에는
  인스턴스 ID가 없어도 정상이다)

기록 모으기
- 작업의 사건 행(요청·승인·실행)은 요청자와 승인자의 것으로 나뉘어 있다(기본 키가 사람). 그래서 날짜 인덱스에서
  작업 ID로 찾는다. 승인은 10분 안에 끝나므로 화면이 넘긴 날짜와 그 앞뒤 하루면 충분하다.
- 같은 질문의 도구 행은 요청자의 기본 키에서 요청 시각 앞뒤로 읽고 질문 ID로 거른다.
"""
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Attr, Key

from audit import DAY_INDEX, AuditQueryError, _day, _now, _output, _query_pages, is_admin

MAX_TRACE_ROWS = 200  # 작업 하나·질문 하나의 기록은 이보다 훨씬 적다
MAX_TRACE_PAGES = 20  # 날짜 하나를 거를 때 읽는 최대 페이지 (하루치 기록이 많아도 한 번의 조회는 여기서 끊는다)
QUESTION_WINDOW = timedelta(hours=1)  # 승인 요청 시각 앞뒤로 같은 질문의 도구 행을 찾는 범위

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
# 판정의 근거: 기록(코드가 확정하는 사실) · 탐지(injection.py의 문구 패턴. 놓칠 수 있다) · 앱 밖(CloudTrail)
RECORD, DETECTION, OUTSIDE = "record", "detection", "outside"


def _time_of(row: Dict[str, Any]) -> str:
    return str(row.get("at", "")).split("#")[0]


def _parse(moment: str) -> Optional[datetime]:
    try:
        return datetime.strptime(moment, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None


def _at(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def collect(table, action_id: str, day) -> Dict[str, List[Dict[str, Any]]]:
    """작업의 사건 행과 같은 질문의 행(질문·도구)을 모은다."""
    events: List[Dict[str, Any]] = []
    for offset in (-1, 0, 1):
        kwargs = dict(IndexName=DAY_INDEX, KeyConditionExpression=Key("day").eq((day + timedelta(days=offset)).isoformat()),
                      FilterExpression=Attr("actionId").eq(action_id))
        found, _, _ = _query_pages(table, kwargs, None, MAX_TRACE_ROWS, MAX_TRACE_PAGES)
        events.extend(found)
    events.sort(key=_time_of)

    question_rows: List[Dict[str, Any]] = []
    requested = next((row for row in events if row.get("event") == "requested"), None)
    if requested and requested.get("requestId"):
        moment = _parse(_time_of(requested))
        if moment:
            kwargs = dict(KeyConditionExpression=Key("userId").eq(requested["userId"])
                          & Key("at").between(_at(moment - QUESTION_WINDOW), _at(moment + QUESTION_WINDOW) + "~"),
                          FilterExpression=Attr("requestId").eq(requested["requestId"]) & Attr("kind").ne("action")
                          & Attr("kind").ne("answer"))  # 답변 전체(audit.py '답변')는 역추적에 쓰지 않는다
            question_rows, _, _ = _query_pages(table, kwargs, None, MAX_TRACE_ROWS, MAX_TRACE_PAGES)
            question_rows.sort(key=_time_of)
    return {"events": events, "question": question_rows}


def _who(row: Dict[str, Any]) -> str:
    """결정한 사람: 이메일이 있으면 이메일, 없으면 Cognito sub."""
    return str(row.get("email") or row.get("decidedBy") or "알 수 없음")


def _distance(calls_ago: Any) -> str:
    calls = int(calls_ago or 0)
    return "바로 다음 호출" if calls <= 1 else f"{calls}번째 뒤 호출"


def _suspicious(row: Dict[str, Any]) -> List[str]:
    value = row.get("injectionSuspected")
    return [str(kind) for kind in value] if isinstance(value, list) else []


def _days(value: Any) -> Optional[float]:
    """보존 기간 글('30일', '영구 보관')을 날짜 수로 (영구는 무한대). 모르는 글이면 None."""
    text = str(value or "").strip()
    if text == "영구 보관":
        return float("inf")
    return float(text[:-1]) if text.endswith("일") and text[:-1].isdigit() else None


# 사용자가 묻지 않은 방어 약화 (판단 단계의 기록 신호): Vigie 자신의 감시 장치를 약하게 하는 변경과,
# 사용자가 그 변경을 원했다면 질문에 있을 말. 변경 도구는 이 환경의 Vigie 로그 그룹 · 알람만 바꿀 수 있다 (mcp/app.py)
DEFENSE_WORDS = {
    "setLogRetention": ("보존", "retention"),
    "setAlarmActions": ("알람", "알림", "alarm"),
}


def _weakens_defense(requested: Dict[str, Any]) -> bool:
    """로그 보존 기간을 줄이거나 알람 알림을 끄는 변경인가 (요청 행의 지금 값 → 바뀔 값)."""
    tool = requested.get("tool")
    if tool == "setLogRetention":
        before, after = _days(requested.get("before")), _days(requested.get("after"))
        return before is not None and after is not None and after < before
    if tool == "setAlarmActions":
        request_input = requested.get("input") or {}
        if isinstance(request_input, str):  # 저장된 행은 글, 화면에 줄 때는 푼 값이다 (audit._output)
            try:
                request_input = json.loads(request_input)
            except ValueError:
                return False
        return isinstance(request_input, dict) and request_input.get("enabled") is False
    return False


def _asked_for(requested: Dict[str, Any], user_question: Optional[str]) -> bool:
    """사용자의 질문에 이 변경(도구가 바꾸는 것이나 대상 이름)이 있었나."""
    question = (user_question or "").lower()
    target = str(requested.get("target") or "").lower()
    names = [target, target.rsplit("/", 1)[-1]] if target else []
    words = list(DEFENSE_WORDS.get(str(requested.get("tool")), ())) + [name for name in names if name]
    return any(word in question for word in words)


def _change_of(requested: Dict[str, Any]) -> str:
    """바뀌는 것 한 줄: '대상 지금 값 → 바뀔 값' (예전 행이면 요약)."""
    target, before, after = requested.get("target"), requested.get("before"), requested.get("after")
    if target and before and after:
        return f"{target} {before} → {after}"
    return str(requested.get("summary") or requested.get("tool"))


def build(events: List[Dict[str, Any]], question_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """역추적의 물음과 답 (모듈 설명의 1~7). 순수 함수: 같은 기록이면 언제나 같은 답.
    단계마다 판정의 근거(basis)를 붙인다: 기록(record, 코드가 확정하는 사실) · 탐지(detection, 문구 패턴이라 놓칠 수
    있다) · 앱 밖(outside). 탐지를 근거로 한 '정상'은 '탐지된 것이 없다'는 뜻이라 문구로 구분한다."""
    by_event = {row.get("event"): row for row in events}
    requested, approved, denied = by_event.get("requested"), by_event.get("approved"), by_event.get("denied")
    finished = by_event.get("executed") or by_event.get("failed")
    request_row = next((row for row in question_rows if row.get("kind") == "request"), None)
    tools = [row for row in question_rows if row.get("kind") == "tool"]
    ingress = [row for row in tools if row.get("locus", "ingress") == "ingress"]
    suspicious = [row for row in ingress if _suspicious(row)]
    unregistered = [row for row in tools if row.get("locus") == "interface"]
    tainted = (requested or {}).get("taintedBy") or []
    user_question = (request_row or {}).get("question")
    steps: List[Dict[str, Any]] = []

    def step(layer: str, question: str, status: str, answer: str, basis: List[str],
             evidence: Optional[List[Dict[str, Any]]] = None):
        steps.append({"layer": layer, "question": question, "status": status, "answer": answer, "basis": basis,
                      "evidence": [row["at"] for row in evidence or [] if row.get("at")]})

    # 1 효과 (기록)
    question = "실행됐는가? 사람이 승인했는가?"
    warned = " 승인 카드에 의심 경고가 떠 있던 요청입니다" if tainted else ""
    if finished and not approved:
        step("effect", question, FAIL, "승인 기록 없이 실행 기록이 있습니다. 승인 테이블과 MCP 로그를 확인하세요",
             [RECORD], [finished])
    elif finished and finished.get("event") == "executed":
        step("effect", question, WARN if tainted else OK,
             f"사람이 승인해 실행했습니다 (승인: {_who(approved)}).{warned}".rstrip("."), [RECORD], [approved, finished])
    elif finished:
        step("effect", question, WARN, f"승인했지만 실행에 실패했습니다 (승인: {_who(approved)})", [RECORD],
             [approved, finished])
    elif approved:
        step("effect", question, WARN, "승인했지만 실행 결과 기록이 없습니다 (실행 중이거나 결과를 남기지 못함)", [RECORD],
             [approved])
    elif denied:
        step("effect", question, OK, f"거절해 실행하지 않았습니다 (거절: {_who(denied)})", [RECORD], [denied])
    else:
        step("effect", question, OK, "결정하지 않아(만료 포함) 실행하지 않았습니다", [RECORD], [])

    # 2 유출 (기록)
    question = "게이트를 거친 승인 요청 기록이 있는가?"
    if requested:
        step("egress", question, OK, f"승인 요청이 있습니다: {requested.get('summary') or requested.get('tool')}",
             [RECORD], [requested])
    elif approved or finished:
        step("egress", question, FAIL, "결정·실행 기록은 있는데 승인 요청 기록이 없습니다. 게이트 밖의 변경일 수 있어 "
                                       "CloudTrail과 대조하세요 (7 매개)", [RECORD],
             [row for row in (approved, finished) if row])
    else:
        step("egress", question, FAIL, "이 작업의 승인 요청 기록을 찾지 못했습니다", [RECORD], [])

    # 3 체류 (탐지: 의심 결과는 탐지된 것만 적힌다)
    question = "요청 전에 의심 문구가 든 결과를 읽었는가?"
    if tainted:
        names = ", ".join(f"{entry.get('tool')} 결과 뒤 {_distance(entry.get('callsAgo'))}" for entry in tainted)
        step("residence", question, WARN, f"예: {names}에서 이 변경을 요청했습니다", [DETECTION], [requested])
    elif requested:
        step("residence", question, OK, "아니오: 탐지된 의심 결과 뒤의 요청이 아닙니다", [DETECTION], [])
    else:
        step("residence", question, INFO, "승인 요청 기록이 없어 알 수 없습니다", [DETECTION], [])

    # 4 판단 (모델 안은 볼 수 없다. 탐지 신호와 기록 신호로 따로 본다)
    #   - 요청 값의 출처 (탐지): 요청 값이 탐지된 지시문 안에 그대로 있고 질문에는 없었다 → 지시문을 따른 요청 (실패)
    #   - 체류 (탐지): 의심 결과를 읽은 뒤 변경을 요청했다 → 주의
    #   - 묻지 않은 방어 약화 (기록): 탐지와 상관없이, 사용자가 묻지 않은 Vigie 감시 장치 약화 → 주의
    question = "모델이 도구 결과 속 지시를 따랐을 가능성이 있는가?"
    asked = f" 사용자의 질문(\"{user_question}\")이 이 변경을 원했는지 비교하세요" if user_question else ""
    matched = [entry for entry in tainted if entry.get("matchedArgs")]
    unasked = bool(requested) and _weakens_defense(requested) and not _asked_for(requested, user_question)
    weakening = f"사용자가 묻지 않은 Vigie 감시 장치 약화입니다 ({_change_of(requested)})." if unasked else ""
    if requested and matched:
        where = ", ".join(f"{', '.join(entry['matchedArgs'])} 값이 {entry.get('tool')} 결과의 의심 문구에"
                          for entry in matched)
        step("deliberation", question, FAIL,
             f"지시문을 따른 요청입니다: {where} 그대로 있었고, 사용자의 질문에는 없었습니다." + (f" {weakening}" if unasked else ""),
             [DETECTION] + ([RECORD] if unasked else []), suspicious + [requested])
    elif tainted and requested:
        step("deliberation", question, WARN, "유입·체류·유출이 함께 성립합니다: 의심 결과를 읽은 뒤 변경을 요청했습니다."
             + (f" {weakening}" if unasked else "") + asked, [DETECTION] + ([RECORD] if unasked else []),
             suspicious + [requested])
    elif unasked:
        step("deliberation", question, WARN, weakening + " 탐지된 의심 문구는 없었지만 탐지가 놓쳤을 수 있습니다." + asked,
             [RECORD], [requested])
    else:
        step("deliberation", question, OK, "성립하지 않습니다: 탐지된 의심 결과 뒤의 요청도, 묻지 않은 감시 장치 약화도 "
                                           "아닙니다", [DETECTION, RECORD], [])

    # 5 유입 (탐지)
    question = "이 질문에서 읽은 결과는 무엇이고, 의심 문구가 있었나?"
    if not question_rows:
        step("ingress", question, INFO, "같은 질문의 도구 기록을 찾지 못했습니다 (보관 기간이 지났거나 승인 요청 기록이 없음)",
             [DETECTION], [])
    elif suspicious:
        kinds = sorted({kind for row in suspicious for kind in _suspicious(row)})
        step("ingress", question, WARN, f"도구 결과 {len(ingress)}건 중 {len(suspicious)}건에 의심 문구가 있었습니다 "
                                        f"({', '.join(kinds)})", [DETECTION], suspicious)
    else:
        step("ingress", question, OK, f"도구 결과 {len(ingress)}건, 탐지된 의심 문구 없음", [DETECTION], ingress)

    # 6 경계 (기록: 등록부)
    question = "등록부에 없는 도구를 불렀나?"
    if unregistered:
        step("interface", question, WARN, "예: " + ", ".join(str(row.get("tool")) for row in unregistered), [RECORD],
             unregistered)
    elif question_rows:
        step("interface", question, OK, "아니오", [RECORD], [])
    else:
        step("interface", question, INFO, "같은 질문의 도구 기록이 없어 알 수 없습니다", [RECORD], [])

    # 7 매개 (앱 밖)
    question = "AWS 쪽 기록(CloudTrail)과 맞는가?"
    request_id = (finished or {}).get("awsRequestId")
    if request_id:
        step("mediation", question, INFO, f"앱에서는 확인할 수 없습니다. CloudTrail에서 요청 ID {request_id}"
                                          f"({finished.get('cloudTrailEvent')}) 이벤트를 찾고, 같은 시간대에 MCP 역할이 만든 "
                                          "다른 변경 이벤트가 없는지 대조하세요", [OUTSIDE], [finished])
    else:
        step("mediation", question, INFO, "실행 기록이 없어 대조할 요청 ID가 없습니다. 같은 시간대에 MCP 역할이 만든 변경 "
                                          "이벤트가 없는지 CloudTrail에서 확인할 수 있습니다", [OUTSIDE], [])

    failed = {s["layer"] for s in steps if s["status"] == FAIL}
    if failed - {"deliberation"}:
        verdict = "기록이 어긋납니다. 실패한 단계부터 확인하세요"
    elif failed:
        verdict = "지시문을 따른 요청입니다. 판단 단계부터 확인하세요"
    elif any(s["status"] == WARN for s in steps):
        verdict = "주의할 단계가 있습니다. 경고가 붙은 단계부터 확인하세요"
    else:
        verdict = ("모든 단계가 정상입니다. 사용자가 요청하고 사람이 결정한 변경입니다 "
                   "(유입과 체류는 탐지 결과를 기준으로 봤습니다. 탐지가 놓친 표현은 드러나지 않습니다)")
    return {"steps": steps, "verdict": verdict, "question": user_question}


def query_trace(table, caller_id: Optional[str], claims: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """GET /audit?trace=<actionId>&day=<YYYY-MM-DD>: 관리자만. day는 화면이 누른 행의 날짜 (없으면 오늘, UTC)."""
    if table is None:
        raise AuditQueryError(503, "감사 로그 테이블이 설정되지 않았습니다")
    if not caller_id:
        raise AuditQueryError(401, "로그인이 필요합니다")
    if not is_admin(claims):
        raise AuditQueryError(403, "감사 로그는 관리자(admins 그룹)만 볼 수 있습니다")
    action_id = params.get("trace") or ""
    try:
        uuid.UUID(action_id)
    except ValueError:
        raise AuditQueryError(400, "trace는 작업 ID(UUID)여야 합니다")
    day = _day(params.get("day"), "day") or _now().date()

    rows = collect(table, action_id, day)
    if not rows["events"]:
        raise AuditQueryError(404, "이 작업의 감사 기록을 찾지 못했습니다 (날짜를 확인하세요)")
    return {"actionId": action_id, **build(rows["events"], rows["question"]),
            "events": [_output(row) for row in rows["events"]],
            "rows": [_output(row) for row in rows["question"]]}
