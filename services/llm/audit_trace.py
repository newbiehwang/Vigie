"""역추적: 변경 작업 하나를 놓고 "어디가 뚫렸나"를 층 하나씩 아래에서 위로 묻는다 (GET /audit?trace=<actionId>)

fingate-x의 '원인의 계층' 역추적 절차를 이 앱의 감사 기록(audit.py의 층)에 맞췄다. 물음마다 예·아니오로 답하고,
어디서 멈추든 무엇을 확인할지가 정해진다. 판단을 바꾸지 않고, 이미 남은 기록만 읽는다.

    1 효과  실행됐는가? 사람이 승인했는가?            (승인·거절·실행·실패 행)
    2 유출  게이트를 거친 승인 요청 기록이 있는가?      (승인 요청 행. 없는데 실행 기록이 있으면 게이트 밖의 변경)
    3 체류  요청 전에 의심 결과를 읽었는가?             (승인 요청 행의 taintedBy)
    4 판단  유입·체류·유출이 함께 성립하는가?            (모델 안은 볼 수 없어 삼각측량으로만 본다)
    5 유입  이 질문에서 무엇을 읽었고, 의심 문구가 있었나 (같은 질문의 도구 행)
    6 경계  등록부에 없는 도구를 불렀나?                (interface 행)
    7 매개  AWS 쪽 기록과 맞는가?                       (앱 밖: CloudTrail과 대조할 단서만 준다)

기록 모으기
- 작업의 사건 행(요청·승인·실행)은 요청자와 승인자의 것으로 나뉘어 있다(기본 키가 사람). 그래서 날짜 인덱스에서
  작업 ID로 찾는다. 승인은 10분 안에 끝나므로 화면이 넘긴 날짜와 그 앞뒤 하루면 충분하다.
- 같은 질문의 도구 행은 요청자의 기본 키에서 요청 시각 앞뒤로 읽고 질문 ID로 거른다.
"""
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Attr, Key

from audit import DAY_INDEX, AuditQueryError, _day, _now, _output, _query_pages, is_admin

MAX_TRACE_ROWS = 200  # 작업 하나·질문 하나의 기록은 이보다 훨씬 적다
MAX_TRACE_PAGES = 20  # 날짜 하나를 거를 때 읽는 최대 페이지 (하루치 기록이 많아도 한 번의 조회는 여기서 끊는다)
QUESTION_WINDOW = timedelta(hours=1)  # 승인 요청 시각 앞뒤로 같은 질문의 도구 행을 찾는 범위

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


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


