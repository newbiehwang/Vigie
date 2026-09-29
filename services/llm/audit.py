"""감사 로그: 누가, 언제, 어떤 질문으로, 어떤 도구를, 어떤 입력으로 불렀고 결과가 어땠는지

    도구 반복(mcp_anthropic_client._report) ──┬──▶ 진행 상황 (llm_progress.py, 화면용·1시간)
                                              └──▶ 감사 로그 (여기)
                                                     ├─▶ DynamoDB vigie-audit-<env>   (화면에서 조회, 90일 뒤 TTL)
                                                     └─▶ CloudWatch Logs /vigie/<env>/audit (1년 보관, Logs Insights로 분석)

기록 단위
- 도구 호출 한 번 = 항목 하나 (kind "tool"): 도구, 입력, 성공·실패, 오류, 걸린 시간, 결과 크기
  진단 도구(diagnoseService)는 층별 판정(diagnosis)도 남긴다: 서비스, 자원, 층마다 판정·찾은 것·확인 항목.
  감사 로그 화면이 이것으로 그 서비스의 진단 층 그림을 그린다 (도구 결과 전체는 남기지 않으므로 판정만 따로 떼어 둔다)
- 질문 하나 = 항목 하나 (kind "request"): 질문, 모델, 성공·실패, 도구 호출 수, 가린 값의 수, 걸린 시간,
  답변 미리보기(answerPreview)와 답변 글자 수(answerChars), 쓴 토큰(tokens·modelCalls)과 예상 비용(costMicroUsd·price)
- 질문 하나의 답변 = 항목 하나 (kind "answer"): 사용자가 받은 답변 전체. 아래 '답변' 참고
- 변경 작업의 사건 하나 = 항목 하나 (kind "action"): 요청·승인·거절·실행·실패, 작업 ID, 도구, 인자, 결정한 사람
- 사용자 관리의 사건 하나 = 항목 하나 (kind "admin"): 초대·권한 변경(전→후)·정지·정지 해제, 대상, 한 사람
두 종류 모두 요청자(sub, 이메일, 웹·Slack), 질문 ID(requestId), 대화 ID(sessionId)를 함께 남긴다.

층 (locus): 사고가 났을 때 "어디가 뚫렸나"를 층 하나씩 좁히려고, 기록마다 도구 반복 위의 자리를 적는다.
fingate-x의 '원인의 계층'(상태 변화에서 거꾸로 세운 층)을 이 앱에 맞춰 옮겼다. 판단은 바꾸지 않고 기록만 나눈다.
    interface  경계  등록부(위험도)에 없는 도구를 불렀다 (변경 도구로 다룬다)
    ingress    유입  조회·결과물 도구의 결과가 들어왔다. 의심 문구(injectionSuspected)가 이 층의 흔적
    residence  체류  의심 결과를 읽은 뒤 같은 질문에서 변경을 요청했다 (요청 행의 taintedBy. 따로 행을 두지 않는다)
    egress     유출  변경 도구를 부르려 했다 (그 도구 호출 행과 승인 요청 행)
    effect     효과  승인·거절·실행·실패
    (판단층은 모델 안이라 기록할 수 없다. 유입·체류·유출이 함께 보이면 그 층이 뚫린 것으로 본다)
질문 행(kind "request")은 요약이라 층이 없다.

가리기 (redaction.py)
- 비밀 값은 감사 로그에도 남기지 않는다.
- 계정 ID·액세스 키 ID·이메일은 원래 값으로 남긴다: 모델에는 가명을 보냈지만, 감사는 "실제로 무엇을 조회했나"를 추적해야 한다.
  그래서 도구 입력은 모델이 준 입력(가명)이 아니라 도구가 실제로 받은 입력(되돌린 값)을 남긴다.

답변 (GET /audit?answer=…)
- 사용자가 실제로 받은 답변(화면에 보인 글자, 가명·가림 적용 뒤)을 남긴다. 대화 기록은 사용자가 지울 수 있어 증거가 되지 못한다
- 목록 화면은 기간 안의 기록을 날짜 인덱스로 한꺼번에 받으므로, 긴 답변을 질문 항목에 넣으면 목록이 무거워지고
  한 번의 조회(1MB)에 읽히는 건수가 줄어든다. 그래서 질문 항목에는 미리보기만 두고, 전체는 따로 둔다
      질문 항목  at = <시각>#request#<질문 ID>   day 있음 → 날짜 인덱스(목록)에 들어간다
      답변 항목  at = <시각>#answer#<질문 ID>    day 없음 → 날짜 인덱스에 들어가지 않는다 (희소 인덱스). 팝업창이 열 때만 읽는다
- CloudWatch Logs에는 답변 항목을 따로 쓰지 않고, 질문 줄에 답변 전체를 함께 쓴다 (Logs Insights에서 한 줄로 보게)
- 답변 길이는 모델의 출력 상한(MAX_TOKENS)으로 이미 묶여 있다. ANSWER_LIMIT은 그래도 넘칠 때를 위한 안전 상한이다
  (DynamoDB 항목 400KB, CloudWatch Logs 이벤트 256KB. 한글 한 글자는 UTF-8로 3바이트)

토큰과 예상 비용 (llm_cost.py)
- LLM 응답이 있는 행은 질문 행뿐이다 (도구 호출·변경 작업·사용자 관리 행은 서버·사람이 한 일). 그래서 질문 행에만 남긴다
- tokens: 입력·출력·캐시 쓰기·캐시 읽기의 합. modelCalls: 모델을 부를 때마다의 토큰 (어느 단계에서 많이 썼나).
  모델 한 번이 도구 여러 개를 함께 부르기도 하므로 도구 행마다 나누지 않는다
- costMicroUsd: 예상 비용 (마이크로달러 정수). price: 계산에 쓴 단가 (USD / 백만 토큰). 단가가 나중에 바뀌어도
  옛 기록의 비용이 바뀌지 않게 기록할 때의 단가를 같이 둔다. 단가표에 없는 모델이면 둘 다 없다 (토큰만)
- 실패한 질문도 그때까지 쓴 토큰과 비용을 남긴다 (실패해도 요금은 나간다)

추가만 한다: 이미 있는 항목을 덮어쓰지 않고(조건부 쓰기), LLM Lambda에는 수정·삭제 권한을 주지 않는다.

기록에 실패해도 답변은 계속 만든다 (조회 도구). 변경 작업의 사건(action_event)은 기록하지 못하면 예외를 올려
승인·실행을 멈춘다 (기록 없이 AWS가 바뀌지 않게, approvals.py).
"""
import base64
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Attr, Key

