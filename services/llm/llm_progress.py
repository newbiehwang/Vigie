"""답변을 만드는 동안의 진행 상황 (Claude Code처럼 '생각 중 → 도구 실행 → 답변'을 화면에 보여 주기 위한 기록)

    화면 ──POST /llm1 {requestId}──▶ LLM Lambda ── 단계마다 기록 ──▶ DynamoDB (wga-llm-progress-<env>)
    화면 ──GET /llm1/progress/{requestId} (1초마다)──▶ LLM Lambda ── 읽기 ──┘

/llm1은 답이 다 만들어진 뒤에 한 번만 응답한다(API Gateway REST, 동기). 그래서 화면은 답을 기다리는 동안
진행 상황 API를 따로 불러 지금까지의 단계를 가져간다.

단계(steps)는 일어난 순서대로 쌓는다.
- {"type": "thinking", "text": 모델의 사고 요약}
- {"type": "tool", "id", "name", "input": 짧은 값만, "status": "running" | "ok" | "error", "error"?, "ms"?: 걸린 시간}
- {"type": "search", "query": 검색 패턴, "found": [찾은 도구 이름], "error"?}: 도구 검색 (tool_search.py). 모델 응답 안에서
  Anthropic 서버가 이미 마친 검색이라 '실행 중'이 없다
지금 하는 일(phase)은 "thinking"(모델 응답을 기다리는 중) · "tool"(도구 실행 중) · "done" · "error"다.

같은 단계 목록을 답변의 inference.steps에도 넣어, 다시 불러온 대화에서도 사고 과정과 도구를 순서대로 볼 수 있다.
진행 상황 저장이 실패해도 답변은 계속 만든다 (화면에 진행 상황이 안 보일 뿐이다).
"""
import re
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

PROGRESS_TTL_SECONDS = 3600  # 진행 상황은 답을 기다리는 동안만 쓴다. 한 시간 뒤 DynamoDB TTL이 지운다

# 화면 요청이 보내는 requestId (crypto.randomUUID). 다른 모양은 받지 않는다
REQUEST_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# 크기 제한. DynamoDB 항목은 400KB까지이고, 답변과 함께 저장하는 대화 기록은 세션 하나가 항목 하나라 더 아껴야 한다
PROGRESS_TEXT_LIMIT = 4000  # 진행 상황: 사고 요약 한 덩어리
SAVED_TEXT_LIMIT = 1500  # 대화 기록(inference.steps): 사고 요약 한 덩어리
SAVED_STEP_LIMIT = 40  # 대화 기록: 단계 수
INPUT_VALUE_LIMIT = 200  # 도구 입력값 하나
ERROR_LIMIT = 300  # 도구 오류 메시지
SEARCH_FOUND_LIMIT = 20  # 도구 검색 한 번에 찾은 도구 이름 (기본 5개, 모델이 limit로 늘릴 수 있다)
LIVE_SAVE_SECONDS = 0.8  # 스트리밍 중 사고 요약을 저장하는 간격 (글자가 올 때마다 쓰면 DynamoDB 쓰기가 너무 많다)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _short_input(tool_input: Any) -> Dict[str, Any]:
    """도구 입력 중 글자·정수·참거짓만 짧게 남긴다 (차트 데이터 같은 배열·객체는 길어서 뺀다).
    실수는 DynamoDB에 그대로 넣을 수 없어(Decimal만 받는다) 글자로 바꾼다."""
    if not isinstance(tool_input, dict):
        return {}
    short = {}
    for key, value in tool_input.items():
        if isinstance(value, bool) or isinstance(value, int):
            short[key] = value
        elif isinstance(value, float):
            short[key] = str(value)
        elif isinstance(value, str) and value.strip():
            short[key] = _clip(value, INPUT_VALUE_LIMIT)
    return short


