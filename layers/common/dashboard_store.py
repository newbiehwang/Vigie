"""홈 대시보드의 구역별 저장 (DynamoDB wga-dashboard-<env>, 키는 section 하나).
    {"section": "alarms", "data": "<JSON>", "ok": true, "collectedAt": 1790000000, "lastSuccessAt": 1790000000}
    {"section": "cost", "data": "<지난번 성공 값>", "ok": false, "error": "…", "collectedAt": …, "lastSuccessAt": …}
- 구역마다 따로 저장한다: 한 구역이 실패해도 다른 구역은 새 값으로 보인다.
- 실패하면 지난번 성공 값(data)과 그 때(lastSuccessAt)는 그대로 두고, 실패했다는 것과 까닭만 덧붙인다.
  화면은 '모으지 못함 · 마지막 성공 3시간 전'으로 보인다.
- 읽는 쪽(services/llm의 GET /dashboard)은 get_all로 모든 구역을 한 번에 읽는다 (구역은 6개뿐이라 Scan 한 번).
- 쓰는 수집 Lambda(services/dashboard)와 읽는 LLM Lambda가 함께 쓰므로 공통 레이어(common)에 둔다.
"""
import json
import time
from typing import Any, Dict, Optional

ERROR_LIMIT = 500  # 저장할 오류 글자 수


class DashboardStore:
    def __init__(self, table):
        self.table = table

    def put_section(self, section: str, data: Dict[str, Any], now: Optional[int] = None) -> None:
        now = now or int(time.time())
        self.table.put_item(Item={"section": section, "data": json.dumps(data, ensure_ascii=False),
                                  "ok": True, "collectedAt": now, "lastSuccessAt": now})

    def put_error(self, section: str, error: str, now: Optional[int] = None) -> None:
        """실패: 지난번 성공 값은 두고 실패 표시만 바꾼다 (항목이 없으면 새로 만든다)."""
        now = now or int(time.time())
        self.table.update_item(
            Key={"section": section},
            UpdateExpression="SET ok = :false, #error = :error, collectedAt = :now",
            ExpressionAttributeNames={"#error": "error"},
            ExpressionAttributeValues={":false": False, ":error": str(error)[:ERROR_LIMIT], ":now": now})

    def collected_at(self, section: str) -> Optional[int]:
        """마지막으로 모은(성공·실패 모두) 때. 이벤트가 몰릴 때 너무 자주 모으지 않게 쓴다."""
        item = self.table.get_item(Key={"section": section}, ProjectionExpression="collectedAt").get("Item")
        return int(item["collectedAt"]) if item and "collectedAt" in item else None

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """모든 구역: {section: {"data": dict|None, "ok", "error", "collectedAt", "lastSuccessAt"}}"""
        sections: Dict[str, Dict[str, Any]] = {}
        kwargs: Dict[str, Any] = {}
        while True:
            page = self.table.scan(**kwargs)
            for item in page.get("Items", []):
                sections[item["section"]] = {
                    "data": json.loads(item["data"]) if item.get("data") else None,
                    "ok": bool(item.get("ok")),
                    "error": item.get("error") if not item.get("ok") else None,
                    "collectedAt": int(item.get("collectedAt", 0)),
                    "lastSuccessAt": int(item["lastSuccessAt"]) if item.get("lastSuccessAt") is not None else None,
                }
            if not page.get("LastEvaluatedKey"):
                return sections
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
