"""로컬 장애 재현 서버: 가짜 AWS(moto) + Vigie MCP 서버(Streamable HTTP) + 시나리오 관리 주소를 한 프로세스에 띄운다.

    python -m local.stack                    # http://127.0.0.1:8765/mcp, 정상 환경으로 시작
    python -m local.stack --admin            # 관리자 전용 도구(diagnoseService 등)를 관리자 요청으로 부른다
    python -m local.stack --scenario alb-targets-stopped

    Claude Code · MCP 클라이언트 ──POST /mcp──▶ mcp/app.py의 lambda_handler (Function URL 요청과 같은 모양으로 바꿔 부른다)
    python -m local.scenario ──/_local/scenario──▶ 정상 환경을 다시 만들고 장애를 심는다 (local/world.py)
    화면 (npm run dev:local) ──http://127.0.0.1:8787──▶ 로컬 API 서버 (local/api.py) ──▶ 위 MCP 서버

실제 AWS에 닿지 않게 하는 장치 (prepare_environment)
- 모든 AWS 호출은 같은 프로세스 안의 moto(mock_aws)가 받는다. 별도 moto 서버를 두지 않아 네트워크로 나가는 AWS 요청이 없다
- AWS_로 시작하는 환경 변수(프로필·엔드포인트·자격 증명)를 모두 지우고 가짜 자격 증명을 넣는다. 설정·자격 증명 파일도 읽지 않는다
- 그래도 moto가 모르는 AWS 주소로 나가려는 요청은 닫힌 로컬 프록시로 보내 실패시킨다 (AWS 문서 검색만 예외)
- 127.0.0.1에만 붙고, Host 머리글이 로컬이 아니면 거절한다 (DNS 리바인딩으로 웹 페이지가 부르는 것을 막는다)

변경 도구는 여기서도 승인 없이는 실행되지 않는다 (MCP 서버의 승인 재확인). 승인 흐름은 로컬 API 서버가 붙은 뒤에 쓴다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8765
DEFAULT_REGION = "ap-northeast-2"
ENV_NAME = "local"
SESSION_TABLE = f"vigie-mcp-sessions-{ENV_NAME}"
PENDING_TABLE = f"vigie-pending-actions-{ENV_NAME}"
REFRESH_SECONDS = 10 * 60  # 지표·로그를 다시 넣는 간격 (진단 창 '최근 1시간' 안에 머물도록)

DEAD_PROXY = "http://127.0.0.1:9"  # 아무도 듣지 않는 포트: 여기로 나간 요청은 곧바로 실패한다
# 프록시를 거치지 않는 곳: 로컬 서버, AWS 문서 검색, 모델 API (AWS 계정과 무관한 주소만)
NO_PROXY = "127.0.0.1,localhost,docs.aws.amazon.com,.docs.aws.amazon.com,api.anthropic.com"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
# /mcp: Claude Code 등이 직접 붙는 주소 (--admin이면 관리자 요청), /mcp/server: 로컬 API 서버(LLM 서버 코드)가 부르는 주소
MCP_PATHS = ("/mcp", "/mcp/server")


def prepare_environment(region: str, environ=os.environ) -> None:
    """실제 AWS 계정에 닿지 않도록 환경 변수를 고친다. boto3 클라이언트를 만들기 전에 부른다."""
    for name in [name for name in environ if name.startswith("AWS_")]:
        del environ[name]
    environ.update({
        "AWS_ACCESS_KEY_ID": "local-fake", "AWS_SECRET_ACCESS_KEY": "local-fake", "AWS_SESSION_TOKEN": "local-fake",
        "AWS_REGION": region, "AWS_DEFAULT_REGION": region,
        "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull, "AWS_EC2_METADATA_DISABLED": "true",
        "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY,
        "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY,
        "ENV": ENV_NAME, "MCP_SESSION_TABLE": SESSION_TABLE, "PENDING_ACTIONS_TABLE": PENDING_TABLE,
    })


def create_tables(region: str) -> None:
    """MCP 서버가 쓰는 테이블 (세션, 승인 요청)."""
    import boto3
    dynamodb = boto3.client("dynamodb", region_name=region)
    for name, key in ((SESSION_TABLE, "session_id"), (PENDING_TABLE, "actionId")):
        dynamodb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                              AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                              KeySchema=[{"AttributeName": key, "KeyType": "HASH"}])


def load_app():
    """mcp/app.py (Lambda에 올라가는 MCP 서버 그대로)."""
    sys.path.insert(0, str(ROOT / "mcp"))
    import app
    return app


class Stack:
    """MCP 서버 하나와 그 서버가 보는 가짜 AWS 환경. 시나리오를 바꾸는 동안에는 도구 호출을 기다리게 한다."""

    def __init__(self, app, region: str, admin: bool = False):
        self.app, self.region, self.admin = app, region, admin
        self.lock = threading.RLock()
        self.world: Optional[dict] = None
        self.scenario = None
        self.on_change = None  # 환경을 다시 만든 뒤 부를 함수 (로컬 API 서버가 홈 대시보드를 다시 모은다)

    # ------------------------------------------------------------ 시나리오
    def apply(self, name: Optional[str] = None) -> dict:
        """정상 환경을 새로 만들고 시나리오의 장애를 심는다. name이 없으면 정상 환경만."""
        from local import world as w
        scenario = None
        if name:
            scenario = w.BY_NAME.get(name)
            if scenario is None:
                raise KeyError(f"시나리오가 없습니다: {name} (python -m local.scenario list로 목록을 봅니다)")
        with self.lock:
            if self.world is not None:
                w.reset_world()
            world = w.build_world(self.region)
            w.install_fakes(self.app.diagnose, world)
            if scenario and scenario.fault:
                scenario.fault(world)
            self.world, self.scenario = world, scenario
            if self.on_change:
                self.on_change()
            return self.describe()

    def refresh(self) -> dict:
        from local import world as w
        with self.lock:
            if self.world is not None:
                w.refresh(self.world)
            return self.describe()

    def describe(self) -> dict:
        from local import world as w
        scenario = self.scenario
        if scenario is None:
            return {"scenario": None, "about": "정상 환경 (심은 장애 없음)"}
        return {"scenario": scenario.name, "about": scenario.about, "service": scenario.service,
                "resource": w.fill(scenario.resource, self.world), "target": w.fill(scenario.target, self.world),
                "ask": w.fill(scenario.ask, self.world),
                "causes": sorted(scenario.causes), "symptoms": sorted(scenario.symptoms)}

    @staticmethod
    def catalog() -> list:
        from local import world as w
        return [{"name": s.name, "service": s.service, "about": s.about,
                 "causes": sorted(s.causes), "symptoms": sorted(s.symptoms)} for s in w.SCENARIOS]

    # ------------------------------------------------------------ MCP (Streamable HTTP → Lambda Function URL 요청)
    def mcp(self, method: str, headers: dict, body: str, direct: bool = True) -> tuple:
        """direct: Claude Code 등이 직접 붙은 요청(/mcp). LLM 서버가 부르는 /mcp/server는 False로, --admin이어도
        관리자 표시를 붙이지 않는다 (LLM 서버가 요청자의 그룹을 보고 스스로 붙인다. 일반 사용자 요청이 관리자가 되면 안 된다)."""
        headers = {k.lower(): v for k, v in headers.items()}
        # Lambda 핸들러는 content-type이 정확히 application/json이어야 받는다 (charset 따위는 뗀다)
        headers["content-type"] = headers.get("content-type", "").split(";")[0].strip()
        if self.admin and direct and method == "POST":
            body = self._as_admin(body)
        with self.lock:
            response = self.app.lambda_handler({"httpMethod": method, "headers": headers, "body": body}, None)
        status = response.get("statusCode", 200)
        if status == 204 and method == "POST":
            status = 202  # 알림(notifications/*)에는 202 Accepted로 답한다 (Streamable HTTP 규약)
        return status, response.get("headers") or {}, response.get("body") or ""

    @staticmethod
    def _as_admin(body: str) -> str:
        """--admin: 도구 호출에 관리자 표시(vigie/role)를 붙인다. 운영에서는 LLM 서버가 Cognito 그룹을 보고 붙이는 값이다."""
        try:
            request = json.loads(body)
        except ValueError:
            return body
        if isinstance(request, dict) and request.get("method") == "tools/call":
            params = request.setdefault("params", {})
            params.setdefault("_meta", {}).setdefault("vigie/role", "admin")
            return json.dumps(request)
        return body

    # ------------------------------------------------------------ HTTP
    def serve(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
        if host not in LOCAL_HOSTS:
            raise ValueError("로컬 서버는 127.0.0.1에만 붙습니다")
        server = ThreadingHTTPServer((host, port), _handler(self))
        server.daemon_threads = True
        return server

    def keep_fresh(self, interval: float = REFRESH_SECONDS) -> threading.Event:
        """지표·로그를 interval마다 다시 넣는다. 돌려준 Event를 set하면 멈춘다."""
        stop = threading.Event()

        def loop():
            while not stop.wait(interval):
                self.refresh()

        threading.Thread(target=loop, name="refresh", daemon=True).start()
        return stop


def _handler(stack: Stack):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # noqa: A002 (BaseHTTPRequestHandler의 이름 그대로)
            if os.environ.get("VIGIE_LOCAL_VERBOSE"):
                super().log_message(format, *args)

        def _send(self, status: int, body="", headers: Optional[dict] = None):
            data = body if isinstance(body, bytes) else (
                body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
            self.send_response(status)
            for name, value in (headers or {}).items():
                if name.lower() not in ("content-length", "connection"):
                    self.send_header(name, value)
            if not any(name.lower() == "content-type" for name in (headers or {})) and data:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> str:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length).decode() if length else ""

        def _local_only(self) -> bool:
            host = self.headers.get("Host") or ""
            host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]  # 포트를 뗀다
            if host not in LOCAL_HOSTS:
                self._send(403, {"error": "로컬 주소로만 부를 수 있습니다"})
                return False
            return True

        def do_GET(self):
            if not self._local_only():
                return
            if self.path in MCP_PATHS:
                # 서버가 먼저 보내는 SSE 스트림은 없다 (규약상 405로 답하면 클라이언트가 POST만 쓴다)
                self._send(405, "", {"Allow": "POST, DELETE"})
            elif self.path == "/_local/scenarios":
                self._send(200, {"current": stack.describe(), "scenarios": stack.catalog()})
            elif self.path == "/_local/scenario":
                self._send(200, stack.describe())
            else:
                self._send(404, {"error": "없는 주소입니다"})

        def do_DELETE(self):
            if not self._local_only():
                return
            if self.path not in MCP_PATHS:
                return self._send(404, {"error": "없는 주소입니다"})
            status, headers, body = stack.mcp("DELETE", dict(self.headers), self._body(), self.path == "/mcp")
            self._send(status, body, headers)

        def do_POST(self):
            if not self._local_only():
                return
            body = self._body()
            if self.path in MCP_PATHS:
                status, headers, response = stack.mcp("POST", dict(self.headers), body, self.path == "/mcp")
                return self._send(status, response, headers)
            if self.path != "/_local/scenario":
                return self._send(404, {"error": "없는 주소입니다"})
            # application/json만 받는다: 다른 사이트의 페이지가 사전 요청(CORS) 없이 보낼 수 있는 형식은 거절
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self._send(415, {"error": "Content-Type은 application/json이어야 합니다"})
            try:
                request = json.loads(body or "{}")
                action = request.get("action")
                if action == "apply":
                    return self._send(200, stack.apply(request.get("name")))
                if action == "reset":
                    return self._send(200, stack.apply(None))
                if action == "refresh":
                    return self._send(200, stack.refresh())
                return self._send(400, {"error": "action은 apply · reset · refresh 중 하나입니다"})
            except KeyError as error:
                return self._send(404, {"error": error.args[0]})
            except ValueError:
                return self._send(400, {"error": "본문이 JSON이 아닙니다"})

    return Handler


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="가짜 AWS 위에 Vigie MCP 서버를 띄운다 (실제 AWS에 닿지 않는다)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="MCP 서버 포트")
    parser.add_argument("--api-port", type=int, default=8787, help="로컬 API 서버 포트 (화면이 붙는다)")
    parser.add_argument("--region", default=DEFAULT_REGION, help="가짜 AWS의 리전 (기본 서울)")
    parser.add_argument("--admin", action="store_true",
                        help="/mcp로 직접 붙은 도구 호출을 관리자 요청으로 보낸다 (Claude Code를 직접 붙일 때. 화면의 요청과는 무관)")
    parser.add_argument("--scenario", help="처음에 심을 시나리오 (없으면 정상 환경)")
    args = parser.parse_args(argv)

    prepare_environment(args.region)
    from moto import mock_aws  # 환경 변수를 고친 뒤에 불러온다

    from local import api as local_api
    url = f"http://127.0.0.1:{args.port}/mcp"

    with mock_aws():
        create_tables(args.region)
        local_api.create_api_tables(args.region)
        local_api.api_environment(os.environ, url + "/server")
        claims = local_api.create_user_pool(args.region)
        print("MCP 서버와 공식 AWS MCP 서버, LLM 서버 코드를 불러오는 중…", flush=True)
        stack = Stack(load_app(), args.region, admin=args.admin)
        api = local_api.LocalApi(args.region, url + "/server", claims, world_of=lambda: stack.world)
        stack.on_change = api.collect_dashboard
        current = stack.apply(args.scenario)
        servers = [stack.serve("127.0.0.1", args.port), api.serve("127.0.0.1", args.api_port)]
        threading.Thread(target=servers[1].serve_forever, name="api", daemon=True).start()
        stop = stack.keep_fresh()
        print(f"\nVigie 로컬 MCP 서버: {url}  (가짜 AWS · {args.region}{' · 관리자 요청' if args.admin else ''})")
        print(f"로컬 API 서버:      http://127.0.0.1:{args.api_port}  (화면: npm --prefix frontend run dev:local)")
        print(f"모델: {'Anthropic API (ANTHROPIC_API_KEY)' if api.has_model else '없음 — 대화는 안내 문구로 거절합니다'}")
        print(f"지금 환경: {current.get('scenario') or '정상'} — {current['about']}")
        print("시나리오 바꾸기: python -m local.scenario list | apply <이름> | reset")
        print("Claude Code에 바로 붙이기 (이 세션에만):")
        print(f"  claude --mcp-config '{{\"mcpServers\":{{\"vigie\":{{\"type\":\"http\",\"url\":\"{url}\"}}}}}}'")
        print("멈추기: Ctrl+C\n", flush=True)
        try:
            servers[0].serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            servers[1].shutdown()
            for server in servers:
                server.server_close()


if __name__ == "__main__":
    main()
