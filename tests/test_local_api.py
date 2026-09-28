"""로컬 API 서버 (local/api.py): 화면의 요청을 Lambda 핸들러에 넘기고, 권한 부여자처럼 로컬 사용자를 넣는다

AWS는 conftest의 moto, MCP 서버는 local/stack.py를 127.0.0.1의 빈 포트에 띄워 LLM 서버 코드가 HTTP로 부른다.
모델(Anthropic API)만 가짜 응답이다.
- 관리자가 대화창에서 장애를 물으면: /llm1 → 모델이 diagnoseService를 부름 → MCP 서버(HTTP) → 감사 로그에 진단이 남는다
- 일반 사용자에게는 관리자 전용 도구가 모델에 보이지 않는다 (--admin으로 띄워도 /mcp/server에는 관리자 표시가 붙지 않는다)
- 머리글(X-Vigie-Local-Role)이 없으면 401, 로컬 화면 주소가 아닌 사전 요청은 403, 모델이 없으면 안내 문구로 503
- 대화 기록(/sessions)·사용자 관리(/users)·홈 대시보드(/dashboard)도 로컬 사용자로 동작한다
"""
import copy
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

from conftest import load_service_module
from local import api as local_api
from local import stack as local_stack
from test_approvals import FakeResponse

ORIGIN = "http://localhost:5173"
# 로컬 API가 불러오는 서비스 모듈: 테이블 이름을 불러올 때 환경 변수에서 읽으므로 테스트마다 새로 불러온다
FRESH_MODULES = ("llm_service", "chat_history_service", "user_admin", "collector", "mcp_anthropic_client",
                 "mcp_client", "lambda_function")


