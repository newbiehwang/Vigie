"""역추적: 변경 작업 하나를 놓고 "어느 관문에서 무엇이 보였나"를 묻는다 (GET /audit?trace=<actionId>)

변경이 지나는 길을 관문 세 개로 나누고, 관문마다 남은 기록만 읽어 판정한다. 판단을 바꾸지 않는다.

    경계  등록부에 있는 도구만 불렀나?                         (interface 행)                       근거: 기록
    유입  무엇을 읽었고, 의심 문구가 있었나? 그 뒤 변경을 요청했나? (같은 질문의 도구 행, 요청 행의 taintedBy)  탐지
          요청 값이 탐지된 지시문에서 왔나? (taintedBy의 matchedArgs)
    실행  승인 요청을 거쳐, 사람이 결정했나? 묻지 않은 감시 장치 약화인가?  (요청·승인·거절·실행·실패 행)     기록

관문이 아닌 둘은 판정하지 않고 글로만 돌려준다
- 판단(assumption): 모델 안은 볼 수 없다. 그래서 묻지 않고, 모델은 언제든 속을 수 있다고 가정한다.
  이 가정 때문에 변경은 언제나 사람의 승인을 거친다 (실행 관문)
- 매개(premise): 모든 변경은 MCP 역할로만 나간다는 전제. 앱 밖(IAM · CloudTrail)이라 대조할 요청 ID만 준다

예전에는 처리 단계 일곱 개(효과 · 유출 · 체류 · 판단 · 유입 · 경계 · 매개)를 따로 판정했다. 실제 기록에서 판단은 언제나
추론, 매개는 언제나 '앱 밖'이라 판정이 갈리지 않았고, 체류는 유입의 조건, 유출과 효과는 같은 승인 관문의 앞뒤였다.
그래서 판정이 실제로 갈리는 자리(기록이나 탐지로 확인할 수 있는 곳) 셋만 남겼다.

판정의 근거 (관문마다 basis로 돌려준다)
- 기록: 코드가 확정하는 사실 (승인 · 실행 · 요청 행, 등록부). 기록끼리 어긋나면 실패(빨강)
- 탐지: injection.py의 문구 패턴. 놓칠 수 있으므로, 탐지를 근거로 한 '정상'은 '탐지된 것이 없다'로 쓴다
관문 하나에 찾은 것(findings)이 여럿일 수 있고, 관문의 판정은 그중 가장 무거운 것이다 (실패 > 주의 > 정상 > 참고)

실패로 보는 것은 코드로 확정되는 경우만이다
- 기록끼리 어긋날 때 (승인 없이 실행, 요청 없이 결정): 실행 관문
- 요청 값이 탐지된 지시문 안에 그대로 있고 사용자의 질문에는 없었을 때 (요청 순간에 mcp_anthropic_client가 비교한
  matchedArgs. 결과 원문은 저장하지 않으므로 여기서는 비교할 수 없다): 유입 관문
주의로 보는 것: 의심 결과를 읽은 뒤의 요청(유입, 시간 순서라 정황), 사용자가 묻지 않은 감시 장치 약화(실행, 탐지가
놓쳐도 성립한다. 질문 대상과 변경 대상이 다르다는 것만으로는 보지 않는다), 의심 경고를 보고도 한 승인(실행)

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
# 판정의 근거: 기록(코드가 확정하는 사실) · 탐지(injection.py의 문구 패턴. 놓칠 수 있다)
RECORD, DETECTION = "record", "detection"


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


# 사용자가 묻지 않은 방어 약화 (실행 관문의 기록 신호): Vigie 자신의 감시 장치를 약하게 하는 변경과,
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


# 판정의 무게 (관문의 판정 = 찾은 것 중 가장 무거운 것)
WEIGHT = {INFO: 0, OK: 1, WARN: 2, FAIL: 3}

ASSUMPTION = ("모델 안은 볼 수 없어, 모델은 언제든 도구 결과 속 지시에 속을 수 있다고 가정합니다. "
              "그래서 판정하지 않고, AWS를 바꾸는 실행은 언제나 사람의 승인을 거칩니다")


def build(events: List[Dict[str, Any]], question_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """관문 셋(경계 · 유입 · 실행)의 판정과 가정(판단) · 전제(매개). 순수 함수: 같은 기록이면 언제나 같은 답."""
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
    asked = f" 사용자의 질문(\"{user_question}\")이 이 변경을 원했는지 비교하세요" if user_question else ""
    steps: List[Dict[str, Any]] = []

    def gate(name: str, question: str, basis: List[str], findings: List[Dict[str, Any]]):
        """찾은 것들을 관문 하나로: 판정은 가장 무거운 것, 답은 찾은 것을 차례로 이은 글, 근거 행은 모두."""
        status = max((finding["status"] for finding in findings), key=WEIGHT.__getitem__)
        evidence: List[str] = []
        for finding in findings:
            for at in finding.pop("rows"):
                if at not in evidence:
                    evidence.append(at)
        steps.append({"gate": name, "question": question, "status": status, "basis": basis, "findings": findings,
                      "answer": " ".join(finding["text"] for finding in findings), "evidence": evidence})

    def found(status: str, text: str, rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return {"status": status, "text": text, "rows": [row["at"] for row in rows or [] if row and row.get("at")]}

    # 경계 (기록: 등록부)
    if unregistered:
        findings = [found(WARN, "등록부에 없는 도구를 불렀습니다: " + ", ".join(str(row.get("tool")) for row in unregistered)
                          + ". 변경 도구로 다뤄 승인을 거쳤습니다", unregistered)]
    elif question_rows:
        findings = [found(OK, "등록부에 있는 도구만 불렀습니다")]
    else:
        findings = [found(INFO, "같은 질문의 도구 기록이 없어 알 수 없습니다")]
    gate("interface", "등록부에 있는 도구만 불렀나?", [RECORD], findings)

    # 유입 (탐지: 읽은 결과의 의심 문구 · 그 뒤의 요청 · 요청 값의 출처)
    findings = []
    if not question_rows:
        findings.append(found(INFO, "같은 질문의 도구 기록을 찾지 못했습니다 (보관 기간이 지났거나 승인 요청 기록이 없음)"))
    elif suspicious:
        kinds = sorted({kind for row in suspicious for kind in _suspicious(row)})
        findings.append(found(WARN, f"도구 결과 {len(ingress)}건 중 {len(suspicious)}건에 의심 문구가 있었습니다 "
                                    f"({', '.join(kinds)}).", suspicious))
    else:
        findings.append(found(OK, f"도구 결과 {len(ingress)}건, 탐지된 의심 문구 없음.", ingress))
    if tainted:
        names = ", ".join(f"{entry.get('tool')} 결과 뒤 {_distance(entry.get('callsAgo'))}" for entry in tainted)
        findings.append(found(WARN, f"의심 결과를 읽은 뒤 변경을 요청했습니다 ({names}).", [requested]))
    elif requested:
        findings.append(found(OK, "탐지된 의심 결과 뒤의 요청이 아닙니다."))
    matched = [entry for entry in tainted if entry.get("matchedArgs")]
    if requested and matched:
        where = ", ".join(f"{', '.join(entry['matchedArgs'])} 값이 {entry.get('tool')} 결과의 의심 문구에"
                          for entry in matched)
        findings.append(found(FAIL, f"지시문에서 온 값으로 요청했습니다: {where} 그대로 있었고, 사용자의 질문에는 "
                                    "없었습니다.", suspicious + [requested]))
    gate("ingress", "무엇을 읽었고, 그 뒤 변경을 요청했나?", [DETECTION], findings)

    # 실행 (기록: 승인 요청 → 결정 → 실행, 그리고 바뀌는 것 자체)
    findings = []
    if requested:
        findings.append(found(OK, f"승인 요청을 거쳤습니다 ({requested.get('summary') or requested.get('tool')}).",
                              [requested]))
    elif approved or finished:
        findings.append(found(FAIL, "결정·실행 기록은 있는데 승인 요청 기록이 없습니다. 승인 관문 밖의 변경일 수 있어 "
                                    "CloudTrail과 대조하세요.", [approved, finished]))
    else:
        findings.append(found(FAIL, "이 작업의 승인 요청 기록을 찾지 못했습니다."))
    warned = " 승인 카드에 의심 경고가 떠 있던 요청입니다." if tainted else ""
    if finished and not approved:
        findings.append(found(FAIL, "승인 기록 없이 실행 기록이 있습니다. 승인 테이블과 MCP 로그를 확인하세요.", [finished]))
    elif finished and finished.get("event") == "executed":
        findings.append(found(WARN if tainted else OK, f"사람이 승인해 실행했습니다 (승인: {_who(approved)}).{warned}",
                              [approved, finished]))
    elif finished:
        findings.append(found(WARN, f"승인했지만 실행에 실패했습니다 (승인: {_who(approved)}).", [approved, finished]))
    elif approved:
        findings.append(found(WARN, "승인했지만 실행 결과 기록이 없습니다 (실행 중이거나 결과를 남기지 못함).", [approved]))
    elif denied:
        findings.append(found(OK, f"거절해 실행하지 않았습니다 (거절: {_who(denied)}).", [denied]))
    else:
        findings.append(found(OK, "결정하지 않아(만료 포함) 실행하지 않았습니다."))
    if requested and _weakens_defense(requested) and not _asked_for(requested, user_question):
        missed = "" if tainted else " 탐지된 의심 문구는 없었지만 탐지가 놓쳤을 수 있습니다."
        findings.append(found(WARN, f"사용자가 묻지 않은 Vigie 감시 장치 약화입니다 ({_change_of(requested)}).{missed}{asked}",
                              [requested]))
    gate("execution", "승인 요청을 거쳐, 사람이 결정했나?", [RECORD], findings)

    # 매개 (전제, 앱 밖): 대조할 단서만
    request_id = (finished or {}).get("awsRequestId")
    premise = {"text": "모든 변경은 MCP 역할로만 나간다는 전제입니다. 앱 밖(IAM · CloudTrail)이라 판정하지 않습니다. "
                       + (f"CloudTrail에서 요청 ID {request_id}({finished.get('cloudTrailEvent')}) 이벤트를 찾고, "
                          "같은 시간대에 MCP 역할이 만든 다른 변경 이벤트가 없는지 대조하세요" if request_id else
                          "실행 기록이 없어 대조할 요청 ID가 없습니다. 같은 시간대에 MCP 역할이 만든 변경 이벤트가 "
                          "없는지 CloudTrail에서 확인할 수 있습니다"),
               "evidence": [finished["at"]] if request_id else []}
    if request_id:
        premise.update(awsRequestId=request_id, cloudTrailEvent=finished.get("cloudTrailEvent"))

    by_gate = {step["gate"]: step["status"] for step in steps}
    if FAIL in (by_gate["interface"], by_gate["execution"]):
        verdict = "기록이 어긋납니다. 실패한 관문부터 확인하세요"
    elif by_gate["ingress"] == FAIL:
        verdict = "지시문에서 온 값으로 요청한 변경입니다. 유입 관문부터 확인하세요"
    elif WARN in by_gate.values():
        verdict = "주의할 관문이 있습니다. 주의가 붙은 관문부터 확인하세요"
    else:
        verdict = ("세 관문 모두 정상입니다. 사용자가 요청하고 사람이 결정한 변경입니다 "
                   "(유입은 탐지 결과를 기준으로 봤습니다. 탐지가 놓친 표현은 드러나지 않습니다)")
    return {"steps": steps, "verdict": verdict, "question": user_question, "assumption": ASSUMPTION, "premise": premise}


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
