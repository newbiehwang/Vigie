"""로컬 장애 재현 서버 (local/stack.py, local/scenario.py)

실제 서버처럼 127.0.0.1의 빈 포트에 띄우고 HTTP로 부른다 (AWS는 conftest의 moto).
- Streamable HTTP로 MCP 서버(mcp/app.py)를 부른다: initialize → tools/list → tools/call, 알림은 202, GET은 405
- 시나리오를 심으면 진단이 기대 원인을 찾고, 되돌리면 정상으로 돌아온다. MCP 세션은 시나리오를 바꿔도 이어진다
- 관리자 전용 도구는 --admin일 때만 관리자 요청이 된다
- 로컬 주소로만 부를 수 있고, 시나리오 주소는 application/json만 받는다
- 실제 AWS에 닿지 않도록 환경 변수를 고친다 (프로필·엔드포인트·자격 증명을 지우고 닫힌 프록시로)
"""
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from conftest import load_service_module
from local import scenario as scenario_cli
from local import stack as local_stack

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def serve(aws, monkeypatch):
    """Stack을 빈 포트에 띄우고 주소를 돌려준다. serve(admin=True)처럼 부른다."""
    monkeypatch.setenv("MCP_SESSION_TABLE", local_stack.SESSION_TABLE)
    monkeypatch.setenv("PENDING_ACTIONS_TABLE", local_stack.PENDING_TABLE)
    region = os.environ["AWS_REGION"]
    local_stack.create_tables(region)
    app = load_service_module("mcp", "app")
    servers = []

    def start(admin=False):
        stack = local_stack.Stack(app, region, admin=admin)
        stack.apply(None)
        server = stack.serve("127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def http(url, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with OPENER.open(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


class Mcp:
    def __init__(self, base):
        self.url = base + "/mcp"
        status, headers, _ = http(self.url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert status == 200
        self.session = headers["MCP-Session-Id"]

    def rpc(self, method, params=None, request_id=2):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            body["id"] = request_id
        return http(self.url, body, {"mcp-session-id": self.session})

    def diagnose(self, **arguments):
        status, _, body = self.rpc("tools/call", {"name": "diagnoseService", "arguments": arguments})
        assert status == 200
        result = json.loads(body)["result"]
        return result, json.loads(result["content"][0]["text"])


def scenario(base, **payload):
    status, _, body = http(base + "/_local/scenario", payload)
    return status, json.loads(body)


def test_scenario_is_diagnosed_through_streamable_http(serve):
    base = serve(admin=True)
    mcp = Mcp(base)
    status, _, body = mcp.rpc("tools/list")
    assert status == 200 and "diagnoseService" in {t["name"] for t in json.loads(body)["result"]["tools"]}

    status, current = scenario(base, action="apply", name="alb-targets-stopped")
    assert status == 200 and current["resource"] == "web-alb" and "503" in current["ask"]
    _, found = mcp.diagnose(service=current["service"], resource=current["resource"])
    assert sorted(found["causes"]) == current["causes"] == ["L2", "L5"] and "L4" in found["symptoms"]

    # 되돌리면 정상 (같은 MCP 세션으로 계속 부른다: Vigie 자신의 테이블은 비우지 않는다)
    status, current = scenario(base, action="reset")
    assert status == 200 and current["scenario"] is None
    assert mcp.diagnose(service="alb", resource="web-alb")[1]["causes"] == []


def test_placeholders_are_filled_for_the_chat(serve):
    """물어볼 말과 진단 대상에 실제 값(사설 IP, 액세스 키)이 들어간다."""
    base = serve(admin=True)
    _, current = scenario(base, action="apply", name="vpc-destination-port-closed")
    ip = current["target"].split(":")[0]
    assert current["target"].endswith(":5432") and ip in current["ask"] and "{" not in current["ask"]
    _, found = Mcp(base).diagnose(service="vpc", resource="web-1", target=current["target"])
    assert found["causes"] == ["L3"]
    _, current = scenario(base, action="apply", name="credential-persistence")
    assert current["resource"].startswith("AKIA") and current["resource"] in current["ask"]


def test_admin_tools_need_the_admin_flag(serve):
    mcp = Mcp(serve(admin=False))
    status, _, body = mcp.rpc("tools/call", {"name": "diagnoseService",
                                             "arguments": {"service": "s3", "resource": "vigie-diag-private"}})
    assert status == 200 and json.loads(body)["result"].get("isError") is True


def test_protocol_edges(serve):
    base = serve()
    mcp = Mcp(base)
    # 알림은 202 Accepted, 서버가 먼저 보내는 SSE 스트림(GET)은 없다
    assert mcp.rpc("notifications/initialized", request_id=None)[0] == 202
    status, headers, _ = http(mcp.url)
    assert status == 405 and headers["Allow"] == "POST, DELETE"
    # content-type에 charset이 붙어도 받는다
    status, _, _ = http(mcp.url, json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}).encode(),
                        {"Content-Type": "application/json; charset=utf-8", "mcp-session-id": mcp.session})
    assert status == 200
    # 세션 닫기
    assert http(mcp.url, None, {"mcp-session-id": mcp.session}, method="DELETE")[0] == 204


