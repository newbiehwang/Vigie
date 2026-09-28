"""도구 위험도 목록: 어떤 도구가 AWS를 바꾸는지의 기준은 여기 한 곳이다.

    tools/list ──▶ 도구 정의에 위험도를 붙여 내보낸다 (MCP 표준 annotations + _meta["vigie/risk"])
                     └─▶ LLM Lambda가 읽고, 변경 도구면 실행하지 않고 승인을 기다린다
    tools/call ──▶ 변경 도구면 승인된 작업인지 다시 확인한 뒤에만 실행한다 (lambda_mcp.py)

위험도
- read     : 조회만 한다.
- artifact : AWS 리소스는 바꾸지 않고 결과물(다이어그램 이미지, 차트 주소)만 만든다.
- write    : AWS 리소스를 바꾼다. 사람이 승인해야 실행된다.

목록에 없는 도구는 write로 본다 (안전하게 실패). 공식 MCP 서버를 올리다가 새 도구가 생겨도 승인 없이는 돌지 않는다.
공식 서버들은 MCP 표준의 annotations(readOnlyHint 등)를 비워 두어 그대로 믿을 수 없다. 그래서 여기서 채운다.

누가 쓸 수 있나 (_meta["vigie/access"], docs/threat-model.md R8)
- all   : 로그인한 사용자 누구나. 서비스를 운영하는 데 필요한 조회 (로그·지표·비용·EC2·문서·차트 등)
- admin : 관리자(Cognito admins 그룹)만. 계정을 정찰할 때 쓸 수 있는 정보다 (ADMIN_ONLY)
            CloudTrail(누가·언제·어디서·무엇을), IAM(사람·역할·권한), 네트워크(내부 IP·보안 그룹·트래픽),
            S3 보안 점검(어느 버킷이 공개인지)과 객체 목록(파일 이름)
    tools/list ──▶ 도구마다 access를 붙인다. LLM Lambda는 요청자가 관리자가 아니면 admin 도구를 모델에게 보이지 않고,
                     모델이 이름으로 불러도 거절한다 (services/llm/tool_access.py)
    tools/call ──▶ admin 도구는 LLM Lambda가 _meta["vigie/role"]="admin"을 붙여 보낸 호출만 실행한다 (lambda_mcp.py)
위험도 목록에 없는 도구는 admin으로 본다 (write와 같은 이유로 안전하게 실패).
"""
from typing import Any, Dict

READ = "read"
ARTIFACT = "artifact"
WRITE = "write"

RISK_META_KEY = "vigie/risk"

ALL = "all"
ADMIN = "admin"
ACCESS_META_KEY = "vigie/access"  # tools/list의 도구 정의 _meta: 누가 쓸 수 있나
ROLE_META_KEY = "vigie/role"  # tools/call params._meta: LLM Lambda가 확인한 요청자의 권한 (admin이면 관리자)

TOOL_RISK: Dict[str, str] = {
    # AWS 공식 CloudWatch MCP 서버
    "describe_log_groups": READ,
    "analyze_log_group": READ,
    "execute_log_insights_query": READ,  # 쿼리 시작도 조회다 (StartQuery)
    "get_logs_insight_query_results": READ,
    "cancel_logs_insight_query": READ,  # 자기가 시작한 쿼리를 멈출 뿐 리소스는 그대로
    "get_metric_data": READ,
    "get_metric_metadata": READ,
    "analyze_metric": READ,
    "get_recommended_metric_alarms": READ,
    "get_active_alarms": READ,
    "get_alarm_history": READ,
    # AWS 공식 문서 MCP 서버
    "search_documentation": READ,
    "read_documentation": READ,
    "read_sections": READ,
    "search_table": READ,
    "recommend": READ,
    # AWS 공식 Billing and Cost Management MCP 서버 (Cost Explorer)
    "cost-explorer": READ,
    # AWS 공식 CloudTrail MCP 서버 (최근 90일 관리 이벤트 조회만. Lake 도구는 official.py에서 뺐다)
    "lookup_events": READ,
    # AWS 공식 Pricing MCP 서버 (공개 가격표 조회. 파일을 읽거나 쓰는 도구는 official.py에서 뺐다)
    "get_pricing_service_codes": READ,
    "get_pricing_service_attributes": READ,
    "get_pricing_attribute_values": READ,
    "get_pricing": READ,
    # AWS 공식 IAM MCP 서버 (조회만. 변경 도구 17개는 official.py에서 뺐다)
    "list_users": READ,
    "get_user": READ,
    "list_roles": READ,
    "list_policies": READ,
    "get_managed_policy_document": READ,
    "simulate_principal_policy": READ,  # 정책 평가만 하고 권한을 바꾸지 않는다
    "list_groups": READ,
    "get_group": READ,
    "get_user_policy": READ,
    "get_role_policy": READ,
    "list_user_policies": READ,
    "list_role_policies": READ,
    # AWS 공식 네트워크 MCP 서버 (모든 도구가 조회. VPC·ENI·경로 추적만 붙였다)
    "get_path_trace_methodology": READ,
    "find_ip_address": READ,
    "get_eni_details": READ,
    "list_vpcs": READ,
    "get_vpc_network": READ,
    "get_vpc_flow_logs": READ,  # Logs Insights 쿼리로 흐름 로그를 읽는다 (조회. 스캔한 양만큼 과금)
    # 직접 둔 도구 (app.py, 이름은 camelCase로 등록된다)
    "listCloudwatchDashboards": READ,
    "getDashboardSummary": READ,
    # S3 조회 (객체 내용은 읽지 않는다)
    "listS3Buckets": READ,
    "checkS3BucketSecurity": READ,
    "getS3BucketSize": READ,
    "listS3Objects": READ,
    # EC2 조회 (사용자 데이터·콘솔 출력·Windows 암호는 읽지 않는다)
    "listEc2Instances": READ,
    "getEc2CpuRanking": READ,
    "getEc2StatusChecks": READ,
    "findEc2Waste": READ,
    # 서비스별 진단 절차 (lambda_mcp/diagnose.py). 조회 API만 부른다
    "diagnoseService": READ,
    "getDiagramCodeExamples": READ,
    "listAvailableDiagramIcons": READ,
    "generateArchitectureDiagram": ARTIFACT,  # 다이어그램 버킷에 이미지를 올린다
    "generateLineChart": ARTIFACT,
    "generateBarChart": ARTIFACT,
    "generatePieChart": ARTIFACT,
    "generateScatterChart": ARTIFACT,
    "generateAreaChart": ARTIFACT,
    "generateWordCloudChart": ARTIFACT,
    "generateRadarChart": ARTIFACT,
    "generateColumnChart": ARTIFACT,
    "generateHistogramChart": ARTIFACT,
    "generateTreemapChart": ARTIFACT,
    "generateDualAxesChart": ARTIFACT,
    "generateMindMap": ARTIFACT,
    "generateNetworkGraph": ARTIFACT,
    "generateFlowDiagram": ARTIFACT,
    "generateFishboneDiagram": ARTIFACT,
    # 변경 도구 (app.py). 승인된 작업만 실행된다
    "setLogRetention": WRITE,
    "setAlarmActions": WRITE,
    "setEc2InstanceState": WRITE,  # 이 리전의 모든 인스턴스 (사람의 승인과 MCP 재확인으로 통제)
    "enableS3PublicAccessBlock": WRITE,  # 보안을 강화하는 방향만. 끄는 도구는 없다
}

