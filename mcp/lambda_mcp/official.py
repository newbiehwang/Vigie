"""AWS 공식 MCP 서버(awslabs)의 도구를 이 Lambda MCP 서버에 붙인다.

공식 서버는 로컬 프로세스로 띄워(stdio) 쓰도록 만들어졌다. Lambda에서는 프로세스를 따로 띄우지 않고
서버 객체를 그대로 import해서, fastmcp의 in-memory 클라이언트로 같은 프로세스 안에서 부른다.

    LLM Lambda ──HTTP(JSON-RPC)──▶ LambdaMCPServer (lambda_mcp.py, 세션·전송은 그대로)
                                      ├─ 직접 만든 도구 (app.py의 @mcp_server.tool)
                                      └─ OfficialTools ──in-memory──▶ 공식 서버 객체 (CloudWatch, 문서, Cost Explorer, CloudTrail, Pricing, IAM, 네트워크)

in-memory 클라이언트로 부르는 이유
- Cost Explorer 서버(fastmcp)는 MCP 세션이 있어야 도구가 돈다 (ctx.info가 세션을 쓴다). 서버 객체의
  call_tool을 바로 부르면 "session is not available"로 실패한다. 클라이언트는 세션을 만들어 준다.
- 도구 안의 예외가 isError 결과로 돌아온다 (서버 객체를 바로 부르면 예외가 그대로 올라온다).
- CloudWatch·문서 서버(MCP SDK MCPServer)도 같은 방법으로 부를 수 있어 한 가지 경로로 모은다.

Lambda 핸들러는 동기 함수이고 공식 도구는 async다. 컨테이너마다 이벤트 루프 하나를 만들어 계속 쓴다
(요청마다 새 루프를 만들면 루프에 묶인 자원이 다음 요청에서 깨질 수 있다).

공식 서버는 도구 목록이 처음 필요할 때 불러온다 (Lambda 초기화 단계에서 불러오지 않는다)
- 공식 서버는 무겁다. CloudWatch 서버는 메트릭 분석 때문에 pandas·scipy·statsmodels까지 불러온다.
  Lambda는 컨테이너 이미지의 파일을 처음 읽을 때 받아 오므로, 콜드 스타트에서는 이 import가 10초를 넘는다.
- Lambda 초기화 단계는 10초로 제한되어 있고 늘릴 수 없다. 넘으면 그때까지 한 일을 버리고 첫 요청에서
  초기화를 처음부터 다시 한다. 초기화 단계에서 불러오면 10초를 버리고 같은 import를 한 번 더 하게 된다.
- 첫 요청에서 불러오면 한 번만 한다. 그 뒤로는 컨테이너가 살아 있는 동안 불러온 서버를 그대로 쓴다.

도구 목록 스냅샷 (official_tools.json, 이미지를 만들 때 쓴다: Dockerfile)
- 예전에는 tools/list가 처음 올 때 공식 서버 7개를 모두 불러와 목록을 모았다. 질문이 "안녕"이어도 LLM Lambda는
  질문 전에 도구 목록을 받으므로, MCP Lambda가 차가우면 첫 질문이 10초 넘게 기다렸다.
- 이제 이미지를 만들 때 도구 정의를 파일로 적어 두고, tools/list는 그 파일을 읽는다 (아무것도 불러오지 않는다).
- 도구를 실제로 부를 때는 그 도구가 속한 서버 하나만 불러온다 (예: 로그 조회면 CloudWatch 서버만).
- 파일에는 리전·숨긴 인자 보정 전의 스키마를 적고, 읽을 때 이 Lambda의 리전으로 보정한다 (빌드 리전과 달라도 맞게).
- 파일이 없거나 읽지 못하면 예전처럼 모두 불러와 모은다 (로컬 실행·테스트, 빌드 때 스냅샷을 못 만든 경우).
"""
import asyncio
import copy
import json
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 공식 서버의 도구 중 이 서비스에서 빼는 것 (이유와 함께). 도구 설명이 길어서 도구 하나가 질문마다
# 수천 토큰을 쓰므로, 쓰임이 겹치거나 권한·전제가 맞지 않는 도구는 넣지 않는다.
EXCLUDED_TOOLS = {
    # CloudWatch PromQL: OpenTelemetry로 보낸 메트릭용이고, 호출하는 API(monitoring SigV4)의 IAM 권한이 문서에 없다
    "execute_promql_query": "PromQL",
    "execute_promql_range_query": "PromQL",
    "get_promql_label_values": "PromQL",
    "get_promql_series": "PromQL",
    "get_promql_labels": "PromQL",
    # 로그 인덱스 추천: 운영 질의응답보다 로그 설정 관리용이고 계정 정책 조회 권한이 더 필요하다
    "recommend_indexes_loggroup": "로그 인덱스 추천",
    "recommend_indexes_account": "로그 인덱스 추천",
    # 여러 쿼리 일괄 실행: execute_log_insights_query와 쓰임이 겹친다
    "execute_cwl_insights_batch": "execute_log_insights_query와 중복",
    # CloudTrail Lake: 스캔한 데이터만큼 쿼리 비용이 들고 이벤트 데이터 저장소(유료)가 있어야 한다.
    # 최근 90일 관리 이벤트 조회(lookup_events, 무료)만 쓴다
    "lake_query": "CloudTrail Lake (유료)",
    "get_query_status": "CloudTrail Lake (유료)",
    "get_query_results": "CloudTrail Lake (유료)",
    "list_event_data_stores": "CloudTrail Lake (유료)",
    # Pricing: 로컬 파일 경로를 받아 연다. Lambda 안에서는 자격 증명이 든 파일(/proc/self/environ 등)까지
    # 읽을 수 있어, 모델이 경로를 고르는 도구는 두지 않는다 (보안)
    "analyze_cdk_project": "로컬 파일 읽기 (보안)",
    "analyze_terraform_project": "로컬 파일 읽기 (보안)",
    # Pricing: 보고서를 파일로 쓴다. 보고서는 모델이 답변으로 쓰면 된다
    "generate_cost_report": "파일 쓰기",
    # Pricing: 수백 MB짜리 가격 파일의 내려받기 주소라 대화에 쓸모가 없다 (pricing:ListPriceLists 권한도 필요)
    "get_price_list_urls": "가격 파일 주소",
    # Pricing: Bedrock 설계 예시 글. 운영 질의응답과 관계가 적고 도구 목록만 늘린다
    "get_bedrock_patterns": "운영 질문과 무관",
    # IAM: 조회(목록·조회·인라인 정책·권한 시뮬레이션)만 쓴다. 사용자·역할·그룹 생성과 삭제, 정책 붙이기,
    # 액세스 키 발급 같은 변경 도구는 목록에서 뺀다. 막는 곳은 네 겹이다:
    #   1. 여기서 뺀다 → 2. 서버 자체의 읽기 전용 모드를 켠다 (load_default_servers)
    #   3. 위험도 목록에 없어 MCP가 변경 도구로 보고 거절한다 (risk.py) → 4. IAM 쓰기 권한이 없다 (llm.yaml)
    "add_user_to_group": "IAM 변경",
    "attach_group_policy": "IAM 변경",
    "attach_user_policy": "IAM 변경",
    "create_access_key": "IAM 변경",
    "create_group": "IAM 변경",
    "create_role": "IAM 변경",
    "create_user": "IAM 변경",
    "delete_access_key": "IAM 변경",
    "delete_group": "IAM 변경",
    "delete_role_policy": "IAM 변경",
    "delete_user": "IAM 변경",
    "delete_user_policy": "IAM 변경",
    "detach_group_policy": "IAM 변경",
    "detach_user_policy": "IAM 변경",
    "put_role_policy": "IAM 변경",
    "put_user_policy": "IAM 변경",
    "remove_user_from_group": "IAM 변경",
    # 네트워크: EC2가 놓인 네트워크(VPC·서브넷·보안 그룹·NACL·라우팅·ENI·VPC 흐름 로그)와 경로 추적만 쓴다.
    # Cloud WAN·Transit Gateway·Network Firewall·VPN 도구는 이 계정에 없는 서비스라 도구 설명과 권한만 늘린다 (필요하면 추가)
    "detect_cwan_inspection": "이 계정에 없는 네트워크 서비스",
    "get_all_cwan_routes": "이 계정에 없는 네트워크 서비스",
    "get_cwan_routes": "이 계정에 없는 네트워크 서비스",
    "get_cwan_attachment": "이 계정에 없는 네트워크 서비스",
    "get_cwan": "이 계정에 없는 네트워크 서비스",
    "get_cwan_logs": "이 계정에 없는 네트워크 서비스",
    "get_cwan_peering": "이 계정에 없는 네트워크 서비스",
    "list_cwan_peerings": "이 계정에 없는 네트워크 서비스",
    "list_core_networks": "이 계정에 없는 네트워크 서비스",
    "simulate_cwan_route_change": "이 계정에 없는 네트워크 서비스",
    "get_firewall_rules": "이 계정에 없는 네트워크 서비스",
    "get_firewall_flow_logs": "이 계정에 없는 네트워크 서비스",
    "list_firewalls": "이 계정에 없는 네트워크 서비스",
    "detect_tgw_inspection": "이 계정에 없는 네트워크 서비스",
    "get_all_tgw_routes": "이 계정에 없는 네트워크 서비스",
    "get_tgw": "이 계정에 없는 네트워크 서비스",
    "get_tgw_routes": "이 계정에 없는 네트워크 서비스",
    "get_tgw_flow_logs": "이 계정에 없는 네트워크 서비스",
    "list_tgw_peerings": "이 계정에 없는 네트워크 서비스",
    "list_transit_gateways": "이 계정에 없는 네트워크 서비스",
    "list_vpn_connections": "이 계정에 없는 네트워크 서비스",
}

