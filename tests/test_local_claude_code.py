"""로컬 Claude: Claude Code를 대화의 모델로 쓰고, 도구 호출은 중계 MCP를 거친다 (local/claude_code.py)

실제 claude 대신 가짜 claude(FAKE_CLAUDE)를 실행한다. 가짜도 실제처럼 --mcp-config의 중계 주소에 HTTP로 붙어
initialize → tools/list → tools/call을 하고, stream-json을 출력한다. 나머지(LLM 서버 코드, MCP 서버, 가짜 AWS)는 진짜다.
- 관리자가 물으면 진단이 불리고, 감사 로그에 진단이 남는다. 모델(가짜 claude)이 받는 결과는 계정 ID가 가려져 있다
- 질문에 적은 액세스 키 ID는 가명으로 나가고, 모델이 그 가명으로 부른 진단은 원래 키 ID로 조회한다
- 변경 도구는 실행되지 않고 승인 요청이 된다. 화면에서 승인하면 가짜 AWS의 인스턴스가 실제로 켜진다
- 일반 사용자에게는 관리자 전용 도구가 보이지 않고, 이름으로 불러도 거절된다
- claude는 기본 도구를 끄고 중계 MCP 하나만 붙여, 빈 임시 폴더에서 AWS 자격 증명 없이 실행한다
- 질문이 끝나면 중계 주소(토큰)는 닫힌다
"""
import json
import os
import stat
import sys
import threading
from urllib.parse import urlsplit

import boto3
import pytest

from local import claude_code
from test_local_api import call, local  # noqa: F401 (local은 fixture)

# 가짜 claude: 인자를 기록하고, 중계 MCP를 HTTP로 부른 뒤 stream-json을 출력한다
FAKE_CLAUDE = r'''#!{python}
import json, os, sys, urllib.request
args = sys.argv[1:]
config = json.loads(args[args.index("--mcp-config") + 1])
url = config["mcpServers"]["vigie"]["url"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))

def rpc(method, params=None, notify=False):
    body = {{"jsonrpc": "2.0", "method": method, "params": params or {{}}}}
    if not notify:
        body["id"] = 1
    request = urllib.request.Request(url, json.dumps(body).encode(), {{"Content-Type": "application/json"}})
    with opener.open(request, timeout=60) as response:
        text = response.read()
        return json.loads(text) if text else None

prompt = sys.stdin.read()
rpc("initialize", {{"protocolVersion": "2025-06-18"}})
rpc("notifications/initialized", notify=True)
tools = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
print(json.dumps({{"type": "system", "subtype": "init", "mcp_servers": [{{"name": "vigie", "status": "connected"}}]}}))
calls = json.loads(os.environ.get("FAKE_CLAUDE_CALLS", "[]"))
results = []
for name, arguments in calls:
    print(json.dumps({{"type": "assistant", "message": {{"content": [
        {{"type": "thinking", "thinking": "진단부터 합니다"}},
        {{"type": "tool_use", "id": "t1", "name": "mcp__vigie__" + name, "input": arguments}}]}}}}))
    results.append(rpc("tools/call", {{"name": name, "arguments": arguments}})["result"])
    print(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1"}}]}}}}))
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as log:
    json.dump({{"args": args, "prompt": prompt, "tools": tools, "results": results, "cwd": os.getcwd(),
               "env": sorted(os.environ)}}, log, ensure_ascii=False)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "답변: 원인은 L5입니다",
                  "usage": {{"input_tokens": 120, "output_tokens": 30}}}}))
'''