def test_only_local_json_requests_change_scenarios(serve):
    base = serve()
    assert http(base + "/_local/scenario", headers={"Host": "evil.example"})[0] == 403  # DNS 리바인딩
    assert http(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, {"Host": "evil.example:8765"})[0] == 403
    status, _, _ = http(base + "/_local/scenario", b'{"action":"reset"}', {"Content-Type": "text/plain"})
    assert status == 415  # 다른 사이트의 페이지가 사전 요청 없이 보낼 수 있는 형식
    status, body = scenario(base, action="apply", name="no-such-scenario")
    assert status == 404 and "local.scenario list" in body["error"]
    assert scenario(base, action="explode")[0] == 400
    with pytest.raises(ValueError):
        local_stack.Stack(None, "x").serve("0.0.0.0", 0)


def test_scenario_cli(serve, capsys):
    base = serve()
    scenario_cli.main(["--url", base, "apply", "rds-stopped"])
    out = capsys.readouterr().out
    assert "rds-stopped" in out and "원인 L2 · L5" in out and "orders-db에 연결이 안 돼요" in out
    scenario_cli.main(["--url", base, "list"])
    listing = capsys.readouterr().out.splitlines()
    assert len(listing) == 44 and [line for line in listing if line.startswith("▶")][0].split()[1] == "rds-stopped"
    scenario_cli.main(["--url", base, "refresh"])
    scenario_cli.main(["--url", base, "reset"])
    assert "정상" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="시나리오가 없습니다"):
        scenario_cli.main(["--url", base, "apply", "nope"])


def test_environment_cannot_reach_real_aws():
    environ = {"AWS_PROFILE": "prod", "AWS_ACCESS_KEY_ID": "AKIAREALKEY", "AWS_SECRET_ACCESS_KEY": "real",
               "AWS_ENDPOINT_URL": "https://example.com", "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.2",
               "HOME": "/Users/someone"}
    local_stack.prepare_environment("ap-northeast-2", environ)
    assert "AWS_PROFILE" not in environ and "AWS_ENDPOINT_URL" not in environ
    assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in environ
    assert environ["AWS_ACCESS_KEY_ID"] == "local-fake" and environ["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert environ["AWS_CONFIG_FILE"] == os.devnull and environ["AWS_REGION"] == "ap-northeast-2"
    assert environ["HTTPS_PROXY"] == environ["https_proxy"] == local_stack.DEAD_PROXY
    assert "amazonaws.com" not in environ["NO_PROXY"]  # AWS API 주소는 닫힌 프록시로 간다
    assert environ["HOME"] == "/Users/someone"
