"""로컬 Claude: 이 맥에 로그인된 Claude Code를 대화의 모델로 쓴다 (API 키 없이, 로컬 실행 전용).

    화면 ──/llm1──▶ 로컬 API 서버 ──▶ llm_service.handle_llm1_with_mcp (배포 코드 그대로)
                                           │ get_client()만 ClaudeCodeClient로 바꿔 끼운다
                                           ▼
                           claude -p (질문마다 한 번 실행, 이 맥의 Claude Code 로그인)
                                           │ 도구 호출
                                           ▼
            중계 MCP  http://127.0.0.1:8787/_local/gateway/<요청마다 새 토큰>/mcp   (Gateway)
                                           │ 배포에서 모델의 도구 호출이 거치는 함수를 그대로 부른다
                                           │ (AnthropicMCPClient의 _start_tool → 권한 · 승인 요청 · 가리기 → _finish_tool)
                                           ▼
                           로컬 MCP 서버 /mcp/server → 가짜 AWS

중계를 두는 까닭: Claude Code가 MCP 서버에 바로 붙으면 Vigie의 안전장치를 모두 건너뛴다.
중계를 거치면 배포와 똑같이 남는다.
- 변경 도구는 실행하지 않고 승인 요청을 만든다
- 도구 결과의 계정 ID·비밀 값은 가린 뒤 모델에 넘기고, 모델이 돌려준 가명은 원래 값으로 되돌려 부른다
- 결과에 지시문처럼 보이는 문구가 있으면 경고를 붙여 감싼다
- 도구마다 감사 로그와 진행 상황을 남긴다 (진단 도구는 감사 로그의 진단 층 그림까지)
- 일반 사용자에게는 관리자 전용 도구를 보이지 않는다

Claude Code 실행 옵션: 모델이 쓸 수 있는 것은 중계 MCP의 도구뿐이다.
- --tools "": 기본 도구(파일 · 명령 실행 · 웹)를 모두 끈다
- --restricted: 명령 실행 도구를 한 번 더 막고, 사용자 · 프로젝트 설정(훅 등)을 읽지 않는다
- --strict-mcp-config + --mcp-config: 이 중계 MCP 하나만 붙인다
- --allowedTools mcp__vigie: 중계 MCP의 도구는 묻지 않고 부른다 (거르는 일은 중계가 한다)
- --system-prompt: Vigie의 시스템 프롬프트로 바꾼다. 빈 임시 폴더에서 실행해 이 저장소의 CLAUDE.md를 읽지 않는다
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

GATEWAY_PATH = "/_local/gateway/"
SERVER_NAME = "vigie"  # Claude Code가 보는 도구 이름: mcp__vigie__<도구>
DEFAULT_MODEL = "sonnet"  # 배포와 같은 Sonnet (Claude Code의 별칭: 최신 Sonnet)
TIMEOUT_SECONDS = 600


class Gateway:
    """요청마다 새 토큰 → 그 요청의 클라이언트. 토큰을 아는 것은 그 요청의 claude 프로세스뿐이다."""

    def __init__(self):
        self._clients: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def open(self, client) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._clients[token] = client
        return token

    def close(self, token: str) -> None:
        with self._lock:
            self._clients.pop(token, None)

    def handle(self, path: str, body: str) -> tuple:
        """(상태 코드, 머리글, 본문). path: /_local/gateway/<토큰>/mcp"""
        parts = path[len(GATEWAY_PATH):].split("/")
        with self._lock:
            client = self._clients.get(parts[0]) if len(parts) == 2 and parts[1] == "mcp" else None
        if client is None:
            return 404, {}, json.dumps({"error": "없는 중계 주소입니다 (질문이 끝났거나 토큰이 틀렸습니다)"},
                                       ensure_ascii=False)
        try:
            request = json.loads(body or "{}")
        except ValueError:
            return 400, {}, _rpc_error(None, -32700, "본문이 JSON이 아닙니다")
        if not isinstance(request, dict) or "method" not in request:
            return 400, {}, _rpc_error(None, -32600, "JSON-RPC 요청이 아닙니다")
        method, params, request_id = request["method"], request.get("params") or {}, request.get("id")
        if request_id is None:  # 알림 (notifications/initialized 등)
            return 202, {}, ""
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "vigie-local-gateway", "version": "local"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": client.gateway_tools()}
        elif method == "tools/call":
            result = client.gateway_call(params.get("name", ""), params.get("arguments") or {})
        else:
            return 200, {}, _rpc_error(request_id, -32601, f"지원하지 않는 메서드입니다: {method}")
        return 200, {"Content-Type": "application/json"}, json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False)


def _rpc_error(request_id, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
                      ensure_ascii=False)


# Claude Code 안(데스크톱 앱 · 다른 claude 세션)에서 local.stack을 띄웠을 때 물려받는 부모 세션의 값.
# 그대로 넘기면 자식 claude가 부모 세션의 인증·연결을 쓰려다 실패한다
PARENT_SESSION_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_PREVIEW_")
PARENT_SESSION_NAMES = {"CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_PID", "CLAUDE_EFFORT", "USE_LOCAL_OAUTH",
                        "USE_STAGING_OAUTH", "ANTHROPIC_BASE_URL"}


def claude_environment(original: Dict[str, str]) -> Dict[str, str]:
    """claude 프로세스의 환경 변수: 로컬 서버를 띄우기 전의 환경(모델 API에 닿아야 한다)에서 AWS 자격 증명을 뺀다.
    Claude Code 안에서 띄웠으면 부모 세션의 값도 뺀다. MCP 도구는 처음부터 모두 싣는다 (도구 검색은 기본 도구를 꺼서 쓸 수 없다)."""
    nested = "CLAUDECODE" in original
    environ = {name: value for name, value in original.items()
               if not name.startswith("AWS_")
               and not (nested and (name in PARENT_SESSION_NAMES or name.startswith(PARENT_SESSION_PREFIXES)))}
    environ["ENABLE_TOOL_SEARCH"] = "false"
    return environ


def find_claude() -> Optional[str]:
    return shutil.which("claude")


def logged_in(command: str, environ: Dict[str, str]) -> Optional[bool]:
    """claude auth status로 로그인했는지 (알 수 없으면 None). 데스크톱 앱의 로그인과 claude 명령의 로그인은 따로다."""
    try:
        done = subprocess.run([command, "auth", "status"], capture_output=True, text=True, env=environ, timeout=30,
                              cwd=tempfile.gettempdir())
        return bool(json.loads(done.stdout).get("loggedIn"))
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
        return None


def make_client_class():
    """ClaudeCodeClient를 만든다. services/llm의 모듈(mcp_anthropic_client 등)은 로컬 API가 경로를 잡은 뒤에야
    불러올 수 있어서 클래스도 그때 만든다."""
    import injection
    import tool_access
    from mcp_anthropic_client import MAX_TAINTED, AnthropicMCPClient

    class ClaudeCodeClient(AnthropicMCPClient):
        """AnthropicMCPClient에서 모델을 부르는 부분(invoke_with_tools)만 claude -p로 바꾼다.
        도구 하나를 부르는 절차(_start_tool · _request_approval · _call_tool_timed · _finish_tool)는 그대로 쓴다."""

        supports_system_blocks = False  # 시스템 프롬프트를 글 한 덩어리로 받는다 (--system-prompt)

        def __init__(self, mcp_url: str, gateway: Gateway, gateway_url: str, command: str,
                     environ: Dict[str, str], model: Optional[str] = DEFAULT_MODEL, timeout: int = TIMEOUT_SECONDS):
            super().__init__(mcp_url, api_key=None, model_id=f"claude-code:{model or 'default'}")
            self.tool_search = False
            self.gateway, self.gateway_url = gateway, gateway_url.rstrip("/")
            self.command, self.environ, self.model, self.timeout = command, environ, model, timeout
            self._gateway_lock = threading.Lock()  # 진행 상황·감사 로그는 한 번에 하나씩 쓰는 기록이다
            self.last_command: List[str] = []

        # ------------------------------------------------------------ 중계 MCP
        def gateway_tools(self) -> List[Dict[str, Any]]:
            """이 요청자가 쓸 수 있는 도구만 (일반 사용자에게 관리자 전용 도구를 보이지 않는다)."""
            self._ensure_connected()
            return [{"name": tool["name"], "description": tool.get("description", ""),
                     "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}}}
                    for tool in tool_access.visible(self.tools, self.role)]

        def gateway_call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            """도구 하나: 배포의 _run_tools가 도구마다 하는 일과 같다. 모델에 돌려줄 MCP 결과(가린 글)를 돌려준다."""
            with self._gateway_lock:
                self._ensure_connected()
                tool_use = {"id": f"toolu_local_{uuid.uuid4().hex[:20]}", "name": name, "input": arguments}
                try:
                    prepared = self._start_tool(tool_use)
                    if not self._can_use(name):
                        result = tool_access.denied_result(name)
                    elif prepared["risk"] == "write":
                        result = self._request_approval(name, prepared["input"], self._tainted_by(prepared["call_no"]))
                    else:
                        restored = self.redactor.restore(prepared["input"]) if prepared["restore"] else prepared["input"]
                        result = self._call_tool_timed(name, restored)
                        if prepared["risk"] == "artifact" and self.artifacts is not None:
                            result = self.artifacts.take(name, result)
                    finished = self._finish_tool(prepared, result)
                except Exception as error:
                    self._tool_failed(tool_use, error)
                    return {"isError": True, "content": [{"type": "text", "text": self.redactor.text(str(error))}]}
                text = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
                if finished["suspicious"]:
                    self._seen_suspicious = (self._seen_suspicious + [
                        {"toolUseId": finished["tool_id"], "tool": name, "kinds": finished["suspicious"],
                         "callNo": finished["call_no"]}])[-MAX_TAINTED:]
                # 계정 밖(모델)으로 나가는 곳: 가리고, 데이터 영역으로 감싼다 (배포의 tool_result와 같다)
                return {"content": [{"type": "text", "text": injection.wrap(name, self.redactor.text(text),
                                                                             finished["suspicious"])}],
                        "isError": isinstance(result, dict) and result.get("isError") is True}

        # ------------------------------------------------------------ 모델 (claude -p)
        def invoke_with_tools(self, prompt: str, system_prompt: str = None, previous_messages: list = None):
            self._prepare_tools()
            if system_prompt:
                self.system_prompt = system_prompt
            # 질문과 이전 대화도 모델로 나가므로 가린다 (배포와 같다)
            self.messages = [self._redact_message(m) for m in previous_messages or []]
            prompt = self.redactor.text(prompt)
            self.debug_log = [{"type": "user_input", "content": prompt, "timestamp": time.time()}]
            self.total_input_tokens = self.total_output_tokens = 0
            self.total_cache_read_tokens = self.total_cache_write_tokens = 0
            self.model_calls, self.tool_search_count = [], 0
            self._tool_calls, self._seen_suspicious = 0, []

            token = self.gateway.open(self)
            try:
                text = self._run_claude(self._system_prompt(system_prompt) or "", self._conversation(prompt), token)
            finally:
                self.gateway.close(token)
            self.messages.append({"role": "user", "content": prompt})
            self.messages.append({"role": "assistant", "content": text})
            return {"output": {"message": {"content": [{"text": text}]}}}

        def _conversation(self, prompt: str) -> str:
            """claude -p에 넣을 글: 이전 대화(가린 것)와 이번 질문. 세션을 이어 가지 않고 질문마다 새로 연다."""
            if not self.messages:
                return prompt
            lines = ["지금까지의 대화입니다 (앞의 것부터):", ""]
            for message in self.messages:
                speaker = "사용자" if message.get("role") == "user" else "Vigie"
                lines += [f"[{speaker}]", self._text_of(message.get("content")), ""]
            return "\n".join(lines + ["이번 질문입니다:", prompt])

        def command_line(self, system_prompt: str, token: str) -> List[str]:
            config = {"mcpServers": {SERVER_NAME: {"type": "http", "url": f"{self.gateway_url}/{token}/mcp"}}}
            command = [self.command, "-p", "--output-format", "stream-json", "--verbose",
                       "--tools", "", "--restricted", "--strict-mcp-config", "--mcp-config", json.dumps(config),
                       "--allowedTools", f"mcp__{SERVER_NAME}", "--no-session-persistence",
                       "--disable-slash-commands", "--system-prompt", system_prompt]
            return command + (["--model", self.model] if self.model else [])

        def _run_claude(self, system_prompt: str, conversation: str, token: str) -> str:
            self.last_command = self.command_line(system_prompt, token)
            workdir = tempfile.mkdtemp(prefix="vigie-claude-")  # 저장소의 CLAUDE.md·설정을 읽지 않도록 빈 폴더
            self._report("thinking_started")
            final, errors, stderr = "", [], []
            try:
                process = subprocess.Popen(self.last_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, cwd=workdir, env=self.environ)
            except OSError as error:
                shutil.rmtree(workdir, ignore_errors=True)
                return f"Claude Code를 실행하지 못했습니다: {error}"
            # stderr는 따로 비운다 (파이프가 차면 claude가 멈춘다)
            drain = threading.Thread(target=lambda: stderr.extend(process.stderr), daemon=True)
            drain.start()
            timer = threading.Timer(self.timeout, process.kill)  # 너무 오래 걸리면 끝낸다
            timer.start()
            try:
                process.stdin.write(conversation)
                process.stdin.close()
                for line in process.stdout:
                    final = self._read_event(line, final, errors)
                process.wait()
                drain.join(timeout=5)
            finally:
                timer.cancel()
                shutil.rmtree(workdir, ignore_errors=True)
            if errors or (process.returncode and not final):
                detail = "; ".join(errors) or "".join(stderr).strip()[-500:] or f"종료 코드 {process.returncode}"
                return f"Claude Code 오류: {detail}"
            return final

        def _read_event(self, line: str, final: str, errors: List[str]) -> str:
            """stream-json 한 줄. 사고 요약은 진행 상황으로, 글은 디버그 기록으로, 끝 결과는 답변으로."""
            try:
                event = json.loads(line)
            except ValueError:
                return final
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                servers = {s.get("name"): s.get("status") for s in event.get("mcp_servers") or []}
                if servers.get(SERVER_NAME) != "connected":
                    errors.append(f"중계 MCP에 붙지 못했습니다 ({servers.get(SERVER_NAME) or '없음'})")
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "thinking" and block.get("thinking"):
                        self._report("thought", block["thinking"])
                    elif block.get("type") == "text" and block.get("text"):
                        self.debug_log.append({"type": "model_reasoning", "content": block["text"],
                                               "timestamp": time.time()})
            elif kind == "user":
                self._report("thinking_started")  # 도구 결과를 받은 모델이 다시 생각한다
            elif kind == "result":
                self._add_usage(event.get("usage") or {})
                if event.get("is_error"):
                    errors.append(str(event.get("result") or event.get("subtype")))
                return event.get("result") or final
            return final

    return ClaudeCodeClient