@pytest.fixture
def claude(local, tmp_path, monkeypatch):  # noqa: F811
    """(api, 실행 기록 읽기, 가짜 claude가 부를 도구 정하기)."""
    stack, api = local()
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    server = api.serve("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log = tmp_path / "claude.json"
    environ = claude_code.claude_environment({**os.environ, "FAKE_CLAUDE_LOG": str(log), "CLAUDECODE": "1",
                                              "CLAUDE_CODE_SESSION_ID": "parent"})
    api.use_claude_code(str(script), f"http://127.0.0.1:{server.server_address[1]}/_local/gateway", environ, None)

    def plan(*calls):
        environ["FAKE_CLAUDE_CALLS"] = json.dumps([list(c) for c in calls])

    yield stack, api, (lambda: json.loads(log.read_text())), plan
    server.shutdown()
    server.server_close()


def ask(api, text, role="admin"):
    status, _, body = call(api, "POST", "/llm1", role=role,
                           body={"text": text, "requestId": "0f8fad5b-d9cb-469f-a165-70867728950e"})
    assert status == 200, body
    return body


def test_diagnosis_through_the_gateway_is_audited_and_redacted(claude):
    stack, api, ran, plan = claude
    plan(("diagnoseService", {"service": "alb", "resource": "web-alb"}))
    answer = ask(api, "web-alb에서 503이 나요. 원인 찾아 줘")
    assert answer["answer"] == "답변: 원인은 L5입니다"
    run = ran()
    assert "diagnoseService" in run["tools"] and "web-alb" in run["prompt"]
    seen = run["results"][0]["content"][0]["text"]
    # 모델이 받는 결과: 데이터 영역으로 감쌌고, 계정 ID(ARN 안)는 가명이다
    assert "StopInstances" in seen and "123456789012" not in seen
    # 감사 로그 탭의 진단 층 그림에 쓰는 진단과 토큰 사용량
    _, _, page = call(api, "GET", "/audit")
    row = next(item for item in page["items"] if item.get("tool") == "diagnoseService")
    assert row["diagnosis"]["causes"] == ["L2", "L5"]
    request = next(item for item in page["items"] if item.get("kind") == "request")
    assert request["model"] == "claude-code:default"  # 가짜 claude는 모델을 고르지 않았다 (model=None)
    # 사고 요약과 도구가 진행 상황에 보인다
    _, _, progress = call(api, "GET", "/llm1/progress/0f8fad5b-d9cb-469f-a165-70867728950e")
    assert "diagnoseService" in json.dumps(progress["steps"], ensure_ascii=False)


def test_leaked_access_key_id_in_the_question_can_be_diagnosed(claude):
    # "액세스 키 AKIA…가 유출됐대요": 모델은 키 ID의 가명만 보고, 그 가명으로 부른 진단은 원래 키 ID로 조회한다
    stack, api, ran, plan = claude
    scenario = stack.apply("credential-persistence")
    key = scenario["resource"]
    alias = key[:4] + "********" + key[-4:]
    plan(("diagnoseService", {"service": "credential", "resource": alias}))
    ask(api, scenario["ask"])
    run = ran()
    assert alias in run["prompt"] and key not in run["prompt"]
    seen = run["results"][0]["content"][0]["text"]
    assert run["results"][0].get("isError") is not True and "IAM 사용자가 없습니다" not in seen
    assert key not in seen  # 결과 속의 키 ID도 가명으로 나간다
    # 감사 로그에는 도구가 실제로 받은 키 ID와 층별 판정이 남는다
    _, _, page = call(api, "GET", "/audit")
    row = next(item for item in page["items"] if item.get("tool") == "diagnoseService")
    assert row["input"]["resource"] == key
    assert row["diagnosis"]["causes"] == scenario["causes"] == ["L2", "L6"]
    request = next(item for item in page["items"] if item.get("kind") == "request")
    assert request["redacted"]["aws_access_key_id"] >= 1


def test_write_tool_becomes_an_approval_and_runs_after_approval(claude):
    stack, api, ran, plan = claude
    web_1 = stack.world["instances"]["web-1"]
    plan(("setEc2InstanceState", {"instance_id": web_1, "action": "start"}))
    answer = ask(api, "web-1을 다시 켜 줘")
    assert "approval_required" in ran()["results"][0]["content"][0]["text"]
    ec2 = boto3.client("ec2", region_name=stack.region)
    state = lambda: ec2.describe_instances(InstanceIds=[web_1])["Reservations"][0]["Instances"][0]["State"]["Name"]  # noqa: E731
    assert state() == "stopped"  # 승인 전에는 바뀌지 않는다
    action_id = answer["inference"]["pendingActions"][0]["actionId"]
    assert call(api, "POST", f"/actions/{action_id}/approve")[0] == 200  # 로컬은 dev처럼 본인 요청도 승인할 수 있다
    assert state() == "running"


def test_member_cannot_see_or_call_admin_tools(claude):
    _, api, ran, plan = claude
    plan(("diagnoseService", {"service": "alb", "resource": "web-alb"}))
    ask(api, "web-alb 원인 찾아 줘", role="member")
    run = ran()
    assert "diagnoseService" not in run["tools"]
    assert run["results"][0]["isError"] is True and "L5" not in json.dumps(run["results"], ensure_ascii=False)


def test_claude_runs_with_only_the_gateway(claude):
    _, api, ran, plan = claude
    plan()
    ask(api, "안녕")
    run = ran()
    args = run["args"]
    assert args[args.index("--tools") + 1] == ""  # 기본 도구(파일·명령·웹)를 모두 끈다
    assert "--restricted" in args and "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__vigie"
    assert "Vigie" in args[args.index("--system-prompt") + 1]
    assert "vigie-claude-" in run["cwd"] and not os.path.exists(run["cwd"])  # 빈 임시 폴더, 끝나면 지운다
    assert not [name for name in run["env"] if name.startswith("AWS_")]
    assert "CLAUDECODE" not in run["env"] and "CLAUDE_CODE_SESSION_ID" not in run["env"]  # 부모 세션의 값
    # 질문이 끝나면 중계 주소는 닫힌다
    config = json.loads(args[args.index("--mcp-config") + 1])
    gateway_path = urlsplit(config["mcpServers"]["vigie"]["url"]).path
    status, _, _ = api.handle("POST", gateway_path, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert status == 404


def test_previous_turns_are_sent_as_a_transcript(claude):
    _, api, ran, plan = claude
    plan()
    _, _, session = call(api, "POST", "/sessions", body={"title": "t"})
    session_id = session["sessionId"]
    for sender, text in (("user", "web-alb가 이상해요"), ("assistant", "대상 두 대가 멈췄습니다")):
        call(api, "POST", f"/sessions/{session_id}/messages", body={"sender": sender, "text": text})
    status, _, _ = call(api, "POST", "/llm1", body={"text": "그럼 어떻게 해?", "sessionId": session_id, "isCached": True,
                                                   "requestId": "0f8fad5b-d9cb-469f-a165-70867728950e"})
    prompt = ran()["prompt"]
    assert status == 200 and "대상 두 대가 멈췄습니다" in prompt and prompt.rstrip().endswith("그럼 어떻게 해?")


def test_environment_keeps_user_settings_outside_claude_code():
    environ = claude_code.claude_environment({"HOME": "/h", "AWS_PROFILE": "prod", "CLAUDE_CODE_USE_BEDROCK": "1"})
    # Claude Code 밖(사용자 터미널)에서 띄웠으면 사용자의 Claude Code 설정은 그대로 둔다
    assert environ == {"HOME": "/h", "CLAUDE_CODE_USE_BEDROCK": "1", "ENABLE_TOOL_SEARCH": "false"}