def build(events: List[Dict[str, Any]], question_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """역추적의 물음과 답 (모듈 설명의 1~7). 순수 함수: 같은 기록이면 언제나 같은 답."""
    by_event = {row.get("event"): row for row in events}
    requested, approved, denied = by_event.get("requested"), by_event.get("approved"), by_event.get("denied")
    finished = by_event.get("executed") or by_event.get("failed")
    request_row = next((row for row in question_rows if row.get("kind") == "request"), None)
    tools = [row for row in question_rows if row.get("kind") == "tool"]
    ingress = [row for row in tools if row.get("locus", "ingress") == "ingress"]
    suspicious = [row for row in ingress if _suspicious(row)]
    unregistered = [row for row in tools if row.get("locus") == "interface"]
    tainted = (requested or {}).get("taintedBy") or []
    steps: List[Dict[str, Any]] = []

    def step(layer: str, question: str, status: str, answer: str, evidence: Optional[List[Dict[str, Any]]] = None):
        steps.append({"layer": layer, "question": question, "status": status, "answer": answer,
                      "evidence": [row["at"] for row in evidence or [] if row.get("at")]})

    # 1 효과
    question = "실행됐는가? 사람이 승인했는가?"
    if finished and not approved:
        step("effect", question, FAIL, "승인 기록 없이 실행 기록이 있습니다. 승인 테이블과 MCP 로그를 확인하세요", [finished])
    elif finished and finished.get("event") == "executed":
        step("effect", question, OK, f"사람이 승인해 실행했습니다 (승인: {_who(approved)})", [approved, finished])
    elif finished:
        step("effect", question, WARN, f"승인했지만 실행에 실패했습니다 (승인: {_who(approved)})", [approved, finished])
    elif approved:
        step("effect", question, WARN, "승인했지만 실행 결과 기록이 없습니다 (실행 중이거나 결과를 남기지 못함)", [approved])
    elif denied:
        step("effect", question, OK, f"거절해 실행하지 않았습니다 (거절: {_who(denied)})", [denied])
    else:
        step("effect", question, OK, "결정하지 않아(만료 포함) 실행하지 않았습니다", [])

    # 2 유출
    question = "게이트를 거친 승인 요청 기록이 있는가?"
    if requested:
        step("egress", question, OK, f"승인 요청이 있습니다: {requested.get('summary') or requested.get('tool')}", [requested])
    elif approved or finished:
        step("egress", question, FAIL, "결정·실행 기록은 있는데 승인 요청 기록이 없습니다. 게이트 밖의 변경일 수 있어 "
                                       "CloudTrail과 대조하세요 (7 매개)", [row for row in (approved, finished) if row])
    else:
        step("egress", question, FAIL, "이 작업의 승인 요청 기록을 찾지 못했습니다", [])

    # 3 체류
    question = "요청 전에 의심 문구가 든 결과를 읽었는가?"
    if tainted:
        names = ", ".join(f"{entry.get('tool')} 결과 뒤 {_distance(entry.get('callsAgo'))}" for entry in tainted)
        step("residence", question, WARN, f"예: {names}에서 이 변경을 요청했습니다", [requested])
    elif requested:
        step("residence", question, OK, "아니오", [])
    else:
        step("residence", question, INFO, "승인 요청 기록이 없어 알 수 없습니다", [])

    # 4 판단 (모델 안은 볼 수 없다: 유입·체류·유출이 함께 성립하면 그 층이 뚫린 것으로 본다)
    question = "모델이 도구 결과 속 지시를 따랐을 가능성이 있는가?"
    user_question = (request_row or {}).get("question")
    if tainted and requested:
        asked = f" 사용자의 질문(\"{user_question}\")이 이 변경을 원했는지 비교하세요" if user_question else ""
        step("deliberation", question, WARN, "유입·체류·유출이 함께 성립합니다: 의심 결과를 읽은 뒤 변경을 요청했습니다." + asked,
             suspicious + [requested])
    else:
        step("deliberation", question, OK, "성립하지 않습니다", [])

    # 5 유입
    question = "이 질문에서 읽은 결과는 무엇이고, 의심 문구가 있었나?"
    if not question_rows:
        step("ingress", question, INFO, "같은 질문의 도구 기록을 찾지 못했습니다 (보관 기간이 지났거나 승인 요청 기록이 없음)", [])
    elif suspicious:
        kinds = sorted({kind for row in suspicious for kind in _suspicious(row)})
        step("ingress", question, WARN, f"도구 결과 {len(ingress)}건 중 {len(suspicious)}건에 의심 문구가 있었습니다 "
                                        f"({', '.join(kinds)})", suspicious)
    else:
        step("ingress", question, OK, f"도구 결과 {len(ingress)}건, 의심 문구 없음", ingress)

    # 6 경계
    question = "등록부에 없는 도구를 불렀나?"
    if unregistered:
        step("interface", question, WARN, "예: " + ", ".join(str(row.get("tool")) for row in unregistered), unregistered)
    elif question_rows:
        step("interface", question, OK, "아니오", [])
    else:
        step("interface", question, INFO, "같은 질문의 도구 기록이 없어 알 수 없습니다", [])

    # 7 매개 (앱 밖)
    question = "AWS 쪽 기록(CloudTrail)과 맞는가?"
    request_id = (finished or {}).get("awsRequestId")
    if request_id:
        step("mediation", question, INFO, f"앱에서는 확인할 수 없습니다. CloudTrail에서 요청 ID {request_id}"
                                          f"({finished.get('cloudTrailEvent')}) 이벤트를 찾고, 같은 시간대에 MCP 역할이 만든 "
                                          "다른 변경 이벤트가 없는지 대조하세요", [finished])
    else:
        step("mediation", question, INFO, "실행 기록이 없어 대조할 요청 ID가 없습니다. 같은 시간대에 MCP 역할이 만든 변경 "
                                          "이벤트가 없는지 CloudTrail에서 확인할 수 있습니다", [])

    if any(s["status"] == FAIL for s in steps):
        verdict = "기록이 어긋납니다. 실패한 단계부터 확인하세요"
    elif any(s["status"] == WARN for s in steps):
        verdict = "주의할 단계가 있습니다. 경고가 붙은 단계부터 확인하세요"
    else:
        verdict = "모든 단계가 정상입니다. 사용자가 요청하고 사람이 결정한 변경입니다"
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
