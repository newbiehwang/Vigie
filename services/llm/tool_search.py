"""도구 검색 (Anthropic tool search tool + defer_loading): 도구 정의를 필요할 때만 모델에 싣는다.

    요청마다 보내는 도구 (모두 보낸다. API가 검색하고 펼치려면 전체 정의가 필요하다)
      ├─ tool_search_tool_regex              ← 검색 도구. 지연하지 않는다
      ├─ describe_log_groups … (ALWAYS_LOADED) ← 자주 쓰는 CloudWatch 도구 4개. 처음부터 모델에 보인다 (+ 캐시 표시)
      └─ lookup_events, get_pricing, listEc2Instances … (defer_loading: true)
            └─ 모델이 검색하면 API가 찾은 도구만 펼쳐 모델에 보여 준다 (tool_reference → 전체 정의)

왜: 공식 MCP 서버(CloudWatch·문서·비용·CloudTrail·Pricing·IAM·네트워크)와 직접 둔 도구를 합치면 70개가 넘고,
정의만 수만 토큰이다. 질문 하나에 쓰는 도구는 몇 개뿐인데 반복(도구 호출)마다 전체 목록이 입력으로 들어간다.
도구가 30~50개를 넘으면 모델이 비슷한 도구 사이에서 고르기도 어려워진다 (Anthropic 문서).

- 검색은 Anthropic 서버에서 돈다 (server_tool_use → tool_search_tool_result). 응답 블록을 고치지 않고 그대로
  다음 요청에 돌려보내야 하고, 검색에는 tool_result를 보내지 않는다 (mcp_anthropic_client가 받은 그대로 이어 붙인다).
- 정규식 검색을 쓴다: 시스템 프롬프트가 도구 이름을 영역별로 적어 두어서, 모델이 이름으로 정확히 찾을 수 있다
  (예: "lookup_events", "listEc2Instances|getEc2CpuRanking"). 대소문자는 가리지 않는다.
- 캐시 표시(cache_control)는 지연한 도구에 붙일 수 없다 (400). 처음부터 싣는 마지막 도구에 붙인다.
  검색으로 찾은 도구 정의는 도구 목록이 아니라 대화(응답 블록) 안에 펼쳐지므로, 대화 쪽에도 캐시 표시를 둔다
  (with_cache_breakpoint). 그래야 같은 질문의 다음 반복에서 찾은 도구 정의를 캐시로 읽는다.
- 위험도·승인은 그대로다: 검색은 모델에게 정의를 보여 줄 뿐이고, 변경 도구는 찾아서 불러도 승인 요청만 만든다.
- 끄기: LLM Lambda 환경 변수 TOOL_SEARCH=off. 모델이 도구 검색을 지원하지 않아 400이 오면
  클라이언트가 그 모델에서는 끄고 모든 도구를 실어 다시 보낸다 (unsupported).
"""
import os
from typing import Any, Dict, List

SEARCH_TOOL = {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}

# 처음부터 모델에 보이는 도구 (Anthropic 권장: 가장 자주 쓰는 3~5개). Vigie 질문에서 가장 흔한 로그 조회와 알람.
# get_metric_data도 자주 쓰지만 정의가 약 1만 5천 자로 혼자서 나머지를 합친 것보다 커서 검색으로 싣는다
# (scripts/measure_tool_search.py size). 이름이 바뀌면(공식 서버 업데이트) 조용히 전부 지연되지 않도록
# 테스트가 tools/list에 있는지 확인한다
ALWAYS_LOADED = (
    "describe_log_groups",
    "execute_log_insights_query",
    "get_logs_insight_query_results",
    "get_active_alarms",
)