# region 인자를 이 Lambda의 리전으로 채우는 도구. 모델이 region을 생략하면 이 Lambda의 리전을 쓰게 한다.
# (CloudWatch 도구들은 이미 AWS_REGION을 기본으로 쓴다)
# - CloudTrail lookup_events: 기본값이 버지니아 북부 리전으로 박혀 있다. 조회는 리전별이라 다른 리전을 보면 '기록 없음'이 된다
# - 네트워크 도구: list_vpcs 등은 region이 기본값 없는 필수 인자라, 모델이 빠뜨리면 검증 오류가 난다
REGION_FROM_ENV_TOOLS = {"lookup_events", "find_ip_address", "get_eni_details", "list_vpcs", "get_vpc_network",
                         "get_vpc_flow_logs"}

# 공식 서버의 결함 보정: 도구 스키마에서 빼고, 부를 때 대신 채워 넣는 인자.
# IAM 1.1.1의 list_users·get_user는 ctx의 타입을 MCP 문맥(Context)이 아니라 CallToolResult로 잘못 적어 두어,
# MCP SDK가 ctx를 모델이 채워야 할 필수 입력값으로 만든다 (그대로 두면 부를 때마다 검증 오류).
# 함수 안에서는 ctx를 쓰지 않으므로 CallToolResult 형식의 빈 값을 넣는다.
# 원래 스키마에 그 인자가 있을 때만 보정하므로, 공식 서버가 고쳐지면 아무 일도 하지 않는다.
# 제보: https://github.com/awslabs/mcp/issues/4675 (이전 제보 #1463·수정 PR #1464는 고쳐지지 않고 자동으로 닫혔다).
# 공식 서버가 고쳐진 버전으로 올리면 이 항목을 지운다
HIDDEN_ARGUMENTS: Dict[str, Dict[str, Any]] = {
    "list_users": {"ctx": {"content": []}},
    "get_user": {"ctx": {"content": []}},
}