# 관리자만 쓰는 도구. 모두 조회 도구지만, 일반 사용자에게는 서비스 운영에 필요 없고 계정 정찰에 쓰일 수 있는 정보다.
# 로그·지표·비용·EC2 상태·버킷 이름과 크기는 운영 조회의 핵심이라 모든 사용자에게 둔다 (docs/threat-model.md R8)
ADMIN_ONLY = frozenset({
    # CloudTrail: 계정 전체의 API 호출 기록 (누가·언제·어느 IP에서·무엇을). Vigie 감사 로그와 같은 이유로 관리자만
    "lookup_events",
    # IAM: 사용자·역할·정책. 어느 역할이 무엇을 할 수 있는지 드러난다
    "list_users", "get_user", "list_roles", "list_policies", "get_managed_policy_document",
    "simulate_principal_policy", "list_groups", "get_group", "get_user_policy", "get_role_policy",
    "list_user_policies", "list_role_policies",
    # 네트워크: 내부 IP, 보안 그룹·NACL·라우팅, 트래픽 흐름
    "get_path_trace_methodology", "find_ip_address", "get_eni_details", "list_vpcs", "get_vpc_network",
    "get_vpc_flow_logs",
    # S3: 어느 버킷이 공개인지(약점 목록)와 버킷 안 파일 이름
    "checkS3BucketSecurity", "listS3Objects",
    # 진단: 위의 CloudTrail·보안 그룹·NACL·버킷 공개 여부를 한꺼번에 읽는다
    "diagnoseService",
})

# MCP 표준 annotations (2025-03-26 이후). 클라이언트에게 주는 힌트이고, 실제 통제는 승인 확인과 IAM이 한다
_ANNOTATIONS = {
    READ: {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    ARTIFACT: {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    WRITE: {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
}


def risk_of(name: str) -> str:
    """도구의 위험도. 목록에 없으면 write (안전하게 실패)."""
    return TOOL_RISK.get(name, WRITE)


def needs_approval(name: str) -> bool:
    return risk_of(name) == WRITE


def access_of(name: str) -> str:
    """누가 쓸 수 있나. 관리자 전용 목록에 있거나 위험도 목록에 없는 도구(모르는 도구)면 admin."""
    return ADMIN if name in ADMIN_ONLY or name not in TOOL_RISK else ALL


def is_allowed(name: str, meta: Dict[str, Any]) -> bool:
    """tools/call을 실행해도 되나: 관리자 전용 도구는 LLM Lambda가 관리자의 요청이라고 붙여 보낸 호출만."""
    return access_of(name) == ALL or (meta or {}).get(ROLE_META_KEY) == ADMIN


def annotate(tool: Dict[str, Any]) -> Dict[str, Any]:
    """tools/list에 내보낼 도구 정의에 위험도와 쓸 수 있는 사람을 붙인 사본."""
    name = tool.get("name", "")
    risk = risk_of(name)
    return {
        **tool,
        "annotations": {**tool.get("annotations", {}), **_ANNOTATIONS[risk]},
        "_meta": {**tool.get("_meta", {}), RISK_META_KEY: risk, ACCESS_META_KEY: access_of(name)},
    }