# 도구 검색을 켰을 때만 시스템 프롬프트 끝에 붙인다 (시스템 프롬프트의 <Tools>가 이름을 알려 준다)
SYSTEM_HINT = """
<Tool search>
Only these tools are loaded at first: """ + ", ".join(ALWAYS_LOADED) + """.
Every other tool named above (metrics, dashboards, documentation, cost, CloudTrail, Pricing, IAM, network, S3, EC2,
diagnosis, charts/diagrams and change tools) is loaded on demand: call tool_search_tool_regex with a pattern made of the exact
tool names you need (e.g. "lookup_events" or "listEc2Instances|getEc2CpuRanking|getEc2StatusChecks"), then call the
tools it returns. Search once for all the tools a task needs instead of one by one. Searching does not run anything.
</Tool search>
"""


def enabled_by_env() -> bool:
    """환경 변수 TOOL_SEARCH가 off가 아니면 켠다 (기본: 켬)."""
    return os.environ.get("TOOL_SEARCH", "on").strip().lower() not in ("off", "false", "0")


def build(tools: List[Dict[str, Any]], enabled: bool) -> List[Dict[str, Any]]:
    """Anthropic 형식 도구 목록(이름·설명·input_schema)에 검색 도구·지연 표시·캐시 표시를 붙인 목록.

    enabled=False면 예전과 같다: 모든 도구를 싣고 마지막 도구에 캐시 표시.
    """
    tools = [dict(tool) for tool in tools]
    if not enabled:
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return tools

    loaded = [tool for tool in tools if tool["name"] in ALWAYS_LOADED]
    deferred = [{**tool, "defer_loading": True} for tool in tools if tool["name"] not in ALWAYS_LOADED]
    # 캐시 기준점은 처음부터 싣는 도구에만 둘 수 있다. 지연한 도구는 캐시되는 앞부분(prefix)에 들어가지 않는다
    if loaded:
        loaded[-1]["cache_control"] = {"type": "ephemeral"}
    return [dict(SEARCH_TOOL), *loaded, *deferred]


def with_cache_breakpoint(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """마지막 사용자 메시지의 마지막 블록에 캐시 표시를 붙인 사본 (원래 대화는 바꾸지 않는다).

    도구를 부를 때마다 요청이 한 번 더 가는데, 앞 요청까지의 대화(도구 결과, 검색으로 찾은 도구 정의)를 캐시에서
    읽게 한다. 표시는 요청마다 하나만 둔다 (캐시 표시는 요청당 4개까지: 도구 목록 1 + 대화 1).
    마지막이 assistant(pause_turn으로 이어 가는 중)이거나 사고 블록이면 붙이지 않는다.
    """
    if not messages or messages[-1].get("role") != "user":
        return messages
    content = messages[-1].get("content")
    if isinstance(content, str):
        if not content:
            return messages
        content = [{"type": "text", "text": content}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content = list(content)
    else:
        return messages
    content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
    return messages[:-1] + [{**messages[-1], "content": content}]


def is_unsupported(status_code: int, text: str) -> bool:
    """이 모델(또는 경로)이 도구 검색을 받지 않아 거절한 응답인가. 그렇다면 모든 도구를 실어 다시 보낸다."""
    return status_code == 400 and ("tool_search" in text or "defer_loading" in text)


def searches(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """모델 응답 블록에서 도구 검색 기록: [{"query", "found": [도구 이름], "error"?}] (화면·측정용)."""
    results = {block.get("tool_use_id"): block.get("content") or {}
               for block in content if isinstance(block, dict) and block.get("type") == "tool_search_tool_result"}
    found = []
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "server_tool_use"
                and str(block.get("name", "")).startswith("tool_search_tool")):
            continue
        query = block.get("input") or {}
        result = results.get(block.get("id"), {})
        record = {
            "query": str(query.get("pattern") or query.get("query") or ""),
            "found": [ref.get("tool_name") for ref in result.get("tool_references") or []
                      if isinstance(ref, dict) and ref.get("tool_name")],
        }
        if result.get("type") == "tool_search_tool_result_error":
            record["error"] = str(result.get("error_code") or "error")
        found.append(record)
    return found