# 네트워크 서버의 profile_name(다른 계정의 AWS 프로필 이름)은 Lambda에 프로필 설정 파일이 없어 쓸 수 없다.
# 모델이 헛되이 채우지 않게 스키마에서 숨기고 비워 둔다 (이 Lambda의 역할로 조회한다)
HIDDEN_ARGUMENTS.update({name: {"profile_name": None} for name in (
    "find_ip_address", "get_eni_details", "list_vpcs", "get_vpc_network", "get_vpc_flow_logs")})


def _inline_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """스키마의 $ref를 $defs의 내용으로 바꿔 넣고 $defs를 없앤다.

    공식 도구 일부(get_metric_data 등)는 pydantic이 만든 $defs/$ref를 쓴다. LLM Lambda는 도구 스키마를
    Anthropic·Bedrock 형식으로 옮길 때 properties만 가져가므로, 참조가 남아 있으면 가리킬 곳이 없어진다.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: Tuple[str, ...] = ()) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/")[-1]
                if name in seen or name not in defs:  # 자기 자신을 가리키면 더 풀지 않는다
                    return {"type": "object"}
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return {**resolve(defs[name], seen + (name,)), **extra}
            return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        return node

    return resolve(copy.deepcopy(schema))


def _keeping_root_logger(importer: Callable[[], Any]) -> Any:
    """importer를 부르는 동안 바뀐 기본(root) 로거 설정을 되돌린다.

    공식 서버를 불러오면 기본 로거가 바뀐다.
    - MCP SDK의 MCPServer는 만들어질 때 logging.basicConfig(level=INFO)로 핸들러를 붙인다 (대부분의 공식 서버)
    - 네트워크 서버는 import할 때 logging.basicConfig(level=DEBUG)를 부른다. boto3·botocore의 디버그 로그
      (요청·응답 내용)까지 쏟아진다
    Lambda는 런타임이 기본 로거에 이미 핸들러를 붙여 두어 basicConfig가 아무 일도 하지 않지만, 핸들러가 없는
    곳(로컬 실행·테스트·컨테이너)에서는 그대로 바뀐다. 어디서 돌든 같게 되돌린다."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        return importer()
    finally:
        root.setLevel(level)
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)


SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "official_tools.json")
SNAPSHOT_VERSION = 1


def load_default_servers() -> List[Any]:
    """이 서비스가 쓰는 공식 서버: CloudWatch, AWS 문서, Cost Explorer, CloudTrail, Pricing, IAM, 네트워크.
    부를 때 import한다 (무겁다). 불러오면서 바뀐 기본 로거 설정은 되돌린다."""
    return _keeping_root_logger(_import_default_servers)


def _prepare_environment() -> None:
    # 공식 서버는 import할 때 loguru로 로그를 남긴다. Lambda 로그가 넘치지 않게 경고 이상만 남긴다
    os.environ.setdefault("FASTMCP_LOG_LEVEL", "ERROR")
    # Lambda에서는 /tmp만 쓸 수 있고 패키지가 설치된 곳(site-packages)은 읽기 전용이다.
    # billing-cost-management 서버는 import하는 순간 로그 파일을 자기 설치 폴더(awslabs/logs)에 만들려고 해서,
    # 그대로 두면 "Read-only file system"으로 MCP Lambda가 시작하지 못한다. 로그 파일 위치를 /tmp로 옮긴다
    os.environ.setdefault("FASTMCP_LOG_FILE", "/tmp/billing-cost-management-mcp-server.log")
    # 같은 서버는 응답이 크면(기본 25KB) 설치 폴더에 SQLite 파일을 만들어 옮겨 두고, 그 표를 SQL 도구로
    # 다시 읽게 한다. 이 서비스는 SQL 도구를 붙이지 않았으므로 옮기지 말고 응답을 그대로 받는다
    # (Lambda 응답 한도 6MB보다 작게)
    os.environ.setdefault("MCP_SQL_THRESHOLD", str(5 * 1024 * 1024))


def _import_cloudwatch():
    from awslabs.cloudwatch_mcp_server.server import mcp
    return mcp


def _import_documentation():
    from awslabs.aws_documentation_mcp_server.server_aws import mcp
    return mcp


def _import_cost_explorer():
    from awslabs.billing_cost_management_mcp_server.tools.cost_explorer_tools import cost_explorer_server
    return cost_explorer_server


