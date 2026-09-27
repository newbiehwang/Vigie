"""그룹별로 쓸 수 있는 도구 (services/llm/tool_access.py, mcp/lambda_mcp/risk.py의 ADMIN_ONLY, docs/threat-model.md R8)

실제 MCP 서버 코드(mcp/app.py)의 tools/list와 tools/call로, 일반 사용자와 관리자가 쓸 수 있는 도구를 확인한다
(Anthropic API는 가짜 응답이라 돈이 들지 않는다. AWS는 moto).

- 목록: tools/list의 모든 도구에 '쓸 수 있는 사람' 표시가 있고, 관리자 전용은 정한 목록(CloudTrail·IAM·네트워크·S3 보안)과 같다
- 일반 사용자: 모델에게 관리자 전용 도구가 보이지 않고(도구 검색으로도), 시스템 프롬프트에 안내가 붙는다
- 이름으로 불러도 거절한다: MCP를 부르지 않고, 거절한 시도가 감사 로그에 남는다
- 관리자: 모든 도구가 보이고, MCP에 관리자의 호출이라고 알린다
- MCP 서버: 관리자 표시가 없는 호출은 관리자 전용 도구를 실행하지 않는다
- Slack 봇 요청은 일반 사용자다. 표시가 없는 예전 목록(tool_cache)은 쓰지 않고 새로 받는다
"""
import copy
import json

import pytest

from test_approvals import ORIGIN, FakeResponse, env  # noqa: F401 (env는 fixture)

# 관리자 전용으로 정한 도구 (docs/threat-model.md R8). 목록을 바꾸면 이 테스트와 문서를 같이 고친다
ADMIN_TOOLS = {
    "lookup_events",
    "list_users", "get_user", "list_roles", "list_policies", "get_managed_policy_document",
    "simulate_principal_policy", "list_groups", "get_group", "get_user_policy", "get_role_policy",
    "list_user_policies", "list_role_policies",
    "get_path_trace_methodology", "find_ip_address", "get_eni_details", "list_vpcs", "get_vpc_network",
    "get_vpc_flow_logs",
    "checkS3BucketSecurity", "listS3Objects",
}

CALL_CLOUDTRAIL = {"type": "tool_use", "id": "toolu_1", "name": "lookup_events", "input": {}}
ANSWER = {"content": [{"type": "text", "text": "답"}], "usage": {}, "stop_reason": "end_turn"}


def real_tools(env):  # noqa: F811
    return json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]


def make_client(monkeypatch, env, replies):  # noqa: F811
    """가짜 Anthropic 응답을 차례로 돌려주는 클라이언트. 보낸 요청은 sent, MCP 호출은 calls에 쌓인다."""
    llm = env["llm"]
    import mcp_anthropic_client
    sent, calls = [], []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse(replies[len(sent) - 1])

    def call_tool(name, args=None, meta=None):
        calls.append((name, meta))
        return {"content": [{"type": "text", "text": '{"events": []}'}]}

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = real_tools(env)
    monkeypatch.setattr(client.mcp_client, "call_tool", call_tool)
    monkeypatch.setattr(llm, "get_client", lambda *_: client)
    return llm, client, sent, calls


def ask(llm, groups=None, sub="alice"):
    body = {"text": "어제 누가 보안 그룹을 바꿨어?"}
    return json.loads(llm.handle_llm1_with_mcp(body, ORIGIN, caller_id=sub, caller_groups=groups)["body"])


def tool_names(request):
    return {tool["name"] for tool in request["tools"] if tool.get("name") != "tool_search_tool_regex"}


def system_text(request):
    system = request["system"]
    return system if isinstance(system, str) else "".join(block["text"] for block in system)


# ---------------------------------------------------------------- 목록 (MCP tools/list)

def test_every_tool_says_who_can_use_it_and_the_admin_list_is_exact(env):  # noqa: F811
    tools = real_tools(env)
    access = {tool["name"]: tool["_meta"]["vigie/access"] for tool in tools}
    assert set(access.values()) == {"all", "admin"}
    # 관리자 전용 목록이 정한 것과 같다. 공식 서버를 올려 새 도구가 생기면 위험도 목록에 넣기 전까지 관리자 전용이다
    assert {name for name, who in access.items() if who == "admin"} == ADMIN_TOOLS
    assert env["risk"].ADMIN_ONLY == ADMIN_TOOLS
    assert env["risk"].access_of("brand_new_official_tool") == "admin"
    # 변경 도구는 모든 사용자가 요청할 수 있다 (실행은 결정자의 승인 뒤)
    assert all(access[name] == "all" for name, risk in env["risk"].TOOL_RISK.items() if risk == "write")


# ---------------------------------------------------------------- 일반 사용자

@pytest.mark.parametrize("search", ["on", "off"])
def test_member_does_not_see_admin_tools(env, monkeypatch, search):  # noqa: F811
    monkeypatch.setenv("TOOL_SEARCH", search)
    llm, client, sent, _ = make_client(monkeypatch, env, [ANSWER])
    ask(llm, groups=["approvers"])  # 결정자도 관리자가 아니다

    names = tool_names(sent[0])
    # 모델에게 주는 목록(도구 검색이 찾는 대상)에 관리자 전용 도구가 없다
    assert names and not (names & ADMIN_TOOLS)
    assert {"describe_log_groups", "cost-explorer", "listEc2Instances", "listS3Buckets"} <= names
    # 쓸 수 없는 도구를 알리는 안내가 붙는다. 캐시되는 본문 뒤의 블록이라 관리자와 같은 캐시를 쓴다
    system = sent[0]["system"]
    if search == "on":
        assert "<Access>" in system[-1]["text"] and "cache_control" not in system[-1]
        assert "<Access>" not in system[0]["text"]
    assert "<Access>" in system_text(sent[0])