from approvals import tainted_view
from llm_cost import cost_micro_usd, price_of
from llm_progress import _plain

# 층 (모듈 설명). residence는 조회 조건으로만 쓴다 (taintedBy가 있는 승인 요청 행)
LOCI = ("interface", "ingress", "residence", "egress", "effect")

AUDIT_TTL_DAYS = 90  # DynamoDB 보관 기간. CloudWatch Logs는 1년 (llm.yaml의 AuditLogGroup)
ADMIN_GROUP = "admins"  # 감사 로그는 이 Cognito 그룹의 사용자만 본다 (자기 것 포함 모든 사람의 기록)

# 크기 제한 (DynamoDB 항목은 400KB까지)
QUESTION_LIMIT = 500
INPUT_LIMIT = 2000
ERROR_LIMIT = 500
ANSWER_LIMIT = 50000  # 답변 전체의 안전 상한 (50,000자 × 3바이트 = 150KB, 모듈 설명 '답변')
ANSWER_PREVIEW = 200  # 질문 항목(목록)에 두는 답변 앞부분

# 조회 제한
DEFAULT_DAYS = 7  # 기간을 주지 않으면 최근 7일
MAX_DAYS = 31  # 전체 사용자 조회는 날짜마다 따로 읽으므로 기간을 제한한다
DIAGNOSIS_TOOL = "diagnoseService"  # 층별 판정을 남기는 도구 (mcp/lambda_mcp/diagnose.py)
DIAGNOSIS_TEXT_LIMIT = 400  # 판정의 글 하나 (찾은 것·요약)
DIAGNOSIS_CHECKS = 12  # 층 하나에 남길 확인 항목의 최대 수
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_MODEL_CALLS = 50  # 질문 행에 남기는 모델 호출별 토큰의 최대 개수 (도구 반복 상한보다 넉넉하게)
MAX_PAGES = 10  # 거르기(도구·결과) 때문에 빈 페이지가 이어져도 한 번의 조회는 여기서 끊고 cursor를 돌려준다
DAY_INDEX = "by-day"