class ProgressReporter:
    """한 요청의 진행 상황. table이 없으면 저장하지 않고 단계만 모은다 (Slack 봇, requestId가 없는 요청)."""

    def __init__(self, table=None, request_id: Optional[str] = None, owner_id: Optional[str] = None):
        self._table = table if (table is not None and request_id and owner_id) else None
        self._request_id = request_id
        self._owner_id = owner_id
        self._started = int(time.time() * 1000)
        self._tool_started: Dict[str, float] = {}
        self.phase = "thinking"
        self.steps: List[Dict[str, Any]] = []
        self._live_saved = 0.0  # 스트리밍 중인 사고 요약을 마지막으로 저장한 때
        self._save()  # 화면이 첫 조회에서 바로 '생각하는 중'을 보도록 시작하자마자 한 번 저장한다

    # ---------------------------------------------------------------- 단계
    def thinking_started(self) -> None:
        """모델에 요청을 보냈다 (응답을 기다리는 동안 화면에는 '생각하는 중')."""
        self.phase = "thinking"
        self._save()

    def thinking_live(self, text: str) -> None:
        """스트리밍 중인 사고 요약 (지금까지 받은 전부). 같은 단계를 늘려 가며 LIVE_SAVE_SECONDS마다 저장한다.
        화면은 첫 응답이 다 오기를 기다리지 않고 1~2초 안에 생각하는 내용을 보기 시작한다."""
        text = (text or "").strip()
        if not text:
            return
        if self.steps and self.steps[-1].get("live"):
            self.steps[-1]["text"] = _clip(text, PROGRESS_TEXT_LIMIT)
        else:
            self.steps.append({"type": "thinking", "text": _clip(text, PROGRESS_TEXT_LIMIT), "live": True})
        now = time.time()
        if now - self._live_saved >= LIVE_SAVE_SECONDS:
            self._live_saved = now
            self._save()

    def thought(self, text: str) -> None:
        """모델이 돌려준 사고 요약 한 덩어리 (다 받은 것). 스트리밍 중이던 단계가 있으면 그 단계를 마무리한다."""
        text = (text or "").strip()
        live = self.steps[-1] if self.steps and self.steps[-1].get("live") else None
        if live is not None:
            live.pop("live")
            if text:
                live["text"] = _clip(text, PROGRESS_TEXT_LIMIT)
            elif not live["text"]:
                self.steps.pop()
            self._save()
            return
        if not text:
            return
        self.steps.append({"type": "thinking", "text": _clip(text, PROGRESS_TEXT_LIMIT)})
        self._save()

    def tool_started(self, tool_id: str, name: str, tool_input: Any, restore: bool = True,
                     locus: str = "ingress") -> None:
        """restore(도구에 가명을 되돌려 넘겼는지)와 locus(감사 로그의 층)는 감사 로그가 쓴다.
        진행 상황은 가린 값만 보여 준다."""
        self.phase = "tool"
        self._tool_started[tool_id] = time.time()
        self.steps.append({"type": "tool", "id": tool_id, "name": name, "input": _short_input(tool_input),
                           "status": "running"})
        self._save()

    def tool_finished(self, tool_id: str, ok: bool, error: Optional[str] = None,
                      result_chars: Optional[int] = None, suspicious: Optional[List[str]] = None) -> None:
        """도구가 끝났다. result_chars(결과 크기)는 감사 로그(audit.py)가 쓰고 진행 상황에는 남기지 않는다.
        suspicious: 결과에 든 지시문처럼 보이는 문구의 종류 (injection.py). 화면에 '의심 문구'로 보인다."""
        for step in reversed(self.steps):
            if step["type"] == "tool" and step["id"] == tool_id:
                step["status"] = "ok" if ok else "error"
                started = self._tool_started.pop(tool_id, None)
                if started is not None:
                    step["ms"] = int((time.time() - started) * 1000)
                if error:
                    step["error"] = _clip(str(error), ERROR_LIMIT)
                if suspicious:
                    step["suspicious"] = list(suspicious)
                break
        self._save()

    def tool_search(self, query: str, found: List[str], error: Optional[str] = None) -> None:
        """모델이 도구를 찾아 불러왔다 (도구 검색). 무엇을 찾았는지 화면에 보인다."""
        step = {"type": "search", "query": _clip(str(query or ""), INPUT_VALUE_LIMIT),
                "found": [str(name) for name in (found or [])][:SEARCH_FOUND_LIMIT]}
        if error:
            step["error"] = _clip(str(error), ERROR_LIMIT)
        self.steps.append(step)
        self._save()

    def finished(self, ok: bool = True) -> None:
        self.phase = "done" if ok else "error"
        self._save()

    # ---------------------------------------------------------------- 대화 기록에 넣을 단계
    def saved_steps(self) -> List[Dict[str, Any]]:
        """답변의 inference.steps로 저장할 단계 (사고 요약을 더 짧게, 단계 수도 제한)."""
        saved = []
        for step in self.steps[:SAVED_STEP_LIMIT]:
            step = {key: value for key, value in step.items() if key != "live"}
            if step["type"] == "thinking":
                step["text"] = _clip(step["text"], SAVED_TEXT_LIMIT)
            saved.append(step)
        return saved

    # ---------------------------------------------------------------- 저장
    def _save(self) -> None:
        if self._table is None:
            return
        now = int(time.time())
        try:
            self._table.put_item(
                Item={
                    "requestId": self._request_id,
                    "ownerId": self._owner_id,
                    "phase": self.phase,
                    "steps": self.steps,
                    "startedAt": self._started,
                    "updatedAt": int(now * 1000),
                    "expiresAt": now + PROGRESS_TTL_SECONDS,
                },
                # requestId는 화면이 정한다. 다른 사람의 진행 상황을 덮어쓰지 못하게 처음 만든 사람만 쓸 수 있다
                ConditionExpression="attribute_not_exists(requestId) OR ownerId = :owner",
                ExpressionAttributeValues={":owner": self._owner_id},
            )
        except Exception as error:  # 진행 상황은 보조 정보다. 저장이 안 돼도 답변은 계속 만든다
            print(f"진행 상황 저장 실패 (계속 진행): {error}")
            if "ConditionalCheckFailed" in str(error):
                self._table = None  # 남의 requestId다. 더 쓰지 않는다


def _plain(value: Any) -> Any:
    """DynamoDB가 돌려준 숫자(Decimal)를 정수로 바꾼다. 응답을 만드는 json.dumps는 Decimal을 모른다.
    여기 저장하는 숫자는 모두 정수다 (시각·걸린 시간은 밀리초, 실수 입력값은 글자로 저장한다)."""
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def read_progress(table, request_id: str, owner_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """요청한 사람의 진행 상황. 없거나 남의 것이면 None (남의 것도 '없음'으로 답해 있는지 알려 주지 않는다)."""
    if table is None or not owner_id or not REQUEST_ID.match(request_id or ""):
        return None
    item = table.get_item(Key={"requestId": request_id}).get("Item")
    if not item or item.get("ownerId") != owner_id:
        return None
    return _plain({
        "phase": item.get("phase", "thinking"),
        "steps": item.get("steps", []),
        "startedAt": item.get("startedAt"),
        "updatedAt": item.get("updatedAt"),
    })