@pytest.fixture
def local(aws, monkeypatch):
    """MCP 서버(HTTP)와 로컬 API. local(admin=True)처럼 부르면 (stack, api)."""
    region = os.environ["AWS_REGION"]
    started = []

    def start(admin=False, has_model=True):
        monkeypatch.setenv("MCP_SESSION_TABLE", local_stack.SESSION_TABLE)
        monkeypatch.setenv("PENDING_ACTIONS_TABLE", local_stack.PENDING_TABLE)
        local_stack.create_tables(region)
        local_api.create_api_tables(region)
        app = load_service_module("mcp", "app")
        stack = local_stack.Stack(app, region, admin=admin)
        server = stack.serve("127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        mcp_url = f"http://127.0.0.1:{server.server_address[1]}/mcp/server"
        for name, value in {**local_api.TABLES, "PENDING_ACTIONS_TABLE": local_stack.PENDING_TABLE,
                            "MCP_URL": mcp_url, "ACCOUNT_ID": local_api.ACCOUNT_ID}.items():
            monkeypatch.setenv(name, value)
        pool = {}
        claims = local_api.create_user_pool(region, environ=pool)
        monkeypatch.setenv("USER_POOL_ID", pool["USER_POOL_ID"])
        for name in [n for n in sys.modules if n in FRESH_MODULES]:
            del sys.modules[name]
        monkeypatch.setattr(sys, "path", list(sys.path))  # _load_services가 넣은 경로를 테스트 뒤에 되돌린다
        api = local_api.LocalApi(region, mcp_url, claims, world_of=lambda: stack.world)
        api.has_model = has_model
        stack.on_change = api.collect_dashboard
        stack.apply("alb-targets-stopped")
        return stack, api

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def call(api, method, path, role="admin", body=None):
    headers = {"Origin": ORIGIN}
    if role:
        headers["X-Vigie-Local-Role"] = role
    if body is not None:
        headers["Content-Type"] = "application/json"
    status, response_headers, text = api.handle(method, path, headers, json.dumps(body) if body is not None else "")
    return status, response_headers, (json.loads(text) if text else None)


def fake_model(monkeypatch, replies):
    """Anthropic API 대신 replies를 차례로 돌려준다. 보낸 요청(도구 목록 등)을 모아 돌려준다."""
    import llm_service
    import mcp_anthropic_client
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse({"usage": {}, **replies[min(len(sent), len(replies)) - 1]})

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    monkeypatch.setattr(llm_service, "current_model", lambda: llm_service.FALLBACK_MODEL)
    llm_service.client_cache.clear()
    return sent


DIAGNOSE = {"type": "tool_use", "id": "toolu_diag", "name": "diagnoseService",
            "input": {"service": "alb", "resource": "web-alb"}}


def test_admin_chat_runs_the_diagnosis_and_keeps_it_in_the_audit_log(local, monkeypatch):
    stack, api = local(admin=True)
    sent = fake_model(monkeypatch, [{"content": [DIAGNOSE], "stop_reason": "tool_use"},
                                    {"content": [{"type": "text", "text": "대상 두 대가 멈췄습니다."}],
                                     "stop_reason": "end_turn"}])
    status, headers, answer = call(api, "POST", "/llm1", body={
        "text": "web-alb에서 503이 나요. 원인 찾아 줘", "requestId": "0f8fad5b-d9cb-469f-a165-70867728950e"})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "diagnoseService" in {tool["name"] for tool in sent[0]["tools"]}
    # 도구 결과는 MCP 서버(HTTP)가 가짜 AWS에서 진단한 것이다
    tool_result = json.dumps(sent[1]["messages"][-1], ensure_ascii=False)
    assert "L5" in tool_result and "StopInstances" in tool_result
    # 감사 로그 탭: 진단 층 그림에 쓰는 진단이 도구 행에 남는다
    status, _, page = call(api, "GET", "/audit")
    row = next(item for item in page["items"] if item.get("tool") == "diagnoseService")
    assert status == 200 and row["diagnosis"]["causes"] == ["L2", "L5"]
    # 진행 상황도 요청한 사람에게 보인다
    status, _, progress = call(api, "GET", "/llm1/progress/0f8fad5b-d9cb-469f-a165-70867728950e")
    assert status == 200 and "diagnoseService" in json.dumps(progress["steps"])


def test_member_does_not_get_admin_tools_even_with_admin_flag(local, monkeypatch):
    _, api = local(admin=True)  # --admin은 /mcp로 직접 붙은 요청에만
    sent = fake_model(monkeypatch, [{"content": [DIAGNOSE], "stop_reason": "tool_use"},
                                    {"content": [{"type": "text", "text": "권한이 없습니다."}], "stop_reason": "end_turn"}])
    status, _, _ = call(api, "POST", "/llm1", role="member", body={"text": "web-alb 원인 찾아 줘"})
    assert status == 200 and "diagnoseService" not in {tool["name"] for tool in sent[0]["tools"]}
    assert "L5" not in json.dumps(sent[1]["messages"][-1], ensure_ascii=False)  # 모델이 불러도 실행되지 않는다
    assert call(api, "GET", "/audit", role="member")[0] == 403


def test_local_users_sessions_and_dashboard(local):
    _, api = local()
    status, _, created = call(api, "POST", "/sessions", body={"title": "503 조사"})
    assert status == 200
    status, _, listing = call(api, "GET", "/sessions")  # 본문 없는 GET
    assert status == 200 and [s["sessionId"] for s in listing["sessions"]] == [created["sessionId"]]
    assert call(api, "GET", "/sessions", role="member")[2]["sessions"] == []  # 사용자마다 따로
    status, _, users = call(api, "GET", "/users")
    assert status == 200 and {u["email"]: u["role"] for u in users["users"]} == {
        "admin@vigie.local": "admin", "decider@vigie.local": "decider", "member@vigie.local": "member"}
    assert call(api, "GET", "/users", role="decider")[0] == 403
    # 홈의 '최근 변경'은 시나리오가 심은 변경 기록 (moto에 CloudTrail 조회가 없다)
    status, _, home = call(api, "GET", "/dashboard")
    assert status == 200 and any("StopInstances" in change["summary"] and change["actor"] == "alice"
                                 for change in home["changes"])


def test_requests_need_a_local_role_and_origin(local):
    _, api = local(has_model=False)
    assert call(api, "GET", "/sessions", role=None)[0] == 401
    assert call(api, "GET", "/health", role=None)[0] == 200
    status, headers, _ = api.handle("OPTIONS", "/llm1", {"Origin": ORIGIN}, "")
    assert status == 204 and "X-Vigie-Local-Role" in headers["Access-Control-Allow-Headers"]
    assert api.handle("OPTIONS", "/llm1", {"Origin": "https://evil.example"}, "")[0] == 403
    status, _, body = call(api, "POST", "/llm1", body={"text": "hi"})
    assert status == 503 and "ANTHROPIC_API_KEY" in body["error"]


def test_http_server_refuses_foreign_hosts(local):
    _, api = local()
    server = api.serve("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/sessions", headers={"X-Vigie-Local-Role": "admin", "Origin": ORIGIN})
        with opener.open(request, timeout=30) as response:
            assert response.status == 200 and response.headers["Access-Control-Allow-Origin"] == ORIGIN
        request = urllib.request.Request(base + "/sessions", headers={"X-Vigie-Local-Role": "admin",
                                                                      "Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError) as refused:
            opener.open(request, timeout=30)
        assert refused.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
