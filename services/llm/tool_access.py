"""그룹별로 쓸 수 있는 도구 (LLM Lambda 쪽, docs/threat-model.md R8).

모든 사용자가 같은 MCP 역할로 AWS를 조회한다. 그래서 가입만 하면 계정 전체의 CloudTrail·IAM·네트워크 구조를
물어볼 수 있었다. 이제 관리자 전용 도구(기준: mcp/lambda_mcp/risk.py의 ADMIN_ONLY)는 관리자(admins 그룹)만 쓴다.

    요청 ──▶ 요청자의 권한 (role_of: ID 토큰의 cognito:groups에 admins가 있으면 admin, 아니면 member)
             ├─ ① 모델에게 주는 도구 목록에서 뺀다 (visible). 도구 검색으로도 찾을 수 없다
             ├─ ② 시스템 프롬프트에 "이 도구는 쓸 수 없다, 관리자에게 문의하라고 안내하라"를 덧붙인다 (MEMBER_NOTE)
             ├─ ③ 모델이 이름으로 불러도 거절한다 (can_use). 거절한 시도는 도구 호출 실패로 감사 로그에 남는다
             └─ ④ MCP에 관리자의 호출이라고 알린다 (call_meta). MCP 서버는 이 표시가 없으면 관리자 전용 도구를 거절한다

- 도구마다 누가 쓸 수 있는지는 MCP tools/list가 알려 준다 (_meta["vigie/access"]). 위험도(approvals.risk_of)와 같은
  방식이다. 표시가 없는 도구(예전 MCP 서버의 목록 등)는 관리자 전용으로 본다 (안전하게 실패).
- Slack 봇 요청은 Cognito 그룹이 없어 일반 사용자로 본다.
- 그룹은 ID 토큰을 받은 때의 것이다: 그룹을 바꾸면 토큰이 갱신될 때(최대 1시간)나 다시 로그인할 때 적용된다.
"""
from typing import Any, Dict, Iterable, List, Optional

ADMIN_GROUP = "admins"  # audit.ADMIN_GROUP과 같은 그룹 (감사 로그도 이 그룹만 본다)
ADMIN = "admin"
MEMBER = "member"
ALL = "all"

ACCESS_META = "vigie/access"  # MCP tools/list의 도구 정의 _meta (mcp/lambda_mcp/risk.py의 ACCESS_META_KEY)
ROLE_META = "vigie/role"  # MCP tools/call params._meta (mcp/lambda_mcp/risk.py의 ROLE_META_KEY)

# 일반 사용자 요청의 시스템 프롬프트 끝에 붙인다. 시스템 프롬프트의 <Tools>는 모든 도구를 적어 두므로(캐시되는 본문),
# 쓸 수 없는 도구를 알리고 어떻게 답할지 정해 준다
MEMBER_NOTE = """
<Access>
This user is not an administrator. CloudTrail (lookup_events), IAM, network (VPC/ENI/flow logs/path trace),
S3 bucket security checks (checkS3BucketSecurity) and S3 object listings (listS3Objects) are available to
administrators (the "admins" group) only, and are not loaded for this user. If the user asks for them, do not try to
find or call them and do not guess the answer: say in Korean that only administrators can view it and suggest asking an
administrator. Answer the rest of the question with the tools you have.
</Access>
"""

DENIED_MESSAGE = ("'{name}'은(는) 관리자(admins 그룹)만 쓸 수 있는 도구라 부르지 않았습니다. "
                  "사용자에게 관리자만 볼 수 있는 정보라고 알리고, 관리자에게 문의하라고 안내하세요.")


def role_of(groups: Optional[Iterable[str]]) -> str:
    """요청자의 권한: admins 그룹이면 admin, 아니면(그룹 없음·결정자·Slack) member."""
    return ADMIN if ADMIN_GROUP in (groups or []) else MEMBER


def access_of(tool_definition: Optional[Dict[str, Any]]) -> str:
    """MCP 도구 정의의 '쓸 수 있는 사람'. 정의나 표시가 없으면 관리자 전용으로 본다."""
    return ((tool_definition or {}).get("_meta") or {}).get(ACCESS_META) or ADMIN


def can_use(tool_definition: Optional[Dict[str, Any]], role: str) -> bool:
    return role == ADMIN or access_of(tool_definition) == ALL


def visible(tools: List[Dict[str, Any]], role: str) -> List[Dict[str, Any]]:
    """이 권한으로 모델에게 보여 줄 도구 정의."""
    return [tool for tool in tools if can_use(tool, role)]


def labeled(tools: List[Dict[str, Any]]) -> bool:
    """이 도구 목록에 '쓸 수 있는 사람' 표시가 있나. 이 기능 전에 저장한 목록(tool_cache)이면 없다.
    그런 목록으로 모델을 부르면 일반 사용자에게 도구가 하나도 보이지 않으므로, 새로 받을 때까지 기다린다."""
    return any(ACCESS_META in ((tool.get("_meta") or {}) if isinstance(tool, dict) else {}) for tool in tools or [])


def call_meta(role: str) -> Optional[Dict[str, str]]:
    """MCP tools/call에 붙일 _meta. 관리자만 표시를 붙인다 (없으면 MCP가 관리자 전용 도구를 거절한다)."""
    return {ROLE_META: ADMIN} if role == ADMIN else None


def denied_result(name: str) -> Dict[str, Any]:
    """거절한 도구 호출의 결과 (MCP 형식). 모델이 읽고 사용자에게 설명한다."""
    return {"isError": True, "content": [{"type": "text", "text": DENIED_MESSAGE.format(name=name)}]}
