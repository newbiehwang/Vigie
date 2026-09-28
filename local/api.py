"""로컬 API 서버: 배포에서 API Gateway와 Cognito 권한 부여자가 하는 일을 대신해, 화면의 요청을 Lambda 핸들러에 넘긴다.

    화면 (npm run dev:local) ──▶ http://127.0.0.1:8787
        GET  /health · POST /llm1 · GET /llm1/progress/{id}   ─┐
        GET  /audit · GET /dashboard · /actions/{id}[/…]      ─┴▶ services/llm/lambda_function.py
        /sessions…                                            ──▶ services/chat-history/lambda_function.py
        /users…                                               ──▶ services/llm/user_admin.py
    LLM 서버 코드가 부르는 MCP 서버는 같은 프로세스의 local/stack.py (http://127.0.0.1:8765/mcp)

로그인: Cognito 대신 moto의 User Pool에 만든 로컬 사용자 세 명 중 하나로 요청한다.
화면이 요청마다 X-Vigie-Local-Role 머리글(admin · decider · member)을 붙이고, 이 서버가 그 사용자의 토큰 내용(claims)을
권한 부여자처럼 요청에 넣는다. 머리글이 없는 요청은 거절한다: 다른 사이트의 페이지는 이 머리글을 붙이려면 사전 요청(CORS)을
거쳐야 하고, 사전 요청은 로컬 화면 주소(localhost · 127.0.0.1)에만 허락한다.

모델: 우선 배포와 같은 Anthropic API 경로를 쓴다. ANTHROPIC_API_KEY 환경 변수가 없으면 대화는 안내 문구로 거절한다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlsplit

from local.stack import ENV_NAME, LOCAL_HOSTS, PENDING_TABLE, ROOT

DEFAULT_API_PORT = 8787
ACCOUNT_ID = "123456789012"  # moto 계정 (답변과 감사 로그에서 가려지는 값)
ROLE_HEADER = "X-Vigie-Local-Role"
TABLES = {
    "LLM_PROGRESS_TABLE": f"vigie-llm-progress-{ENV_NAME}",
    "AUDIT_TABLE": f"vigie-audit-{ENV_NAME}",
    "DASHBOARD_TABLE": f"vigie-dashboard-{ENV_NAME}",
    "CHAT_HISTORY_TABLE": f"vigie-chat-history-{ENV_NAME}",
}
# 로컬 사용자: 권한 세 단계마다 한 명 (services/llm/user_admin.py의 role_of와 같은 그룹)
LOCAL_USERS = {
    "admin": ("admin@vigie.local", ["admins", "approvers"]),
    "decider": ("decider@vigie.local", ["approvers"]),
    "member": ("member@vigie.local", []),
}
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")
NO_MODEL = ("로컬 모델이 연결되지 않았습니다. ANTHROPIC_API_KEY 환경 변수를 넣고 local.stack을 다시 띄우세요 "
            "(비용이 드는 Anthropic API를 씁니다).")


def api_environment(environ, mcp_url: str) -> None:
    """LLM·대화 기록·사용자 관리 Lambda가 읽는 환경 변수 (CloudFormation이 넣는 값을 로컬 테이블로)."""
    environ.update(TABLES)
    environ.update({"PENDING_ACTIONS_TABLE": PENDING_TABLE, "MCP_URL": mcp_url, "ACCOUNT_ID": ACCOUNT_ID})


def local_config(region: str, mcp_url: str) -> dict:
    """common.config가 SSM에서 읽는 설정 대신 쓰는 값."""
    return {
        "aws_region": region, "env": ENV_NAME,
        "amplify": {"app_id": "", "default_domain": "", "default_domain_with_env": ""},
        "api": {"endpoint": f"http://127.0.0.1:{DEFAULT_API_PORT}", "gateway_id": "", "root_resource_id": ""},
        "s3": {}, "frontend": {"redirect_domain": ""},
        "cognito": {"user_pool_id": os.environ.get("USER_POOL_ID", ""), "client_id": "", "domain": "",
                    "identity_pool_id": ""},
        "slackbot": {"token": "", "signing_secret": ""},
        "mcp": {"function_url": mcp_url}, "kb": {"kb_id": ""},
        "db": {"chat_history_table": TABLES["CHAT_HISTORY_TABLE"]},
        "anthropic": {"api_key": ""},
    }


def create_api_tables(region: str) -> None:
    """LLM 서버·대화 기록이 쓰는 테이블 (cloudformation/base.yaml · llm.yaml과 같은 키)."""
    import boto3
    dynamodb = boto3.client("dynamodb", region_name=region)

    def table(name, keys, indexes=None):
        attributes = {key for key, _ in keys} | {key for index in (indexes or []) for key, _ in index[1]}
        kwargs = {"GlobalSecondaryIndexes": [
            {"IndexName": index_name, "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": key, "KeyType": kind} for key, kind in index_keys]}
            for index_name, index_keys in indexes]} if indexes else {}
        dynamodb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                              AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attributes)],
                              KeySchema=[{"AttributeName": key, "KeyType": kind} for key, kind in keys], **kwargs)

    table(TABLES["LLM_PROGRESS_TABLE"], [("requestId", "HASH")])
    table(TABLES["AUDIT_TABLE"], [("userId", "HASH"), ("at", "RANGE")],
          [("by-day", [("day", "HASH"), ("at", "RANGE")])])
    table(TABLES["DASHBOARD_TABLE"], [("section", "HASH")])
    table(TABLES["CHAT_HISTORY_TABLE"], [("sessionId", "HASH")], [("UserIdIndex", [("userId", "HASH")])])


def create_user_pool(region: str, environ=os.environ) -> dict:
    """moto의 User Pool과 로컬 사용자 세 명. 권한별 토큰 내용(claims)을 돌려준다.
    사용자 관리가 토큰만 믿지 않고 User Pool에서 그룹을 다시 확인하므로 실제로 만들어 둔다."""
    import boto3
    cognito = boto3.client("cognito-idp", region_name=region)
    pool = cognito.create_user_pool(PoolName=f"vigie-user-pool-{ENV_NAME}", UsernameAttributes=["email"])["UserPool"]["Id"]
    for group in ("admins", "approvers"):
        cognito.create_group(UserPoolId=pool, GroupName=group)
    claims = {}
    for role, (email, groups) in LOCAL_USERS.items():
        username = cognito.admin_create_user(UserPoolId=pool, Username=email, MessageAction="SUPPRESS",
                                             UserAttributes=[{"Name": "email", "Value": email},
                                                             {"Name": "email_verified", "Value": "true"}])["User"]["Username"]
        # 확인된 계정으로 둔다 (사용자 관리 탭에 '초대됨'이 아니라 활성으로 보이게). 아무도 쓰지 않는 임의 비밀번호
        cognito.admin_set_user_password(UserPoolId=pool, Username=username, Password=f"Aa1!{secrets.token_urlsafe(24)}",
                                        Permanent=True)
        for group in groups:
            cognito.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName=group)
        claims[role] = {"sub": username, "cognito:username": username, "email": email,
                        "cognito:groups": ",".join(groups)}
    environ["USER_POOL_ID"] = pool
    return claims


def _load(name: str, path) -> object:
    """같은 이름(lambda_function)의 파일이 서비스마다 있어서, 파일 경로로 다른 이름을 붙여 불러온다."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LocalApi:
    """경로를 Lambda 핸들러로 넘기고, 권한 부여자처럼 로컬 사용자의 claims를 넣는다."""

    def __init__(self, region: str, mcp_url: str, claims: dict, modules: Optional[dict] = None,
                 world_of: Optional[Callable[[], Optional[dict]]] = None):
        self.region, self.mcp_url, self.claims = region, mcp_url, claims
        self.world_of = world_of or (lambda: None)  # 지금 가짜 AWS 환경 (local/world.py, 홈의 '최근 변경'에 쓴다)
        self.has_model = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.modules = modules or self._load_services(region, mcp_url)

    @staticmethod
    def _load_services(region: str, mcp_url: str) -> dict:
        for folder in ("services/dashboard", "services/chat-history", "services/llm", "layers"):
            path = str(ROOT / folder)
            if path not in sys.path:
                sys.path.insert(0, path)
        import common.config
        common.config._config = local_config(region, mcp_url)  # SSM 대신
        import user_admin
        return {
            "llm": _load("vigie_local_llm", ROOT / "services/llm/lambda_function.py"),
            "chat": _load("vigie_local_chat_history", ROOT / "services/chat-history/lambda_function.py"),
            "dashboard": _load("vigie_local_dashboard", ROOT / "services/dashboard/lambda_function.py"),
            "users": user_admin,
        }

    def collect_dashboard(self) -> dict:
        """홈 대시보드를 한 번 모은다 (배포에서는 Scheduler가 5분·1시간·하루마다 부른다)."""
        from local.world import TrailLookup
        dashboard = self.modules["dashboard"]
        dashboard._clients.clear()  # 가짜 AWS를 다시 만들었으면 예전 클라이언트를 버린다
        dashboard._clients["cloudtrail"] = TrailLookup(self.world_of)  # moto에 LookupEvents가 없다
        return dashboard.lambda_handler({"sections": list(dashboard.SECTIONS)}, None)["sections"]

    def handle(self, method: str, target: str, headers: dict, body: str) -> tuple:
        """(상태 코드, 머리글, 본문)."""
        headers = {k.lower(): v for k, v in headers.items()}
        origin = headers.get("origin", "")
        cors = self.cors(origin)
        if method == "OPTIONS":
            return (204 if cors else 403), cors, ""
        url = urlsplit(target)
        path, query = url.path, dict(parse_qsl(url.query)) or None
        role = headers.get(ROLE_HEADER.lower())
        if path != "/health" and role not in self.claims:
            return 401, cors, json.dumps({"error": f"{ROLE_HEADER} 머리글(admin · decider · member)이 필요합니다"})
        if path == "/llm1" and method == "POST" and not self.has_model:
            return 503, cors, json.dumps({"error": NO_MODEL}, ensure_ascii=False)
        # API Gateway처럼 요청에 있는 머리글만 넘긴다 (본문 없는 GET에 content-type을 붙이면 핸들러가 빈 본문을 JSON으로 읽는다)
        forwarded = {name: headers[name] for name in ("origin", "content-type") if name in headers}
        event = {"httpMethod": method, "path": path, "queryStringParameters": query, "body": body or None,
                 "headers": forwarded,
                 "requestContext": {"authorizer": {"claims": dict(self.claims.get(role) or {})}}}
        if path.startswith("/sessions"):
            module = self.modules["chat"]
        elif path == "/users" or path.startswith("/users/"):
            module = self.modules["users"]
        else:
            module = self.modules["llm"]
        response = module.lambda_handler(event, None)
        # Lambda가 설정(허용 목록)으로 정한 CORS 머리글 대신 로컬 화면 주소를 넣는다
        response_headers = {k: v for k, v in (response.get("headers") or {}).items()
                            if not k.lower().startswith("access-control-")}
        return response.get("statusCode", 200), {**response_headers, **cors}, response.get("body") or ""

    @staticmethod
    def cors(origin: str) -> dict:
        if not LOCAL_ORIGIN.match(origin or ""):
            return {}
        return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
                "Access-Control-Allow-Headers": f"Authorization,Content-Type,{ROLE_HEADER}",
                "Access-Control-Max-Age": "600", "Vary": "Origin"}

    def serve(self, host: str = "127.0.0.1", port: int = DEFAULT_API_PORT) -> ThreadingHTTPServer:
        if host not in LOCAL_HOSTS:
            raise ValueError("로컬 서버는 127.0.0.1에만 붙습니다")
        server = ThreadingHTTPServer((host, port), _handler(self))
        server.daemon_threads = True
        return server


def _handler(api: LocalApi):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # noqa: A002 (BaseHTTPRequestHandler의 이름 그대로)
            if os.environ.get("VIGIE_LOCAL_VERBOSE"):
                super().log_message(format, *args)

        def _serve(self):
            host = self.headers.get("Host") or ""
            host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            if host not in LOCAL_HOSTS:  # DNS 리바인딩
                status, headers, body = 403, {}, json.dumps({"error": "로컬 주소로만 부를 수 있습니다"}, ensure_ascii=False)
            else:
                try:
                    status, headers, body = api.handle(self.command, self.path, dict(self.headers), body)
                except Exception as error:  # 핸들러 밖의 실패 (핸들러 안의 실패는 핸들러가 500으로 돌려준다)
                    status, headers, body = 500, api.cors(self.headers.get("Origin", "")), json.dumps(
                        {"error": "로컬 API 서버 오류", "detail": str(error)}, ensure_ascii=False)
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            for name, value in headers.items():
                if name.lower() not in ("content-length", "connection"):
                    self.send_header(name, value)
            if data and not any(name.lower() == "content-type" for name in headers):
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = _serve

    return Handler