def _trail_of(result: Optional[str]) -> Optional[Dict[str, Any]]:
    """변경 도구 결과의 CloudTrail 단서 (mcp/app.py의 _trail). 없으면 None."""
    try:
        trail = json.loads(result or "").get("cloudtrail")
    except (ValueError, AttributeError):
        return None
    return trail if isinstance(trail, dict) and trail.get("request_id") else None


def diagnosis_of(name: str, result: Any) -> Optional[Dict[str, Any]]:
    """진단 도구 결과에서 감사 로그에 남길 층별 판정만 떼어 낸다. 다른 도구·실패한 진단이면 None.
    result: MCP tools/call의 결과 ({"content": [{"type": "text", "text": "<JSON>"}]})."""
    if name != DIAGNOSIS_TOOL:
        return None
    try:
        body = json.loads(result["content"][0]["text"])
    except (TypeError, KeyError, IndexError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(body.get("layers"), list):
        return None

    def text(value: Any) -> str:
        return _clip(str(value or ""), DIAGNOSIS_TEXT_LIMIT)

    return {
        "service": text(body.get("service")),
        "serviceName": text(body.get("service_name")),
        "resource": text(body.get("resource")),
        **({"target": text(body["target"])} if body.get("target") else {}),  # VPC 연결의 목적지
        "hours": int(body.get("hours") or 0),
        "summary": text(body.get("summary")),
        "causes": [text(layer) for layer in body.get("causes", [])],
        "layers": [{
            "id": text(layer.get("id")), "name": text(layer.get("name")), "component": text(layer.get("component")),
            "status": text(layer.get("status")), "finding": text(layer.get("finding")),
            "checks": [{key: text(check.get(key)) for key in ("name", "status", "finding", "evidence")}
                       for check in layer.get("checks", [])[:DIAGNOSIS_CHECKS]],
        } for layer in body["layers"] if isinstance(layer, dict)],
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """정렬 키에 쓰는 시각 (밀리초까지, 글자 순서 = 시간 순서)."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class CloudWatchSink:
    """감사 로그 전용 로그 그룹에 JSON 한 줄씩 쓴다. 로그 스트림은 Lambda 실행 환경마다 하나 만든다."""

    def __init__(self, log_group: Optional[str], client=None):
        self._group = log_group or None
        self._client = client
        self._stream: Optional[str] = None

    def write(self, record: Dict[str, Any]) -> None:
        if self._group is None:
            return
        if self._client is None:
            import boto3
            self._client = boto3.client("logs")
        if self._stream is None:
            stream = f"{_now():%Y/%m/%d}/{uuid.uuid4().hex}"
            self._client.create_log_stream(logGroupName=self._group, logStreamName=stream)
            self._stream = stream
        self._client.put_log_events(
            logGroupName=self._group, logStreamName=self._stream,
            logEvents=[{"timestamp": int(time.time() * 1000), "message": json.dumps(record, ensure_ascii=False)}])


class AuditLog:
    """요청 하나의 감사 기록. mcp_anthropic_client._report가 진행 상황과 같은 이름의 메서드를 부른다."""

    def __init__(self, table, sink: Optional[CloudWatchSink], redactor, *, user_id: str, email: Optional[str],
                 source: str, request_id: Optional[str], session_id: Optional[str], model_id: Optional[str],
                 question: str):
        self._table = table
        self._sink = sink
        self._redactor = redactor
        self.model_id = model_id  # 기본 모델로 바뀔 수 있어 클라이언트를 만든 뒤 다시 넣는다
        self._common = {
            "userId": user_id,
            "email": email,
            "source": source,
            "requestId": request_id or str(uuid.uuid4()),  # Slack 요청은 requestId가 없다
            "sessionId": session_id,
        }
        self._question = _clip(redactor.secrets_only(question or ""), QUESTION_LIMIT)
        self._started = _now()
        self._tools: Dict[str, Tuple[datetime, str, Any, str]] = {}  # 도구 호출 ID → (시작 시각, 이름, 입력, 층)
        self.tool_count = 0
        self.suspicious_count = 0  # 지시문처럼 보이는 문구가 든 도구 결과 수 (injection.py)

    # ---------------------------------------------------------------- 도구 반복에서 부르는 메서드
    def tool_started(self, tool_id: str, name: str, tool_input: Any, restore: bool = True,
                     locus: str = "ingress") -> None:
        # 모델이 준 입력에는 가명이 들어 있을 수 있다. 도구가 실제로 받은 값으로 되돌린 뒤 비밀 값만 가린다.
        # restore=False: 가명 그대로 받은 도구 (차트 등 결과물 도구. mcp_anthropic_client)
        # locus: 도구 반복이 정한 층 (등록부에 없으면 interface, 변경 도구면 egress, 나머지는 ingress)
        actual = self._redactor.secrets_only(self._redactor.restore(tool_input) if restore else tool_input)
        self._tools[tool_id] = (_now(), name, actual, locus)

    def tool_finished(self, tool_id: str, ok: bool, error: Optional[str] = None,
                      result_chars: Optional[int] = None, suspicious: Optional[List[str]] = None,
                      diagnosis: Optional[Dict[str, Any]] = None) -> None:
        """diagnosis: 진단 도구의 층별 판정 (diagnosis_of). 비밀 값만 가린 뒤 남긴다."""
        started, name, tool_input, locus = self._tools.pop(tool_id, (_now(), "unknown", {}, "ingress"))
        self.tool_count += 1
        record = {
            "kind": "tool",
            "locus": locus,
            "tool": name,
            "toolUseId": tool_id,
            "input": _clip(json.dumps(tool_input, ensure_ascii=False, default=str), INPUT_LIMIT),
            "status": "ok" if ok else "error",
            "ms": int((_now() - started).total_seconds() * 1000),
        }
        if error:
            record["error"] = _clip(self._redactor.secrets_only(str(error)), ERROR_LIMIT)
        if result_chars is not None:
            record["resultChars"] = int(result_chars)
        if suspicious:
            record["injectionSuspected"] = list(suspicious)
            self.suspicious_count += 1
        if diagnosis:
            record["diagnosis"] = self._redactor.secrets_only(diagnosis)
        self._write(started, tool_id, record)

    # ---------------------------------------------------------------- 요청이 끝날 때 (llm_service)
    def request_finished(self, ok: bool, error: Optional[str] = None, answer: Optional[str] = None,
                         usage: Optional[Dict[str, Any]] = None) -> None:
        """answer: 사용자가 받은 답변 (실패한 요청은 없다). 질문 항목에는 미리보기, 답변 항목에는 전체를 남긴다.
        usage: 쓴 토큰 (AnthropicMCPClient.usage_summary: tokens 합계와 calls 목록). 없으면 토큰·비용을 남기지 않는다."""
        record = {
            "kind": "request",
            "question": self._question,
            "model": self.model_id,
            "status": "ok" if ok else "error",
            "toolCount": self.tool_count,
            "injectionSuspected": self.suspicious_count,
            # 이번 요청에서 Claude로 보내기 전에 가린 값의 수 (종류별)
            "redacted": dict(self._redactor.counts),
            "ms": int((_now() - self._started).total_seconds() * 1000),
        }
        if error:
            record["error"] = _clip(self._redactor.secrets_only(str(error)), ERROR_LIMIT)
        if usage and usage.get("tokens"):
            tokens = {kind: int(usage["tokens"].get(kind) or 0) for kind in ("input", "output", "cacheWrite", "cacheRead")}
            record["tokens"] = tokens
            record["modelCalls"] = [dict(call) for call in (usage.get("calls") or [])[:MAX_MODEL_CALLS]]
            cost = cost_micro_usd(self.model_id, tokens)
            if cost is not None:
                record["costMicroUsd"] = cost
                record["price"] = price_of(self.model_id)
        full = None
        if answer is not None:
            # 화면에 보인 답변은 이미 가렸지만, 다른 항목처럼 비밀 값을 한 번 더 가린다
            text = self._redactor.secrets_only(str(answer))
            full = _clip(text, ANSWER_LIMIT)
            record["answerPreview"] = _clip(text, ANSWER_PREVIEW)
            record["answerChars"] = len(text)  # 잘리기 전의 전체 길이
        request_id = self._common["requestId"]
        # CloudWatch Logs에는 질문 줄에 답변 전체를 함께 쓴다
        self._write(self._started, f"request#{request_id}", record,
                    log_extra={"answer": full} if full is not None else None)
        if full is not None:
            # 답변 전체: 날짜 인덱스에 넣지 않고(목록이 무거워지지 않게), CloudWatch Logs에는 이미 썼다
            self._write(self._started, f"answer#{request_id}",
                        {"kind": "answer", "answer": full, "answerChars": record["answerChars"]},
                        indexed=False, logged=False)

    # ---------------------------------------------------------------- 변경 작업 (approvals.py, llm_service)
    def action_event(self, event: str, action: Dict[str, Any], decided_by: Optional[str] = None,
                     result: Optional[str] = None) -> None:
        """변경 작업의 사건 (requested · approved · denied · executed · failed). 저장하지 못하면 예외를 올린다."""
        record = {
            "kind": "action",
            "locus": "egress" if event == "requested" else "effect",
            "event": event,
            "actionId": action.get("actionId"),
            "tool": action.get("tool"),
            "input": _clip(self._redactor.secrets_only(action.get("args") or "{}"), INPUT_LIMIT),
            "summary": action.get("summary"),
            "status": "error" if event == "failed" else "ok",
            "decidedBy": decided_by,
            # 이 변경을 요청하기 전에 같은 질문에서 읽은 의심 결과 (체류층, approvals 모듈 설명). 요청 행에만 있다
            "taintedBy": (tainted_view(action.get("taintedBy")) or None) if event == "requested" else None,
        }
        if result:
            record["result"] = _clip(self._redactor.secrets_only(result), ERROR_LIMIT)
        # 실행했으면 CloudTrail 이벤트를 찾을 단서 (요청 ID가 CloudTrail 이벤트의 requestID와 같다)
        trail = _trail_of(result)
        if trail:
            record["awsRequestId"] = trail.get("request_id")
            record["cloudTrailEvent"] = f"{trail.get('event_source')}:{trail.get('event_name')}"
        self._write(_now(), f"action#{action.get('actionId')}#{event}", record, strict=True)

    # ---------------------------------------------------------------- 사용자 관리 (user_admin.py)
    def admin_event(self, event: str, target: Dict[str, Any], group: Optional[str] = None, ok: bool = True,
                    error: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        """관리자가 사용자를 바꾼 사건 (invited · role_changed · disabled · enabled). extra: 권한 변경의 fromRole·toRole.
        바꾸기 전에 남기고, 저장하지 못하면 예외를 올려 바꾸지 않는다. 바꾸다 실패하면 ok=False로 한 번 더 남긴다."""
        record = {
            "kind": "admin",
            "event": event,
            "targetUser": target.get("username"),
            "targetEmail": target.get("email"),
            "group": group,
            "status": "ok" if ok else "error",
            "decidedBy": self._common["userId"],
            **(extra or {}),
        }
        if error:
            record["error"] = _clip(self._redactor.secrets_only(str(error)), ERROR_LIMIT)
        suffix = f"admin#{uuid.uuid4().hex[:12]}#{event}"
        self._write(_now(), suffix, record, strict=True)

    # ---------------------------------------------------------------- 저장
    def _write(self, moment: datetime, suffix: str, record: Dict[str, Any], strict: bool = False, *,
               indexed: bool = True, logged: bool = True, log_extra: Optional[Dict[str, Any]] = None) -> None:
        """indexed=False: 날짜 인덱스에 넣지 않는다 (day를 두지 않는다. 답변 항목).
        logged=False: CloudWatch Logs에 쓰지 않는다. log_extra: CloudWatch Logs에만 더 쓸 값 (질문 줄의 답변 전체)."""
        at = _iso(moment)
        item = {**self._common, **record,
                # 정렬 키: 시각 + 도구 호출 ID 또는 질문 ID (같은 밀리초에 여러 건이어도 겹치지 않게)
                "at": f"{at}#{suffix}",
                "day": at[:10] if indexed else None,  # 날짜 인덱스 (관리자가 기간으로 전체 사용자를 조회)
                "expiresAt": int(time.time()) + AUDIT_TTL_DAYS * 86400}
        item = {key: value for key, value in item.items() if value is not None}
        if strict and self._table is None:
            raise RuntimeError("감사 로그 테이블이 설정되지 않아 변경 작업을 기록할 수 없습니다")
        try:
            if self._table is not None:
                # 추가만: 같은 키가 이미 있으면 덮어쓰지 않고 실패한다
                self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(userId)")
        except Exception as error:
            if strict:
                raise
            print(f"감사 로그 저장 실패 (DynamoDB, 계속 진행): {error}")
        try:
            if self._sink is not None and logged:
                self._sink.write({"vigie_audit": True, **{k: v for k, v in item.items() if k != "expiresAt"},
                                  **(log_extra or {})})
        except Exception as error:
            print(f"감사 로그 저장 실패 (CloudWatch Logs, 계속 진행): {error}")


# ---------------------------------------------------------------- 조회: GET /audit

class AuditQueryError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def groups_of(claims: Dict[str, Any]) -> List[str]:
    """ID 토큰의 cognito:groups. API Gateway(REST) Cognito 권한 부여자는 목록을 글자로 넘긴다
    (그룹 하나면 "admins", 여럿이면 "[admins approvers]"처럼). 목록으로 오는 경우도 받는다."""
    raw = (claims or {}).get("cognito:groups")
    if isinstance(raw, list):
        return [str(group) for group in raw]
    if not raw:
        return []
    return [group for group in str(raw).strip("[]").replace(",", " ").split() if group]


def is_admin(claims: Dict[str, Any]) -> bool:
    return ADMIN_GROUP in groups_of(claims)


def _day(value: Optional[str], name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AuditQueryError(400, f"{name}는 YYYY-MM-DD 형식이어야 합니다")


def _encode_cursor(cursor: Dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode()


def _decode_cursor(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        cursor = json.loads(base64.urlsafe_b64decode(value.encode()))
    except (ValueError, TypeError):
        raise AuditQueryError(400, "cursor가 올바르지 않습니다")
    if not isinstance(cursor, dict):
        raise AuditQueryError(400, "cursor가 올바르지 않습니다")
    return cursor


def _query_pages(table, kwargs: Dict[str, Any], start_key, limit: int, pages_left: int):
    """limit개를 채우거나 더 없을 때까지 읽는다. 거르기 때문에 한 페이지에 limit보다 적게 올 수 있어
    남은 개수만큼만 Limit으로 읽는다 (그래야 LastEvaluatedKey로 정확히 이어 읽을 수 있다)."""
    items, key = [], start_key
    while len(items) < limit and pages_left > 0:
        request = dict(kwargs, Limit=limit - len(items))
        if key:
            request["ExclusiveStartKey"] = key
        page = table.query(**request)
        pages_left -= 1
        items.extend(page.get("Items", []))
        key = page.get("LastEvaluatedKey")
        if not key:
            break
    return items, key, pages_left


def _output(item: Dict[str, Any]) -> Dict[str, Any]:
    item = _plain(item)
    item.pop("expiresAt", None)
    if isinstance(item.get("input"), str):
        try:
            item["input"] = json.loads(item["input"])
        except ValueError:
            pass  # 길어서 잘린 입력은 글자로 둔다
    return item


def query_audit(table, caller_id: Optional[str], claims: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """감사 로그 조회. admins 그룹만 볼 수 있다: 자기 기록(scope=mine), 모든 사람(scope=all), 특정 사람(user=<sub>).
    일반 사용자는 자기 기록도 볼 수 없다 (403). 화면이 탭을 숨기는 것은 편의일 뿐이고, 막는 곳은 여기다.

    params (쿼리 문자열): from, to (YYYY-MM-DD, UTC), scope (mine|all), user, tool, status (ok|error),
                          kind (tool|request|action), locus (LOCI), limit, cursor
    """
    if table is None:
        raise AuditQueryError(503, "감사 로그 테이블이 설정되지 않았습니다")
    if not caller_id:
        raise AuditQueryError(401, "로그인이 필요합니다")
    params = params or {}
    # 관리자만: 조건을 읽기 전에 막는다 (잘못된 조건에 400으로 답해 조회 방법을 알려 주지 않게)
    if not is_admin(claims):
        raise AuditQueryError(403, "감사 로그는 관리자(admins 그룹)만 볼 수 있습니다")

    # 누구의 기록인가
    scope = params.get("scope") or "mine"
    target = params.get("user") or None
    if scope not in ("mine", "all"):
        raise AuditQueryError(400, "scope는 mine 또는 all이어야 합니다")
    if scope == "mine":
        target = target or caller_id

    # 기간 (UTC 날짜)
    end = _day(params.get("to"), "to") or _now().date()
    start = _day(params.get("from"), "from") or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise AuditQueryError(400, "from이 to보다 늦습니다")
    if (end - start).days + 1 > MAX_DAYS:
        raise AuditQueryError(400, f"기간은 {MAX_DAYS}일까지 조회할 수 있습니다")

    try:
        limit = min(max(int(params.get("limit") or DEFAULT_LIMIT), 1), MAX_LIMIT)
    except ValueError:
        raise AuditQueryError(400, "limit은 숫자여야 합니다")

    # 거르기 (도구·결과·종류). 답변 항목은 목록에 넣지 않는다: 날짜 인덱스에는 없지만,
    # 한 사람을 기본 키로 읽을 때는 걸리므로 거른다 (모듈 설명 '답변')
    conditions = [Attr("kind").ne("answer")]
    for field, name in (("tool", "tool"), ("status", "status"), ("kind", "kind")):
        if params.get(field):
            conditions.append(Attr(name).eq(params[field]))
    locus = params.get("locus")
    if locus:
        if locus not in LOCI:
            raise AuditQueryError(400, f"locus는 {', '.join(LOCI)} 중 하나여야 합니다")
        # 체류층은 행이 따로 없다: 의심 결과를 읽은 뒤의 승인 요청 행
        conditions.append(Attr("taintedBy").exists() if locus == "residence" else Attr("locus").eq(locus))
    base: Dict[str, Any] = {"ScanIndexForward": False}  # 최신 기록부터
    if conditions:
        expression = conditions[0]
        for condition in conditions[1:]:
            expression = expression & condition
        base["FilterExpression"] = expression

    cursor = _decode_cursor(params.get("cursor"))
    if target:
        # 한 사람: 기본 키(userId + at)로 기간을 읽는다
        start_key = cursor.get("key")
        if start_key and start_key.get("userId") != target:
            raise AuditQueryError(400, "cursor가 올바르지 않습니다")
        kwargs = dict(base, KeyConditionExpression=Key("userId").eq(target)
                      & Key("at").between(f"{start.isoformat()}T", f"{end.isoformat()}T~"))
        items, key, _ = _query_pages(table, kwargs, start_key, limit, MAX_PAGES)
        next_cursor = _encode_cursor({"key": key}) if key else None
    else:
        # 모든 사람: 날짜 인덱스(day + at)로 최근 날짜부터 하루씩 읽는다
        days = [end - timedelta(days=offset) for offset in range((end - start).days + 1)]
        day_names = [d.isoformat() for d in days]
        index = day_names.index(cursor["day"]) if cursor.get("day") in day_names else 0
        start_key = cursor.get("key") if cursor.get("day") in day_names else None
        items, next_cursor, pages_left = [], None, MAX_PAGES
        while index < len(day_names):
            kwargs = dict(base, IndexName=DAY_INDEX, KeyConditionExpression=Key("day").eq(day_names[index]))
            found, key, pages_left = _query_pages(table, kwargs, start_key, limit - len(items), pages_left)
            items.extend(found)
            if key:  # 이 날짜에 더 있다 (개수를 채웠거나 페이지 한도)
                next_cursor = _encode_cursor({"day": day_names[index], "key": key})
                break
            index, start_key = index + 1, None
            if len(items) >= limit or pages_left <= 0:
                if index < len(day_names):
                    next_cursor = _encode_cursor({"day": day_names[index]})
                break

    return {
        "items": [_output(item) for item in items],
        "cursor": next_cursor,
        "scope": "all" if not target else ("mine" if target == caller_id else "user"),
        "isAdmin": True,  # 여기까지 왔으면 관리자다 (예전 화면과의 호환을 위해 남긴다)
        "from": start.isoformat(),
        "to": end.isoformat(),
    }


# ---------------------------------------------------------------- 조회: GET /audit?answer=<질문 행의 at>&user=<요청자>

REQUEST_AT = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)#request#([0-9A-Za-z-]{1,64})")


def query_answer(table, caller_id: Optional[str], claims: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """질문 하나의 답변 전체 (모듈 설명 '답변'). 관리자만. 화면은 목록에서 받은 질문 행의 at과 userId를 그대로 넘긴다.
    답변 항목의 키는 질문 행의 키에서 request를 answer로 바꾼 것이다. 조회는 기본 키 Query 하나 (새 권한이 필요 없다)."""
    if table is None:
        raise AuditQueryError(503, "감사 로그 테이블이 설정되지 않았습니다")
    if not caller_id:
        raise AuditQueryError(401, "로그인이 필요합니다")
    if not is_admin(claims):
        raise AuditQueryError(403, "감사 로그는 관리자(admins 그룹)만 볼 수 있습니다")
    params = params or {}
    user = params.get("user") or ""
    match = REQUEST_AT.fullmatch(params.get("answer") or "")
    if not match or not user:
        raise AuditQueryError(400, "answer는 질문 행의 at, user는 그 요청자여야 합니다")
    key = f"{match.group(1)}#answer#{match.group(2)}"
    items = table.query(KeyConditionExpression=Key("userId").eq(user) & Key("at").eq(key)).get("Items", [])
    if not items:
        # 답변을 남기기 전에 쌓인 질문이거나, 실패해 답변이 없는 질문
        raise AuditQueryError(404, "이 질문의 답변 기록이 없습니다")
    item = _output(items[0])
    return {"answer": item.get("answer", ""), "answerChars": item.get("answerChars")}
