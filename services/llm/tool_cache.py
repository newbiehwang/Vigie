"""MCP 도구 목록을 DynamoDB에 저장해 두고, 새 컨테이너의 첫 질문에서 MCP를 기다리지 않고 모델을 부른다.

예전에는 새 컨테이너의 첫 질문이 MCP 세션 초기화 + tools/list를 끝낸 뒤에야 모델을 불렀다. MCP Lambda도 차가우면
공식 MCP 서버를 불러오느라 10초를 넘겼고, 도구가 필요 없는 "안녕"도 이것을 기다렸다.
이제는 (mcp_anthropic_client.AnthropicMCPClient._prepare_tools)
  1. 지난번에 받은 도구 목록을 여기서 읽어 바로 모델을 부르고,
  2. MCP 연결(초기화 + tools/list)은 그동안 뒤에서 한다.
  3. 모델이 도구를 부르면 그때 연결을 기다리고, 새로 받은 목록으로 바꾼다 (위험도는 새 목록으로 정한다).
  4. 새로 받은 목록이 저장된 것과 다르면(배포로 도구가 바뀜) 저장한 것을 바꾼다.
저장한 것이 없으면(처음 배포) 예전처럼 연결을 기다린 뒤 모델을 부르고, 받은 목록을 저장한다.

저장 위치: LLM Lambda가 이미 쓰는 진행 상황 테이블(vigie-llm-progress-<env>, llm_progress.py)에 항목 하나.
  {"requestId": "cache#mcp-tools", "mcpUrl": "...", "hash": "<sha256>", "tools": <gzip JSON>, "savedAt", "expiresAt"}
- 새 테이블·권한 없이 PutItem·GetItem만 쓴다. 키가 요청 ID(UUID) 모양이 아니고 ownerId가 없어
  진행 상황 조회(GET /llm1/progress, read_progress)로는 읽을 수 없다.
- 다시 받으면 되는 값이라 테이블의 TTL(expiresAt)을 그대로 쓴다: 오래 쓰지 않으면 지워지고, 다음 연결이 다시 저장한다.
- 도구 설명이 길어 JSON이 수십~수백 KB라 gzip으로 줄여 저장한다 (DynamoDB 항목은 400KB까지).
"""
import gzip
import hashlib
import json
import time
from typing import Any, Dict, List, Optional

CACHE_KEY = "cache#mcp-tools"
# 저장한 목록을 이 기간 쓰지(다시 저장하지) 않으면 TTL로 지워진다
TTL_SECONDS = 7 * 24 * 3600
# 목록이 같아도 이만큼 지났으면 다시 저장해 TTL을 늘린다 (컨테이너가 뜰 때마다 쓰지 않게)
REFRESH_SECONDS = 24 * 3600
# 줄인 뒤에도 이보다 크면 저장하지 않는다 (DynamoDB 항목 한도 400KB에 다른 속성 몫을 남긴다)
MAX_STORED_BYTES = 350 * 1024


def tools_hash(tools: List[Dict[str, Any]]) -> str:
    """도구 목록이 바뀌었는지 비교할 지문 (키 순서와 상관없이 같은 목록이면 같다)."""
    raw = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ToolCache:
    def __init__(self, table, mcp_url: str):
        self.table = table
        self.mcp_url = mcp_url
        self._stored: Optional[Dict[str, Any]] = None  # 마지막으로 읽거나 쓴 {hash, savedAt} (같은 목록을 또 쓰지 않게)

    def load(self) -> Optional[List[Dict[str, Any]]]:
        """저장해 둔 도구 목록. 없거나, 다른 MCP 서버의 것이거나, 읽지 못하면 None (그때는 연결을 기다린다)."""
        try:
            item = self.table.get_item(Key={"requestId": CACHE_KEY}).get("Item")
            if not item or item.get("mcpUrl") != self.mcp_url:
                return None
            tools = json.loads(gzip.decompress(bytes(item["tools"])).decode("utf-8"))
            if not isinstance(tools, list) or not tools:
                return None
            self._stored = {"hash": item.get("hash"), "savedAt": int(item.get("savedAt", 0))}
            return tools
        except Exception as error:  # 저장소 문제로 질문이 실패하지 않게 한다 (연결을 기다리는 예전 길로 간다)
            print(f"저장한 도구 목록을 읽지 못했습니다: {error}")
            return None

    def save(self, tools: List[Dict[str, Any]], now: Optional[float] = None) -> bool:
        """새로 받은 목록을 저장한다. 저장한 것과 같고 오래되지 않았으면 쓰지 않는다. 썼으면 True."""
        if not tools:
            return False
        now = int(now or time.time())
        digest = tools_hash(tools)
        stored = self._stored
        if stored and stored.get("hash") == digest and now - stored.get("savedAt", 0) < REFRESH_SECONDS:
            return False
        body = gzip.compress(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
        if len(body) > MAX_STORED_BYTES:
            print(f"도구 목록이 너무 커서 저장하지 않습니다 ({len(body)}바이트)")
            return False
        try:
            self.table.put_item(Item={"requestId": CACHE_KEY, "mcpUrl": self.mcp_url, "hash": digest,
                                      "tools": body, "savedAt": now, "expiresAt": now + TTL_SECONDS})
        except Exception as error:  # 다음 연결 때 다시 저장하면 된다
            print(f"도구 목록을 저장하지 못했습니다: {error}")
            return False
        self._stored = {"hash": digest, "savedAt": now}
        return True