def _import_cloudtrail():
    from awslabs.cloudtrail_mcp_server.server import mcp
    return mcp


def _import_pricing():
    from awslabs.aws_pricing_mcp_server.server import mcp
    return mcp


def _import_iam():
    from awslabs.iam_mcp_server.context import Context as IamContext
    from awslabs.iam_mcp_server.server import mcp

    # IAM 서버 자체의 읽기 전용 모드. 기본값이 켜짐이고 --allow-write로 실행할 때만 꺼지지만(main),
    # 같은 프로세스에서 import해 쓰므로 여기서 명시적으로 켜고 확인한다 (켜지지 않았으면 서버를 붙이지 않는다)
    IamContext.set_readonly(True)
    if not IamContext.is_readonly():
        raise RuntimeError("IAM MCP 서버의 읽기 전용 모드를 켜지 못했습니다")
    return mcp


def _import_network():
    from awslabs.aws_network_mcp_server.server import mcp
    return mcp


# 서버 이름 → 불러오는 함수 (이 순서가 tools/list의 순서다). 도구를 부를 때 그 도구의 서버 하나만 불러온다
SERVER_IMPORTERS: Dict[str, Callable[[], Any]] = {
    "cloudwatch": _import_cloudwatch,
    "documentation": _import_documentation,
    "cost_explorer": _import_cost_explorer,
    "cloudtrail": _import_cloudtrail,
    "pricing": _import_pricing,
    "iam": _import_iam,
    "network": _import_network,
}


def _import_server(key: str) -> Any:
    """서버 하나를 불러온다 (환경 준비·기본 로거 되돌리기 포함)."""
    def importer():
        _prepare_environment()
        return SERVER_IMPORTERS[key]()
    return _keeping_root_logger(importer)


def _import_default_servers() -> List[Any]:
    _prepare_environment()
    return [importer() for importer in SERVER_IMPORTERS.values()]


