"""변경 도구 실행 직전의 승인 재확인 (MCP Lambda 쪽).

LLM Lambda가 이미 승인을 확인하지만, MCP Lambda는 그 판단을 믿지 않고 승인 테이블을 직접 다시 본다
(다층 방어: LLM Lambda에 버그가 있거나 누군가 MCP를 직접 불러도, 승인되지 않은 변경은 실행되지 않는다).

    tools/call(변경 도구, _meta["vigie/actionId"]) ──▶ claim: 조건부 쓰기 한 번으로 확인과 표시를 함께 한다
        조건: 상태가 approved · 도구 이름이 같음 · 인자 해시가 같음 · 만료 전
        성공하면 상태를 executing으로 바꾼다 → 같은 작업 ID로는 다시 실행되지 않는다 (한 번만)
    실행이 끝나면 finish: executed 또는 failed와 결과 요약을 남긴다

인자 해시는 LLM Lambda(services/llm/approvals.py)와 같은 방법으로 만든다. 두 쪽이 다르면 모든 승인이 거절된다
(테스트가 두 구현의 결과를 비교한다).
"""
import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple

ACTION_ID_META = "vigie/actionId"  # tools/call params._meta: 승인된 작업 ID
PREVIEW_META = "vigie/preview"  # tools/call params._meta: 실행하지 않고 바뀔 내용만 본다 (승인 요청 카드용)
RESULT_LIMIT = 2000


def args_hash(tool: str, args: Dict[str, Any]) -> str:
    """도구 이름과 인자의 해시. 키 순서·공백과 무관하게 같은 값이면 같은 해시다."""
    canonical = json.dumps({"tool": tool, "args": args or {}}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalGate:
    """승인 테이블(vigie-pending-actions-<env>)을 보고 변경 도구 실행을 허락한다."""

    def __init__(self, table):
        self._table = table

    def claim(self, action_id: Optional[str], tool: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        """실행해도 되면 (True, "")를, 아니면 (False, 이유)를 돌려준다. 허락하면 그 자리에서 executing으로 바꾼다."""
        if not action_id:
            return False, "승인된 작업 ID가 없습니다. 변경 작업은 사용자가 승인한 뒤에만 실행됩니다."
        try:
            self._table.update_item(
                Key={"actionId": action_id},
                UpdateExpression="SET #status = :executing, executingAt = :now",
                ConditionExpression=("#status = :approved AND tool = :tool AND argsHash = :hash "
                                     "AND expiresAt > :now"),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":executing": "executing", ":approved": "approved", ":tool": tool,
                                           ":hash": args_hash(tool, args), ":now": int(time.time())},
            )
            return True, ""
        except Exception as error:
            if "ConditionalCheckFailed" not in str(error):
                raise
        return False, self._reason(action_id, tool, args)

    def finish(self, action_id: str, ok: bool, result: str) -> None:
        """실행 결과를 남긴다 (LLM Lambda가 승인 API의 응답과 이어지는 설명에 쓴다)."""
        text = result if len(result) <= RESULT_LIMIT else result[:RESULT_LIMIT - 1] + "…"
        self._table.update_item(
            Key={"actionId": action_id},
            UpdateExpression="SET #status = :status, #result = :result, finishedAt = :now",
            ConditionExpression="#status = :executing",
            ExpressionAttributeNames={"#status": "status", "#result": "result"},
            ExpressionAttributeValues={":status": "executed" if ok else "failed", ":result": text,
                                       ":executing": "executing", ":now": int(time.time())},
        )

    def _reason(self, action_id: str, tool: str, args: Dict[str, Any]) -> str:
        item = self._table.get_item(Key={"actionId": action_id}).get("Item")
        if not item:
            return "승인 요청을 찾을 수 없습니다."
        if item.get("tool") != tool or item.get("argsHash") != args_hash(tool, args):
            return "승인된 작업과 도구 또는 인자가 다릅니다."
        if item.get("status") != "approved":
            return f"승인된 상태가 아닙니다 (지금 상태: {item.get('status')})."
        return "승인 시간이 지났습니다. 다시 요청해 주세요."