def test_member_calling_an_admin_tool_by_name_is_refused_and_audited(env, monkeypatch):  # noqa: F811
    llm, client, sent, calls = make_client(
        monkeypatch, env, [{"content": [CALL_CLOUDTRAIL], "usage": {}, "stop_reason": "tool_use"}, ANSWER])
    body = ask(llm, groups=[])

    # MCP를 부르지 않았다
    assert calls == []
    # 모델에는 거절과 안내를 돌려준다
    result = sent[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "toolu_1"
    assert "관리자(admins 그룹)만" in json.dumps(result, ensure_ascii=False)
    # 거절한 시도가 감사 로그와 화면(도구 단계)에 실패로 남는다
    items = env["audit"].query(KeyConditionExpression="userId = :u",
                               ExpressionAttributeValues={":u": "alice"})["Items"]
    tried = next(item for item in items if item.get("tool") == "lookup_events")
    assert tried["status"] == "error" and "관리자" in tried["error"]
    assert body["inference"]["tools_used"][0]["tool_name"] == "lookup_events"


def test_member_tools_run_without_the_admin_mark(env, monkeypatch):  # noqa: F811
    call = {"type": "tool_use", "id": "toolu_1", "name": "describe_log_groups", "input": {}}
    llm, client, sent, calls = make_client(
        monkeypatch, env, [{"content": [call], "usage": {}, "stop_reason": "tool_use"}, ANSWER])
    ask(llm, groups=None)
    assert calls == [("describe_log_groups", None)]


def test_slack_requests_are_members(env, monkeypatch):  # noqa: F811
    llm, client, sent, _ = make_client(monkeypatch, env, [ANSWER])
    monkeypatch.setattr(llm, "send_slack_dm", lambda user, text: None)
    # Slack 봇은 Lambda를 직접 부른다 (caller_id 없음). 그룹을 넘겨도 쓰지 않는다
    llm.handle_llm1_with_mcp({"text": "q", "user_id": "U1", "previous_questions": [{"role": "user", "content": "x"}]},
                             ORIGIN, caller_groups=["admins"])
    assert client.role == "member" and not (tool_names(sent[0]) & ADMIN_TOOLS)


# ---------------------------------------------------------------- 관리자

def test_admin_sees_every_tool_and_mcp_is_told(env, monkeypatch):  # noqa: F811
    llm, client, sent, calls = make_client(
        monkeypatch, env, [{"content": [CALL_CLOUDTRAIL], "usage": {}, "stop_reason": "tool_use"}, ANSWER])
    ask(llm, groups=["admins"])

    assert ADMIN_TOOLS <= tool_names(sent[0])
    assert "<Access>" not in system_text(sent[0])
    assert calls == [("lookup_events", {"vigie/role": "admin"})]


def test_role_is_set_per_request_on_the_shared_client(env, monkeypatch):  # noqa: F811
    # 클라이언트는 모델별로 캐시되어 여러 요청이 함께 쓴다. 관리자 다음 요청이 일반 사용자면 다시 좁혀야 한다
    llm, client, sent, _ = make_client(monkeypatch, env, [ANSWER, ANSWER])
    ask(llm, groups=["admins"])
    ask(llm, groups=[], sub="bob")
    assert ADMIN_TOOLS <= tool_names(sent[0]) and not (tool_names(sent[1]) & ADMIN_TOOLS)


# ---------------------------------------------------------------- MCP 서버의 재확인

def test_mcp_refuses_admin_tools_without_the_admin_mark(env):  # noqa: F811
    for meta in (None, {"vigie/role": "member"}, {"vigie/role": "ADMIN"}):
        result = env["mcp"].call_tool("lookup_events", {}, meta=meta)
        assert result["isError"] is True and "관리자(admins 그룹)만" in result["content"][0]["text"]
    # 모든 사용자의 도구는 표시 없이 실행된다
    assert not env["mcp"].call_tool("listCloudwatchDashboards", {}).get("isError")


# ---------------------------------------------------------------- 저장해 둔 목록 (tool_cache)

def test_saved_list_without_access_marks_is_not_used(env, monkeypatch):  # noqa: F811
    import mcp_anthropic_client
    old = [{**tool, "_meta": {"vigie/risk": tool["_meta"]["vigie/risk"]}} for tool in real_tools(env)]
    fresh = real_tools(env)

    class Cache:
        def __init__(self, tools):
            self.tools = tools

        def load(self):
            return self.tools

        def save(self, tools):
            return True

    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5", tool_cache=Cache(old))
    monkeypatch.setattr(client, "_connect", lambda: fresh)
    client._prepare_tools()
    # 표시가 없는 예전 목록이면 연결을 기다려 새 목록을 쓴다 (일반 사용자에게 도구가 하나도 안 보이는 일이 없게)
    assert client.tools is fresh and client._connecting is None

    labeled = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                      model_id="claude-sonnet-5", tool_cache=Cache(fresh))
    monkeypatch.setattr(labeled, "_connect", lambda: fresh)
    labeled._prepare_tools()
    assert labeled.tools is fresh and labeled._connecting is not None  # 표시가 있으면 예전처럼 바로 쓴다
    labeled._connecting.result()


def test_role_of_groups():
    import tool_access
    assert tool_access.role_of(["admins", "approvers"]) == "admin"
    assert tool_access.role_of(["approvers"]) == tool_access.role_of([]) == tool_access.role_of(None) == "member"
    assert tool_access.access_of({"name": "x"}) == "admin"  # 표시가 없는 도구는 관리자 전용으로 본다