class OfficialTools:
    """공식 서버 여러 개의 도구를 하나로 모아 목록을 주고, 이름으로 해당 서버에 호출을 넘긴다."""

    def __init__(self, load_servers: Callable[[], List[Any]], excluded: Optional[Dict[str, str]] = None,
                 region_from_env: Optional[set] = None, hidden_arguments: Optional[Dict[str, Dict[str, Any]]] = None,
                 snapshot_path: Optional[str] = None, server_keys: Optional[List[str]] = None,
                 import_server: Optional[Callable[[str], Any]] = None):
        self._load_servers = load_servers  # 서버 목록을 돌려주는 함수. 도구 목록이 처음 필요할 때 부른다
        # 도구 목록 스냅샷 (모듈 설명). server_keys: load_servers가 돌려주는 서버의 이름 (같은 순서),
        # import_server: 이름으로 서버 하나만 불러오는 함수 (스냅샷으로 목록을 만든 뒤 도구를 부를 때 쓴다)
        self._snapshot_path = snapshot_path
        self._server_keys = server_keys
        self._import_server = import_server
        self._owner: Dict[str, str] = {}  # 도구 이름 → 서버 이름
        self._raw: Dict[str, Dict[str, Any]] = {}  # 도구 이름 → 보정 전 정의 (스냅샷에 적는 것)
        self._excluded = excluded or {}
        self._region_from_env = region_from_env or set()
        self._hidden_arguments = hidden_arguments or {}
        self._injected: Dict[str, Dict[str, Any]] = {}  # 도구 이름 → 부를 때 채워 넣을 인자 (스키마에 있던 것만)
        self._loop = asyncio.new_event_loop()
        self._schemas: Optional[Dict[str, Dict[str, Any]]] = None  # 도구 이름 → MCP 도구 정의
        self._routes: Dict[str, Any] = {}  # 도구 이름 → 서버 객체

    @classmethod
    def default(cls) -> "OfficialTools":
        """이 서비스가 쓰는 공식 서버를 붙인다. 여기서는 불러오지 않는다 (위 모듈 설명)."""
        return cls(load_default_servers, EXCLUDED_TOOLS, REGION_FROM_ENV_TOOLS, HIDDEN_ARGUMENTS,
                   snapshot_path=SNAPSHOT_PATH, server_keys=list(SERVER_IMPORTERS), import_server=_import_server)

    def _without_hidden(self, name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """HIDDEN_ARGUMENTS의 인자를 스키마에서 뺀다. 실제로 있던 인자만 부를 때 채워 넣는다."""
        hidden = {arg: value for arg, value in self._hidden_arguments.get(name, {}).items()
                  if arg in schema.get("properties", {})}
        if not hidden:
            return schema
        self._injected[name] = hidden
        schema = copy.deepcopy(schema)
        for arg in hidden:
            schema["properties"].pop(arg, None)
        if "required" in schema:
            schema["required"] = [arg for arg in schema["required"] if arg not in hidden]
        return schema

    def _region(self) -> Optional[str]:
        return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

    def _with_env_region(self, name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """region 기본값을 이 Lambda의 리전으로 바꾼 스키마 (모델이 읽는 설명도 함께)."""
        region = self._region()
        props = schema.get("properties", {})
        if name not in self._region_from_env or "region" not in props or not region:
            return schema
        schema = copy.deepcopy(schema)
        schema["properties"]["region"].update(
            default=region, description=f"AWS region to query. Defaults to {region} (this deployment's region).")
        # 생략해도 부를 때 채워 넣으므로 필수에서 뺀다
        if "required" in schema:
            schema["required"] = [arg for arg in schema["required"] if arg != "region"]
        return schema

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _finish(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """보정 전 정의(스냅샷에 적는 것) → tools/list에 넣을 정의 (이 Lambda의 리전·숨긴 인자 보정)."""
        name = raw["name"]
        return {"name": name, "description": raw.get("description", ""),
                "inputSchema": self._without_hidden(name, self._with_env_region(name, raw["inputSchema"]))}

    async def _collect(self) -> None:
        from fastmcp import Client

        schemas: Dict[str, Dict[str, Any]] = {}
        servers = self._load_servers()
        keys = self._server_keys if self._server_keys and len(self._server_keys) == len(servers) else None
        for index, server in enumerate(servers):
            async with Client(server) as client:
                for tool in await client.list_tools():
                    if tool.name in self._excluded:
                        continue
                    if tool.name in schemas:
                        raise ValueError(f"공식 MCP 도구 이름이 겹칩니다: {tool.name}")
                    definition = tool.model_dump(by_alias=True, exclude_none=True, mode="json")
                    raw = {"name": tool.name, "description": definition.get("description", ""),
                           "inputSchema": _inline_refs(definition.get("inputSchema", {"type": "object"}))}
                    self._raw[tool.name] = raw
                    schemas[tool.name] = self._finish(raw)
                    self._routes[tool.name] = server
                    self._owner[tool.name] = keys[index] if keys else str(index)
        self._schemas = schemas

    def _load_snapshot(self) -> bool:
        """스냅샷 파일로 도구 목록을 만든다 (서버를 불러오지 않는다). 없거나 맞지 않으면 False."""
        if not self._snapshot_path or not self._import_server or not os.path.exists(self._snapshot_path):
            return False
        try:
            with open(self._snapshot_path, encoding="utf-8") as file:
                snapshot = json.load(file)
            if snapshot.get("version") != SNAPSHOT_VERSION:
                return False
            tools = snapshot["tools"]
            if any(tool["server"] not in (self._server_keys or []) for tool in tools):
                return False
            schemas = {}
            for tool in tools:
                if tool["name"] in self._excluded:  # 스냅샷을 만든 뒤 뺀 도구
                    continue
                raw = {"name": tool["name"], "description": tool.get("description", ""),
                       "inputSchema": tool["inputSchema"]}
                schemas[tool["name"]] = self._finish(raw)
                self._owner[tool["name"]] = tool["server"]
            self._schemas = schemas
            logger.info("공식 MCP 도구 %d개를 스냅샷에서 읽었습니다 (서버는 부를 때 불러옵니다)", len(schemas))
            return True
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("공식 MCP 도구 스냅샷을 읽지 못해 서버를 불러와 모읍니다: %s", error)
            self._owner.clear()
            return False

    def schemas(self) -> List[Dict[str, Any]]:
        """tools/list에 넣을 도구 정의. 스냅샷이 있으면 그것으로, 없으면 공식 서버를 불러와 한 번만 모은다
        (컨테이너가 살아 있는 동안 그대로)."""
        if self._schemas is None and not self._load_snapshot():
            self._run(self._collect())
        return list(self._schemas.values())

    def has(self, name: str) -> bool:
        self.schemas()
        return name in self._schemas

    def _server_of(self, name: str) -> Any:
        """도구가 속한 서버 객체. 스냅샷으로 목록을 만들었으면 그 서버 하나만 이때 불러온다."""
        if name in self._routes:
            return self._routes[name]
        key = self._owner.get(name)
        if key is None or self._import_server is None:
            self._run(self._collect())  # 스냅샷에 없는 도구: 예전처럼 모두 불러와 모은다
            return self._routes[name]
        server = self._import_server(key)
        for tool_name, owner in self._owner.items():
            if owner == key:
                self._routes[tool_name] = server
        # 숨긴 인자 보정(_injected)은 스냅샷을 읽을 때(_finish) 이미 정해졌다
        return server

    def write_snapshot(self, path: str) -> int:
        """모든 서버를 불러와 도구 정의를 파일로 적는다 (이미지를 만들 때, Dockerfile). 적은 도구 수를 돌려준다."""
        self._run(self._collect())
        tools = [{**self._raw[name], "server": self._owner[name]} for name in self._schemas]
        with open(path, "w", encoding="utf-8") as file:
            json.dump({"version": SNAPSHOT_VERSION, "tools": tools}, file, ensure_ascii=False)
        return len(tools)

    def call(self, name: str, arguments: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
        """도구를 부르고 (MCP content 목록, 오류 여부)를 돌려준다. 도구 안의 오류도 예외가 아니라 결과로 온다."""
        from fastmcp import Client

        server = self._server_of(name)
        # 스키마에서 뺀 인자를 채워 넣는다 (공식 서버 결함 보정). 모델이 같은 이름을 보내도 보정 값이 이긴다
        if name in self._injected:
            arguments = {**(arguments or {}), **copy.deepcopy(self._injected[name])}
        # region을 생략하면 서버 기본값(버지니아 북부) 대신 이 Lambda의 리전을 넘긴다
        if name in self._region_from_env and not (arguments or {}).get("region") and self._region():
            arguments = {**(arguments or {}), "region": self._region()}

        async def call_once():
            async with Client(server) as client:
                return await client.call_tool(name, arguments or {}, raise_on_error=False)

        result = self._run(call_once())
        content = [item.model_dump(by_alias=True, exclude_none=True, mode="json") for item in result.content]
        if result.is_error:
            logger.warning("공식 MCP 도구 %s 오류: %s", name, content[:1])
        return content, bool(result.is_error)


def main(argv: List[str]) -> int:
    """이미지를 만들 때 도구 목록 스냅샷을 적는다: python -m lambda_mcp.official --write-snapshot
    실패해도 빌드를 멈추지 않는다 (스냅샷이 없으면 실행할 때 예전처럼 모두 불러와 모은다)."""
    if argv[1:] != ["--write-snapshot"]:
        print("사용법: python -m lambda_mcp.official --write-snapshot")
        return 2
    try:
        count = OfficialTools.default().write_snapshot(SNAPSHOT_PATH)
        print(f"공식 MCP 도구 {count}개를 {SNAPSHOT_PATH}에 적었습니다")
    except Exception as error:  # noqa: BLE001 - 빌드는 계속한다
        print(f"공식 MCP 도구 스냅샷을 만들지 못했습니다 (실행할 때 모두 불러와 모읍니다): {error}")
        if os.path.exists(SNAPSHOT_PATH):
            os.remove(SNAPSHOT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
