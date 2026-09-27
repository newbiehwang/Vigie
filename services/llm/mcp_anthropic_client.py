import json
import time
import re
import requests
from typing import Dict, Any, List, Optional
from mcp_client import MCPClient
from redaction import Redactor
from approvals import PREVIEW_META, is_registered, risk_of
import injection
import tool_search


# 응답 한 번의 최대 출력 토큰. 사고 과정(thinking)도 이 안에서 쓰므로 사고를 켠 뒤 8192에서 늘렸다
MAX_TOKENS = 16000
# 승인 요청에 적는 '먼저 읽은 의심 결과'의 최대 수 (최근 것부터). 승인 테이블 항목 크기를 제한한다
MAX_TAINTED = 5
# Messages API 연결을 컨테이너 안에서 다시 쓴다 (요청마다 TLS 연결을 새로 맺지 않게). requests.post는 부를 때마다
# 새 세션을 만들어 연결을 버린다. 제한 시간: 연결 10초, 응답을 기다리는 시간 300초 (사고가 긴 답변도 끊기지 않게)
HTTP = requests.Session()
HTTP_TIMEOUT = (10, 300)


class AnthropicMCPClient:
    """Anthropic API와 통합된 MCP 클라이언트 (SDK 없이 직접 API 호출)"""

    def __init__(self, mcp_url: str, api_key: str = None, model_id: str = None,
                 session_id: str = None, max_retries: int = 5, max_iterations: int = 15,
                 thinking: Optional[Dict[str, Any]] = None):
        """
        Anthropic MCP 클라이언트 초기화

        Args:
            mcp_url: MCP 서버 URL
            api_key: Anthropic API 키
            model_id: Anthropic 모델 ID (필수. 호출하는 쪽이 llm_service.resolve_model_id로 정한다)
            session_id: 기존 세션 ID (선택 사항)
            max_retries: 작업 상태 확인을 위한 최대 재시도 횟수
            max_iterations: 도구 호출을 위한 최대 반복 횟수
            thinking: 요청에 넣을 사고 설정 (llm_service.thinking_config가 모델에 맞게 정한다. None이면 넣지 않음)
        """
        self.mcp_client = MCPClient(mcp_url, None, session_id)
        if not model_id:
            # 기본값을 여기 적어 두면 그 모델이 퇴역하는 날 조용히 실패한다
            raise ValueError("model_id가 필요합니다")
        self.model_id = model_id
        self.api_key = api_key
        self.api_url = "https://api.anthropic.com/v1/messages"
        self.api_headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        self.tools = []
        self.messages = []
        self.max_retries = max_retries
        self.max_iterations = max_iterations
        self.pending_tasks = {}  # 대기 중인 작업 ID 및 상태 추적
        self.system_prompt = None
        self.debug_log = []  # 디버그 로그 추가 - 사고 과정과 도구 사용 추적
        # 토큰 사용량 누적 추적. 캐시에서 읽은·캐시에 쓴 입력은 input_tokens와 따로 온다 (도구 검색 전후 비교에 쓴다)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_write_tokens = 0
        # 모델을 부를 때마다의 토큰 (감사 로그의 질문 행에 남긴다, usage_summary)
        self.model_calls: List[Dict[str, int]] = []
        self.tool_search_count = 0
        self.thinking = thinking
        # 도구 검색 (tool_search.py). 모델이 지원하지 않아 거절되면 이 클라이언트(모델별로 캐시된다)에서는 끈다
        self.tool_search = tool_search.enabled_by_env()
        # 요청마다 llm_service가 넣어 주는 진행 상황 기록 (llm_progress.ProgressReporter).
        # 클라이언트는 모델별로 캐시해 여러 요청이 함께 쓰므로 생성자가 아니라 요청마다 바꿔 끼운다
        self.progress = None
        # 요청마다 llm_service가 새로 넣어 주는 민감정보 가리기 (redaction.Redactor). 가명 표가 요청마다 따로여야 한다
        self.redactor = Redactor()
        # 요청마다 llm_service가 넣어 주는 감사 로그 (audit.AuditLog). 진행 상황과 같은 지점에서 기록한다
        self.audit = None
        # 요청마다 llm_service가 넣어 주는 승인 요청 (approvals.ApprovalRequester). 없으면(Slack 등) 변경 도구를 쓸 수 없다
        self.approvals = None
        # 요청마다 llm_service가 넣어 주는 결과물 목록 (artifacts.Artifacts). 없으면 예전처럼 주소를 모델에 넘긴다
        self.artifacts = None
        # 이번 질문에서 부른 도구 수와, 모델이 이미 읽은 의심 결과 (체류 신호, invoke_with_tools가 질문마다 비운다)
        self._tool_calls = 0
        self._seen_suspicious: List[Dict[str, Any]] = []
        # 요청마다 llm_service가 넣어 주는 시간 기록 (timing.Stopwatch). 모델 호출·도구마다 걸린 시간을 모은다
        self.timer = None

    # system에 블록 목록(system_prompt.build_system_blocks: 캐시되는 본문 + 요청 시각)을 받는다
    supports_system_blocks = True

    def _time(self, name: str):
        """걸린 시간을 재는 with 블록 (시간 기록이 없으면 재지 않는다)."""
        from timing import Stopwatch
        return (self.timer or Stopwatch.off()).step(name)

    def _report(self, event: str, *args) -> None:
        """한 단계를 진행 상황과 감사 로그에 알린다 (기록할 곳이 없으면 아무것도 하지 않는다).
        - 진행 상황은 화면과 대화 기록에 남으므로 도구 오류 메시지 등을 가린 뒤에 넘긴다.
        - 감사 로그는 원래 값을 받아 스스로 가린다 (비밀 값만 가리고 계정 ID 등은 남긴다).
          감사 로그에 없는 단계(사고 요약 등)는 넘기지 않는다."""
        if self.progress is not None:
            getattr(self.progress, event)(*self.redactor.redact(list(args)))
        if self.audit is not None and hasattr(self.audit, event):
            getattr(self.audit, event)(*args)

    def _tool_definition(self, name: str) -> Optional[Dict[str, Any]]:
        return next((tool for tool in self.tools if tool.get("name") == name), None)

    def _tool_risk(self, name: str) -> str:
        """MCP tools/list가 알려 준 위험도 (mcp/lambda_mcp/risk.py). 모르는 도구는 변경 도구로 본다."""
        return risk_of(self._tool_definition(name))

    def _tainted_by(self, call_no: int) -> List[Dict[str, Any]]:
        """체류 신호: 이 변경 요청 전에 모델이 읽은 의심 결과와, 그 뒤로 몇 번째 도구 호출인지 (approvals 모듈 설명).
        같은 응답에서 함께 부른 도구의 결과는 모델이 아직 보지 못했으므로 넣지 않는다 (배치가 끝난 뒤에 더한다)."""
        return [{"toolUseId": seen["toolUseId"], "tool": seen["tool"], "kinds": seen["kinds"],
                 "callsAgo": call_no - seen["callNo"]} for seen in self._seen_suspicious]

    def _request_approval(self, tool_name: str, tool_input: Dict[str, Any],
                          tainted_by: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """변경 도구: 실행하지 않고 승인 요청을 만든다. 모델에 돌려줄 도구 결과(MCP 형식)를 돌려준다.

        1. 승인 요청을 받을 수 없는 경로(Slack 등)면 거절한다.
        2. MCP에 미리 보기만 받는다 (지금 상태 → 바뀔 상태, 범위 밖 리소스는 여기서 거절된다).
        3. 승인 요청을 저장하고, 모델에는 '승인 대기 중'을 알린다. 모델은 이것으로 답변을 마무리한다.
        """
        def error(text: str) -> Dict[str, Any]:
            return {"isError": True, "content": [{"type": "text", "text": text}]}

        if self.approvals is None:
            return error("이 경로(Slack 등)에서는 AWS를 바꾸는 작업을 요청할 수 없습니다. "
                         "웹 화면에서 요청하면 사용자가 승인한 뒤 실행됩니다.")
        # 모델은 가명(********9012 등)만 안다. 승인 요청에는 실제로 실행될 값을 저장한다
        args = self.redactor.restore(tool_input)
        preview_result = self.mcp_client.call_tool(tool_name, args, meta={PREVIEW_META: True})
        if isinstance(preview_result, dict) and preview_result.get("isError"):
            return preview_result  # 범위 밖 리소스·잘못된 값: 모델이 읽고 사용자에게 설명한다
        try:
            preview = json.loads(self._tool_error_text(preview_result))
        except ValueError:
            preview = {}
        action = self.approvals.request(tool_name, args, preview, tainted_by)
        return {"content": [{"type": "text", "text": json.dumps({
            "status": "approval_required",
            "actionId": action["actionId"],
            "summary": action.get("summary"),
            "message": ("아직 실행하지 않았습니다. 결정자(승인 권한이 있는 사용자)가 화면의 승인 요청에서 승인해야 실행됩니다 "
                        "(10분 안에). "
                        "무엇이 바뀌는지와 승인이 필요하다는 것을 사용자에게 알리고 답변을 마치세요. "
                        "같은 작업을 다시 요청하지 마세요."),
        }, ensure_ascii=False)}]}

    def _redact_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """이전 대화의 메시지 하나를 가린 사본. 사고 블록은 서명과 함께 받은 그대로 보내야 하므로 건드리지 않는다
        (사고 블록은 이미 가린 입력으로 모델이 만든 것이다)."""
        content = message.get("content")
        if isinstance(content, list):
            content = [block if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking")
                       else self.redactor.redact(block) for block in content]
        else:
            content = self.redactor.redact(content)
        return {**message, "content": content}

    @staticmethod
    def _text_of(content) -> str:
        """메시지 content(글자 또는 블록 목록)에서 글자만 모은다."""
        if isinstance(content, str):
            return content
        return "".join(block.get("text", "") for block in content or []
                       if isinstance(block, dict) and block.get("type") == "text")

    def initialize(self) -> str:
        """
        MCP 세션 초기화 및 도구 목록 로드

        Returns:
            세션 ID
        """
        session_id = self.mcp_client.initialize()
        self.tools = self.mcp_client.list_tools()
        return session_id

    def _convert_tools_format(self):
        """
        MCP 도구 형식을 Anthropic 도구 형식으로 변환

        Returns:
            Anthropic API 형식의 도구 목록
        """
        anthropic_tools = []
        for tool in self.tools:
            # MCP 입력 스키마(JSON Schema)를 그대로 넘긴다. 예전에는 속성마다 type·description만 남겼는데,
            # AWS 공식 MCP 도구는 배열 안의 객체(items), 선택 인자(anyOf), 선택지(enum)를 쓰므로 그 정보를 지우면
            # 모델이 인자를 엉뚱한 모양으로 보낸다. ($ref는 MCP 서버가 미리 풀어서 보낸다: mcp/lambda_mcp/official.py)
            schema = dict(tool.get('inputSchema') or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})

            anthropic_tools.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": schema,
            })

        # 도구 목록을 프롬프트 캐시에 올리고(마지막으로 싣는 도구에 표시하면 그 앞까지 캐시된다),
        # 도구 검색을 켰으면 자주 쓰는 도구만 처음부터 싣고 나머지는 모델이 찾을 때 싣는다 (tool_search.py).
        # AWS 공식 MCP 도구는 설명이 길어 도구 목록만 수만 토큰이고, 질문 하나에서도 도구를 부를 때마다 다시 보낸다
        return tool_search.build(anthropic_tools, self.tool_search)

    def _system_prompt(self, system_prompt):
        """요청에 넣을 시스템 프롬프트. 도구 검색을 켰으면 찾는 방법을 덧붙인다.
        블록 목록(캐시되는 본문 + 요청 시각)이면 찾는 방법을 본문 블록 끝에 붙인다 (캐시되는 앞부분에 들어가게)."""
        if not system_prompt or not self.tool_search:
            return system_prompt
        if isinstance(system_prompt, list):
            blocks = [dict(block) for block in system_prompt]
            blocks[0]["text"] = blocks[0]["text"] + tool_search.SYSTEM_HINT
            return blocks
        return system_prompt + tool_search.SYSTEM_HINT

    def _post(self, payload: Dict[str, Any], system_prompt: Optional[str]):
        """Messages API 요청. 모델이 도구 검색을 받지 않으면(400) 끄고 모든 도구를 실어 한 번 다시 보낸다.
        self.tool_search를 끄므로 같은 질문의 다음 반복과 이 모델의 다음 질문도 모든 도구로 보낸다.
        거절은 첫 요청에서 오므로 대화에 검색 블록이 남아 있지 않다."""
        response = HTTP.post(self.api_url, headers=self.api_headers, json=payload, timeout=HTTP_TIMEOUT)
        if self.tool_search and tool_search.is_unsupported(response.status_code, response.text):
            print(f"도구 검색을 쓸 수 없어 모든 도구를 싣고 다시 보냅니다 ({self.model_id}): {response.text[:300]}")
            self.tool_search = False
            payload["tools"] = self._convert_tools_format()
            if system_prompt:
                payload["system"] = system_prompt
            response = HTTP.post(self.api_url, headers=self.api_headers, json=payload, timeout=HTTP_TIMEOUT)
        return response

    def _is_response_complete(self, message_content: str, tool_uses: List) -> bool:
        """
        응답이 완전한지 확인

        Args:
            message_content: 모델의 텍스트 응답
            tool_uses: 도구 호출 목록

        Returns:
            응답이 완전한지 여부
        """
        # 도구 호출이 있으면 아직 진행 중
        if tool_uses:
            return False

        # 응답이 너무 짧거나 비어있으면 불완전
        if not message_content or len(message_content.strip()) < 50:
            return False

        # 중간 응답을 나타내는 패턴들
        incomplete_patterns = [
            r'더\s*(자세한|구체적인|정확한)\s*정보를?\s*(찾아보겠습니다|확인해보겠습니다|알아보겠습니다)',
            r'추가\s*(검색|정보)를?\s*(해보겠습니다|확인해보겠습니다)',
            r'좀\s*더\s*.+\s*(해보겠습니다|확인해보겠습니다)',
            r'에\s*대한\s*정보를?\s*(찾아보겠습니다|확인해보겠습니다)',
            r'관한\s*정보를?\s*(찾아보겠습니다|확인해보겠습니다)',
            r'검색을?\s*(해보겠습니다|진행하겠습니다)',
            r'확인하기\s*위해',
            r'알아보기\s*위해',
            r'찾기\s*위해',
        ]

        for pattern in incomplete_patterns:
            if re.search(pattern, message_content):
                print(f"불완전한 응답 패턴 감지: {pattern}")
                return False

        # 완전한 답변의 특징들
        complete_indicators = [
            r'방법은?\s*(다음과\s*같습니다|아래와\s*같습니다)',
            r'단계는?\s*(다음과\s*같습니다|아래와\s*같습니다)',
            r'절차는?\s*(다음과\s*같습니다|아래와\s*같습니다)',
            r'\d+\.\s*.+',  # 번호가 매겨진 목록
            r'##\s*.+',  # 제목/섹션
            r'```',  # 코드 블록
            r'요약하면',
            r'정리하면',
            r'결론적으로',
            r'마지막으로',
        ]

        for pattern in complete_indicators:
            if re.search(pattern, message_content):
                print(f"완전한 응답 패턴 감지: {pattern}")
                return True

        # 응답이 충분히 길고 특별한 패턴이 없으면 완전한 것으로 간주
        if len(message_content.strip()) > 200:
            return True

        return False

    def _request_complete_answer(self) -> str:
        """
        완전한 답변을 요청하는 메시지 생성

        Returns:
            완전한 답변 요청 메시지
        """
        return ("이전 응답들을 바탕으로 사용자의 원래 질문에 대한 완전하고 구체적인 답변을 제공해주세요. "
                "단계별 방법, 필요한 설정, 예시 코드나 명령어 등을 포함하여 실용적인 가이드를 작성해주세요.")

    def _check_task_completion(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """
        응답에서 대기 중인 작업을 확인하고 완료될 때까지 대기

        Args:
            response: Anthropic 모델 응답

        Returns:
            최종 응답
        """
        # 응답 텍스트 추출
        response_text = ""
        if "content" in response:
            if isinstance(response["content"], list):
                for item in response["content"]:
                    if isinstance(item, str):
                        response_text += item
                    elif isinstance(item, dict) and "text" in item:
                        response_text += item["text"]
            elif isinstance(response["content"], str):
                response_text = response["content"]

        # 작업 ID 패턴 탐지
        task_patterns = [
            r'task[_\s]id["\s:]+([a-zA-Z0-9_-]+)',  # task_id: "abc123"
            r'execution[_\s]arn["\s:]+([a-zA-Z0-9:/_-]+)',  # execution_arn: "arn:aws:..."
            r'status["\s:]+pending',  # status: "pending"
            r'status["\s:]+in[_\s]progress'  # status: "in_progress"
        ]

        pending_task_detected = False
        task_ids = []

        # 작업 ID 추출 및 실행 ARN 검색
        for pattern in task_patterns:
            matches = re.findall(pattern, response_text, re.IGNORECASE)
            if matches:
                pending_task_detected = True
                task_ids.extend(matches)

        # 대기 중인 작업이 감지되지 않은 경우 현재 응답 반환
        if not pending_task_detected or not task_ids:
            return response

        # 디버그 로그에 비동기 작업 감지 기록
        self.debug_log.append({
            "type": "async_task_detected",
            "task_ids": task_ids,
            "timestamp": time.time()
        })

        # 대기 중인 작업 처리
        for task_id in task_ids:
            if not task_id in self.pending_tasks:
                self.pending_tasks[task_id] = {
                    'id': task_id,
                    'start_time': time.time(),
                    'status': 'pending'
                }

        # 작업 완료 확인 도구 이름 추출
        check_status_tools = []
        status_tool_pattern = r'check[_\s].*?status[_\s]tool["\s:]+([a-zA-Z0-9_-]+)'
        status_matches = re.findall(status_tool_pattern, response_text, re.IGNORECASE)

        if status_matches:
            check_status_tools.extend(status_matches)
        else:
            # 기본 상태 확인 도구 사용
            check_status_tools = ["check-log-analysis-status", "get-analysis-status", "check-task-status"]

        # 모든 대기 중인 작업에 대해 상태 확인
        for task_id, task_info in list(self.pending_tasks.items()):
            if task_info['status'] in ['complete', 'error']:
                continue

            for retry in range(self.max_retries):
                # 작업 상태 확인
                status_checked = False

                for tool_name in check_status_tools:
                    try:
                        # 도구 이름을 MCP 형식으로 변환 (언더스코어를 대시로)
                        mcp_tool_name = tool_name.replace('_', '-')

                        # 작업 ID가 ARN인지 확인
                        if "arn:aws" in task_id:
                            tool_input = {"execution_arn": task_id}
                        else:
                            tool_input = {"task_id": task_id}

                        # 디버그 로그에 상태 확인 도구 요청 기록
                        self.debug_log.append({
                            "type": "status_check_request",
                            "tool_name": mcp_tool_name,
                            "input": tool_input,
                            "timestamp": time.time()
                        })

                        status_result = self.mcp_client.call_tool(mcp_tool_name, tool_input)
                        status_checked = True

                        # 디버그 로그에 상태 확인 결과 기록
                        self.debug_log.append({
                            "type": "status_check_result",
                            "tool_name": mcp_tool_name,
                            "input": tool_input,
                            "output": status_result,
                            "timestamp": time.time()
                        })

                        # 작업 상태 확인
                        if status_result.get('status') == 'complete':
                            self.pending_tasks[task_id]['status'] = 'complete'
                            self.pending_tasks[task_id]['result'] = status_result

                            # 디버그 로그에 작업 완료 기록
                            self.debug_log.append({
                                "type": "async_task_complete",
                                "task_id": task_id,
                                "result": status_result,
                                "timestamp": time.time()
                            })
                            break
                        elif status_result.get('status') == 'error':
                            self.pending_tasks[task_id]['status'] = 'error'
                            self.pending_tasks[task_id]['error'] = status_result.get('message')

                            # 디버그 로그에 작업 오류 기록
                            self.debug_log.append({
                                "type": "async_task_error",
                                "task_id": task_id,
                                "error": status_result.get('message'),
                                "timestamp": time.time()
                            })
                            break

                        # 여전히 진행 중인 경우 대기
                        time.sleep(2)

                    except Exception as e:
                        # 이 도구가 실패하면 다음 도구로 시도
                        self.debug_log.append({
                            "type": "status_check_error",
                            "tool_name": mcp_tool_name,
                            "error": str(e),
                            "timestamp": time.time()
                        })
                        continue

                # 하나 이상의 도구로 상태를 확인했고 작업이 완료된 경우
                if status_checked and self.pending_tasks[task_id]['status'] in ['complete', 'error']:
                    break

                # 타임아웃 확인 (60초)
                if time.time() - task_info['start_time'] > 60:
                    self.pending_tasks[task_id]['status'] = 'timeout'

                    # 디버그 로그에 작업 타임아웃 기록
                    self.debug_log.append({
                        "type": "async_task_timeout",
                        "task_id": task_id,
                        "timestamp": time.time()
                    })
                    break

                # 잠시 대기 후 다시 시도
                time.sleep(2)

        # 모든 작업이 완료되었는지 확인
        all_tasks_completed = all(task['status'] in ['complete', 'error', 'timeout']
                                  for task in self.pending_tasks.values())

        if all_tasks_completed:
            # 작업 결과를 포함하여 최종 분석 요청
            task_results = {task_id: task_info.get('result', {})
                            for task_id, task_info in self.pending_tasks.items()
                            if task_info['status'] == 'complete'}

            task_errors = {task_id: task_info.get('error', "Unknown error")
                           for task_id, task_info in self.pending_tasks.items()
                           if task_info['status'] in ['error', 'timeout']}

            # 최종 분석 요청 메시지 구성
            final_request = "모든 비동기 작업이 완료되었습니다. 다음 결과를 바탕으로 최종 분석을 제공해주세요:\n\n"

            if task_results:
                final_request += "## 완료된 작업 결과\n"
                for task_id, result in task_results.items():
                    final_request += f"작업 ID: {task_id}\n"
                    final_request += f"결과: {json.dumps(result, ensure_ascii=False)[:500]}...\n\n"

            if task_errors:
                final_request += "## 오류가 발생한 작업\n"
                for task_id, error in task_errors.items():
                    final_request += f"작업 ID: {task_id}, 오류: {error}\n"

            # 디버그 로그에 최종 분석 요청 기록
            self.debug_log.append({
                "type": "final_analysis_request",
                "content": final_request,
                "timestamp": time.time()
            })

            # 최종 분석 요청
            self.messages.append({
                "role": "user",
                "content": final_request
            })

            # API 요청 페이로드 구성 - max_tokens 필드 추가
            payload = {
                "model": self.model_id,
                "max_tokens": 8192,
                "messages": self.messages
            }

            anthropic_tools = self._convert_tools_format()
            if anthropic_tools:
                payload["tools"] = anthropic_tools

            # 시스템 프롬프트가 있는 경우 추가
            if self.system_prompt:
                payload["system"] = self._system_prompt(self.system_prompt)

            # API 요청 전송
            response = requests.post(
                self.api_url,
                headers=self.api_headers,
                json=payload
            )

            # 응답 파싱
            if response.status_code == 200:
                response_json = response.json()
                content = response_json.get("content", [])
                usage = response_json.get("usage", {})

                final_message = ""
                for item in content:
                    if item.get("type") == "text":
                        final_message += item.get("text", "")

                # 토큰 사용량 누적
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                self._add_usage(usage)

                # 디버그 로그에 최종 분석 응답 기록 (토큰 사용량 포함)
                self.debug_log.append({
                    "type": "final_analysis_response",
                    "content": final_message,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "timestamp": time.time()
                })

                print(f"최종 분석 토큰 사용량: 입력={input_tokens}, 출력={output_tokens}")

                # 메시지에 최종 응답 추가
                self.messages.append({
                    "role": "assistant",
                    "content": final_message
                })

                # 대기 중인 작업 목록 초기화
                self.pending_tasks = {}

                # 표준 형식으로 응답 변환
                return {
                    "output": {
                        "message": {
                            "content": [{"text": final_message}]
                        }
                    }
                }
            else:
                error_message = f"Anthropic API 오류: {response.status_code} - {response.text}"

                # 디버그 로그에 API 오류 기록
                self.debug_log.append({
                    "type": "api_error",
                    "error": error_message,
                    "timestamp": time.time()
                })

                return {
                    "output": {
                        "message": {
                            "content": [{"text": error_message}]
                        }
                    }
                }
        else:
            # 상태 업데이트 요청
            status_summary = "일부 작업이 아직 진행 중입니다. 현재 상태:\n\n"
            for task_id, task_info in self.pending_tasks.items():
                status_summary += f"작업 ID: {task_id}, 상태: {task_info['status']}\n"

            # 디버그 로그에 상태 업데이트 요청 기록
            self.debug_log.append({
                "type": "status_update_request",
                "content": status_summary,
                "timestamp": time.time()
            })

            self.messages.append({
                "role": "user",
                "content": status_summary + "\n계속해서 작업 상태를 확인해주세요."
            })

            # API 요청 페이로드 구성 - max_tokens 필드 추가
            payload = {
                "model": self.model_id,
                "max_tokens": 8192,
                "messages": self.messages
            }

            anthropic_tools = self._convert_tools_format()
            if anthropic_tools:
                payload["tools"] = anthropic_tools

            # 시스템 프롬프트가 있는 경우 추가
            if self.system_prompt:
                payload["system"] = self._system_prompt(self.system_prompt)

            # API 요청 전송
            response = requests.post(
                self.api_url,
                headers=self.api_headers,
                json=payload
            )

            # 응답 파싱
            if response.status_code == 200:
                response_json = response.json()
                content = response_json.get("content", [])
                usage = response_json.get("usage", {})

                updated_message = ""
                for item in content:
                    if item.get("type") == "text":
                        updated_message += item.get("text", "")

                # 토큰 사용량 누적
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                self._add_usage(usage)

                # 디버그 로그에 상태 업데이트 응답 기록 (토큰 사용량 포함)
                self.debug_log.append({
                    "type": "status_update_response",
                    "content": updated_message,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "timestamp": time.time()
                })

                print(f"상태 업데이트 토큰 사용량: 입력={input_tokens}, 출력={output_tokens}")

                # 메시지에 업데이트된 응답 추가
                self.messages.append({
                    "role": "assistant",
                    "content": updated_message
                })

                # 재귀적으로 작업 완료 확인
                return self._check_task_completion({
                    "content": [updated_message]
                })
            else:
                error_message = f"Anthropic API 오류: {response.status_code} - {response.text}"

                # 디버그 로그에 API 오류 기록
                self.debug_log.append({
                    "type": "api_error",
                    "error": error_message,
                    "timestamp": time.time()
                })

                return {
                    "output": {
                        "message": {
                            "content": [{"text": error_message}]
                        }
                    }
                }

    def invoke_with_tools(self, prompt: str, system_prompt: str = None, previous_messages: list = None) -> Dict[
        str, Any]:
        """
        MCP 도구를 사용하여 Anthropic 모델 호출

        Args:
            prompt: 사용자 프롬프트
            system_prompt: 시스템 프롬프트 (선택 사항)
            previous_messages: 이전 대화 기록 (messages 배열 형식, 선택 사항)

        Returns:
            Anthropic 모델 응답 (표준 형식으로 변환됨)
        """
        # 세션 및 도구가 초기화되지 않은 경우
        if not self.tools:
            self.initialize()

        # 시스템 프롬프트 저장 (나중에 재사용)
        if system_prompt:
            self.system_prompt = system_prompt

        # 메시지 배열 초기화 - 이전 대화 기록 포함.
        # 질문과 이전 대화도 Claude로 나가므로 가린다 (사용자가 키나 계정 ID를 붙여 넣었을 수 있다)
        if previous_messages:
            self.messages = [self._redact_message(message) for message in previous_messages]
        else:
            self.messages = []
        prompt = self.redactor.text(prompt)

        # 디버그 로그 초기화
        self.debug_log = []

        # 토큰 사용량 초기화
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_write_tokens = 0
        self.model_calls = []
        self.tool_search_count = 0
        # 체류 신호도 질문마다 새로 센다 (이전 질문에서 읽은 것은 대화 기록에 도구 결과로 남지 않는다)
        self._tool_calls = 0
        self._seen_suspicious = []

        # 디버그 로그에 사용자 입력 기록
        self.debug_log.append({
            "type": "user_input",
            "content": prompt,
            "previous_messages_count": len(self.messages) if previous_messages else 0,
            "timestamp": time.time()
        })

        # 시스템 프롬프트가 있는 경우 디버그 로그에 기록
        if system_prompt:
            self.debug_log.append({
                "type": "system_prompt",
                "content": system_prompt,
                "timestamp": time.time()
            })

        # 현재 사용자 입력 추가 (중복 방지)
        if not (self.messages and self.messages[-1].get("role") == "user" and self.messages[-1].get(
                "content") == prompt):
            self.messages.append({
                "role": "user",
                "content": prompt
            })

        # Anthropic 도구 형식으로 변환
        anthropic_tools = self._convert_tools_format()

        # 디버그 로그에 사용 가능한 도구 기록
        self.debug_log.append({
            "type": "available_tools",
            "tools": [tool["name"] for tool in anthropic_tools],
            "timestamp": time.time()
        })

        # 모델 호출 루프 시작 (도구 사용이 완료될 때까지)
        final_response = None
        iteration = 0

        # 모든 도구 결과 응답을 저장할 배열
        all_responses = []

        print(f"대화 시작 - 메시지 수: {len(self.messages)}")
        while iteration < self.max_iterations:
            iteration += 1
            print(f"반복 {iteration}/{self.max_iterations}")

            # 디버그 로그에 반복 정보 기록
            self.debug_log.append({
                "type": "iteration_start",
                "iteration": iteration,
                "timestamp": time.time()
            })

            # API 요청 페이로드 구성 - max_tokens 필드 추가.
            # 대화에도 캐시 표시를 둔다: 다음 반복에서 앞 대화(도구 결과, 검색으로 찾은 도구 정의)를 캐시로 읽는다
            payload = {
                "model": self.model_id,
                "max_tokens": MAX_TOKENS,
                "messages": tool_search.with_cache_breakpoint(self.messages)
            }
            # 사고 과정: 화면에 보여 주려면 사고 요약을 받아야 한다 (display: summarized)
            if self.thinking:
                payload["thinking"] = self.thinking
            # 마지막 반복에서는 도구 호출 중지
            if iteration == self.max_iterations - 1:
                payload["tool_choice"] = {"type": "none"}
            else:
                payload["tool_choice"] = {"type": "auto"}

            # 도구가 있는 경우 추가 (도구 검색이 도중에 꺼졌을 수 있어 반복마다 다시 만든다)
            anthropic_tools = self._convert_tools_format()
            if anthropic_tools:
                payload["tools"] = anthropic_tools

            # 시스템 프롬프트가 있는 경우 추가
            if system_prompt:
                payload["system"] = self._system_prompt(system_prompt)

            # 요청 요약만 남긴다 (예전에는 도구 정의까지 든 페이로드 전체를 들여쓰기 JSON으로 만든 뒤 500자만 찍었다)
            print(f"API 요청: 모델={self.model_id}, 메시지 {len(self.messages)}개, 도구 {len(payload.get('tools', []))}개")

            # API 요청 전송 (응답을 기다리는 동안 화면에는 '생각하는 중')
            self._report("thinking_started")
            with self._time("model"):
                response = self._post(payload, system_prompt)

            # 디버깅을 위한 응답 로깅
            print(f"API 응답 상태 코드: {response.status_code}")
            print(f"API 응답 내용: {response.text[:500]}...")

            # 응답 파싱
            if response.status_code != 200:
                error_message = f"Anthropic API 오류: {response.status_code} - {response.text}"

                # 디버그 로그에 API 오류 기록
                self.debug_log.append({
                    "type": "api_error",
                    "error": error_message,
                    "timestamp": time.time()
                })

                return {
                    "output": {
                        "message": {
                            "content": [{"text": error_message}]
                        }
                    }
                }

            response_json = response.json()
            content = response_json.get("content", [])
            usage = response_json.get("usage", {})

            message_content = ""
            tool_uses = []

            # 토큰 사용량 추출 및 누적
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            self._add_usage(usage)

            print(f"이번 반복 토큰 사용량: 입력={input_tokens}, 출력={output_tokens}, "
                  f"캐시 읽기={usage.get('cache_read_input_tokens') or 0}, "
                  f"캐시 쓰기={usage.get('cache_creation_input_tokens') or 0}")
            print(f"누적 토큰 사용량: 입력={self.total_input_tokens}, 출력={self.total_output_tokens}")

            # 응답 콘텐츠에서 텍스트와 도구 사용 분리. 사고 요약은 진행 상황으로 보낸다
            for item in content:
                if item.get("type") == "text":
                    message_content += item.get("text", "")
                elif item.get("type") == "tool_use":
                    tool_uses.append(item)
                elif item.get("type") == "thinking":
                    self._report("thought", item.get("thinking", ""))
            # 도구 검색(서버에서 돈다)은 실행할 것이 없고, 무엇을 찾았는지만 화면에 알린다
            for search in tool_search.searches(content):
                self.tool_search_count += 1
                self._report("tool_search", search["query"], search["found"], search.get("error"))

            # 응답 저장
            print(f"응답 텍스트: {message_content[:100]}...")
            print(f"도구 사용 요청 수: {len(tool_uses)}")

            # 디버그 로그에 모델 응답 기록 (토큰 사용량 포함)
            if message_content:
                self.debug_log.append({
                    "type": "model_reasoning",
                    "content": message_content,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "timestamp": time.time()
                })

            # 텍스트 응답이 있으면 저장
            if message_content:
                all_responses.append(message_content)

            # 응답을 받은 그대로(사고·글·도구 호출 블록 모두) 한 assistant 메시지로 이어 붙인다.
            # 사고 과정을 켜고 도구를 쓰면, 다음 요청에 사고 블록을 고치지 않고 그대로 돌려보내야 한다
            # (예전처럼 글과 도구 호출을 따로 두 메시지로 나누면 사고 블록이 빠진다)
            # 도구 검색 블록(server_tool_use, tool_search_tool_result)도 그대로 돌려보내야 찾은 도구가 이어진다
            if content:
                self.messages.append({
                    "role": "assistant",
                    "content": content
                })

            # 서버 도구(도구 검색)가 길어져 응답이 중간에 멈췄다: 받은 그대로 다시 보내면 이어서 한다
            if response_json.get("stop_reason") == "pause_turn" and not tool_uses:
                continue

            # 도구 사용 요청이 있는 경우
            if tool_uses:
                # 디버그 로그에 도구 사용 요청 기록
                self.debug_log.append({
                    "type": "tool_use_requests",
                    "count": len(tool_uses),
                    "timestamp": time.time()
                })

                # 각 도구에 대해 MCP 도구 호출
                tool_results = []
                for tool_use in tool_uses:
                    try:
                        # 도구 정보 추출
                        tool_use_id = tool_use.get("id")
                        tool_name = tool_use.get("name")
                        tool_input = tool_use.get("input", {})

                        print(f"도구 호출: {tool_name}, 입력: {json.dumps(tool_input, ensure_ascii=False)}")
                        # 조회 도구에는 가명을 원래 값으로 되돌려 넘긴다 (ARN으로 다시 조회하는 흐름이 깨지지 않게).
                        # 결과물 도구(차트·다이어그램)는 AWS를 부르지 않고 모델이 쓴 값을 그림에 옮길 뿐이라 가명 그대로
                        # 넘긴다: 그림은 링크로 공유될 수 있고, 답변과 같이 가명만 보이는 것이 맞다 (docs/threat-model.md R1)
                        risk = self._tool_risk(tool_name)
                        restore = risk != "artifact"
                        self._tool_calls += 1
                        call_no = self._tool_calls
                        # 감사 로그의 층 (audit.py 모듈 설명): 등록부에 없는 도구는 경계, 변경 도구는 유출, 나머지는 유입
                        locus = ("interface" if not is_registered(self._tool_definition(tool_name))
                                 else "egress" if risk == "write" else "ingress")
                        self._report("tool_started", tool_use_id, tool_name, tool_input, restore, locus)

                        # 디버그 로그에 도구 사용 요청 기록
                        self.debug_log.append({
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "input": tool_input,
                            "timestamp": time.time()
                        })

                        # MCP 도구 호출. AWS를 바꾸는 도구는 실행하지 않고 승인 요청을 만든다 (approvals.py)
                        is_write = risk == "write"
                        if is_write:
                            result = self._request_approval(tool_name, tool_input, self._tainted_by(call_no))
                        else:
                            with self._time(f"tool:{tool_name}"):
                                result = self.mcp_client.call_tool(
                                    tool_name,
                                    self.redactor.restore(tool_input) if restore else tool_input
                                )
                            # 결과물은 주소·그릴 내용을 떼어 두고 모델에는 참조(artifact://…)만 준다
                            if risk == "artifact" and self.artifacts is not None:
                                result = self.artifacts.take(tool_name, result)

                        print(f"도구 결과: {self.redactor.text(json.dumps(result, ensure_ascii=False))[:200]}...")
                        # MCP 도구는 실패를 예외 대신 결과의 isError로 알리기도 한다
                        failed = isinstance(result, dict) and result.get("isError") is True
                        # 도구 결과(제3자가 쓴 글)에 지시문처럼 보이는 문구가 있는지 (injection.py).
                        # 승인 요청 결과는 이 서비스가 만든 글이라 보지 않는다
                        suspicious = [] if is_write else injection.scan(json.dumps(result, ensure_ascii=False))
                        self._report("tool_finished", tool_use_id, not failed,
                                     self._tool_error_text(result) if failed else None,
                                     len(json.dumps(result, ensure_ascii=False, default=str)), suspicious)

                        # 디버그 로그에 도구 결과 기록
                        self.debug_log.append({
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "input": tool_input,
                            "output": result,
                            "timestamp": time.time()
                        })

                        # 도구 결과를 저장
                        tool_results.append({
                            "tool_id": tool_use_id,
                            "name": tool_name,
                            "result": result,
                            "suspicious": suspicious,
                            "call_no": call_no,
                        })
                    except Exception as e:
                        # 오류 처리
                        print(f"도구 호출 오류: {str(e)}")
                        self._report("tool_finished", tool_use_id, False, str(e))

                        # 디버그 로그에 도구 오류 기록
                        self.debug_log.append({
                            "type": "tool_error",
                            "tool_name": tool_name,
                            "input": tool_input,
                            "error": str(e),
                            "timestamp": time.time()
                        })

                        tool_results.append({
                            "tool_id": tool_use_id,
                            "name": tool_name,
                            "error": str(e)
                        })

                # Append user tool_result message in the required format
                tool_results_list = []
                for res in tool_results:
                    # determine content value and ensure it's a string
                    if "error" in res:
                        content_value = str(res.get("error"))
                    else:
                        result = res.get("result")
                        # Convert result to string if it's not already
                        if isinstance(result, dict) or isinstance(result, list):
                            content_value = json.dumps(result, ensure_ascii=False)
                        else:
                            content_value = str(result)

                    tool_results_list.append({
                        "type": "tool_result",
                        "tool_use_id": res["tool_id"],
                        # 도구 결과가 계정 밖(Claude API)으로 나가는 곳이다. 여기서 반드시 가리고,
                        # 데이터 영역으로 감싸 지시로 읽히지 않게 한다 (의심 문구가 있으면 경고를 붙인다)
                        "content": injection.wrap(res["name"], self.redactor.text(content_value),
                                                  res.get("suspicious") or [])
                    })
                # Save as a single user message with a list of tool_result objects
                self.messages.append({
                    "role": "user",
                    "content": tool_results_list
                })
                # 이제 모델이 이 결과들을 읽는다: 의심 결과를 이후 변경 요청의 체류 신호로 남긴다 (최근 것 몇 개만)
                self._seen_suspicious = (self._seen_suspicious + [
                    {"toolUseId": res["tool_id"], "tool": res["name"], "kinds": res["suspicious"],
                     "callNo": res["call_no"]}
                    for res in tool_results if res.get("suspicious")])[-MAX_TAINTED:]
                continue  # proceed to next iteration

            # 도구 호출이 없으면 마지막 assistant 응답을 즉시 반환
            return {
                "output": {
                    "message": {
                        "content": [{"text": message_content}]
                    }
                }
            }

        # 루프를 빠져나왔을 때 마지막 어시스턴트 응답 반환
        if self.messages and self.messages[-1].get("role") == "assistant":
            last = self._text_of(self.messages[-1]["content"])
        else:
            last = ""
        return {
            "output": {
                "message": {
                    "content": [{"text": last}]
                }
            }
        }

    @staticmethod
    def _tool_error_text(result) -> str:
        """isError 결과의 첫 글자 블록 (화면에 보일 실패 이유)."""
        for block in (result or {}).get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        return "도구가 오류를 돌려주었습니다"

    def _extract_text_from_response(self, response):
        """
        응답에서 텍스트 추출

        Args:
            response: Anthropic 응답 객체

        Returns:
            추출된 텍스트
        """
        if "content" in response:
            if isinstance(response["content"], list):
                text = ""
                for item in response["content"]:
                    if isinstance(item, str):
                        text += item
                    elif isinstance(item, dict) and "text" in item:
                        text += item["text"]
                return text
            elif isinstance(response["content"], str):
                return response["content"]

        output_message = response.get('output', {}).get('message', {})
        if output_message:
            content = output_message.get('content', [])
            if content:
                text = ""
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        text += item["text"]
                return text

        return str(response)

    def process_user_input(self, user_input: str, system_prompt: str = None) -> str:
        """
        사용자 입력 처리 및 최종 텍스트 응답 반환

        Args:
            user_input: 사용자 질문/입력
            system_prompt: 시스템 프롬프트 (선택 사항)

        Returns:
            최종 텍스트 응답
        """
        # 시스템 프롬프트 저장
        if system_prompt:
            self.system_prompt = system_prompt

        # 메시지 배열 초기화
        self.messages = []

        # 디버그 로그 초기화
        self.debug_log = []

        # 디버그 로그에 처리 시작 기록
        self.debug_log.append({
            "type": "process_start",
            "user_input": user_input,
            "timestamp": time.time()
        })

        # LLM이 모든 필요한 도구를 사용하여 완전한 응답 생성
        response = self.invoke_with_tools(user_input, system_prompt)

        # 응답에서 텍스트 추출
        output_message = response.get('output', {}).get('message', {})
        content = output_message.get('content', [])

        # 모든 텍스트 콘텐츠 결합
        final_text = ""
        for item in content:
            if isinstance(item, dict) and 'text' in item:
                final_text += item['text']
            elif isinstance(item, str):
                final_text += item

        # 최종 텍스트 로깅 추가
        print(f"최종 응답 반환: {final_text[:200]}...")
        print(f"총 토큰 사용량: 입력={self.total_input_tokens}, 출력={self.total_output_tokens}")

        # 디버그 로그에 최종 응답 기록 (총 토큰 사용량 포함)
        self.debug_log.append({
            "type": "final_response",
            "content": final_text,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            **self._usage_extra(),
            "timestamp": time.time()
        })

        # 메시지 배열에서 마지막 assistant 응답 찾기
        if not final_text:
            for message in reversed(self.messages):
                if message.get("role") == "assistant":
                    final_text = self._text_of(message.get("content", ""))
                    print(f"마지막 assistant 메시지 사용: {final_text[:200]}...")
                    break

        return final_text

    def process_user_input_with_history(self, user_input: str, system_prompt: str = None,
                                        previous_messages: list = None) -> str:
        """
        이전 대화 기록을 포함하여 사용자 입력 처리

        Args:
            user_input: 현재 사용자 입력
            system_prompt: 시스템 프롬프트 (선택 사항)
            previous_messages: 이전 대화 기록 (messages 배열 형식)

        Returns:
            최종 텍스트 응답
        """
        # 시스템 프롬프트 저장
        if system_prompt:
            self.system_prompt = system_prompt

        # 디버그 로그 초기화
        self.debug_log = []

        # 디버그 로그에 처리 시작 기록
        self.debug_log.append({
            "type": "process_start_with_history",
            "user_input": user_input,
            "previous_messages_count": len(previous_messages) if previous_messages else 0,
            "timestamp": time.time()
        })

        # LLM이 모든 필요한 도구를 사용하여 완전한 응답 생성 (이전 메시지 포함)
        response = self.invoke_with_tools(user_input, system_prompt, previous_messages)

        # 응답에서 텍스트 추출
        output_message = response.get('output', {}).get('message', {})
        content = output_message.get('content', [])

        # 모든 텍스트 콘텐츠 결합
        final_text = ""
        for item in content:
            if isinstance(item, dict) and 'text' in item:
                final_text += item['text']
            elif isinstance(item, str):
                final_text += item

        # 최종 텍스트 로깅 추가
        print(f"최종 응답 반환 (히스토리 포함): {final_text[:200]}...")
        print(f"총 토큰 사용량: 입력={self.total_input_tokens}, 출력={self.total_output_tokens}")

        # 디버그 로그에 최종 응답 기록 (총 토큰 사용량 포함)
        self.debug_log.append({
            "type": "final_response_with_history",
            "content": final_text,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            **self._usage_extra(),
            "timestamp": time.time()
        })

        # 메시지 배열에서 마지막 assistant 응답 찾기
        if not final_text:
            for message in reversed(self.messages):
                if message.get("role") == "assistant":
                    final_text = self._text_of(message.get("content", ""))
                    print(f"마지막 assistant 메시지 사용: {final_text[:200]}...")
                    break

        return final_text

    def _add_usage(self, usage: Dict[str, Any]) -> None:
        """모델 호출 한 번의 토큰을 누적하고, 호출별 목록에도 남긴다. 캐시에서 읽은·캐시에 쓴 입력은 input_tokens와 따로 온다."""
        call = {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "cacheWrite": int(usage.get("cache_creation_input_tokens") or 0),
            "cacheRead": int(usage.get("cache_read_input_tokens") or 0),
        }
        self.total_input_tokens += call["input"]
        self.total_output_tokens += call["output"]
        self.total_cache_write_tokens += call["cacheWrite"]
        self.total_cache_read_tokens += call["cacheRead"]
        self.model_calls.append(call)

    def usage_summary(self) -> Dict[str, Any]:
        """이번 질문에 쓴 토큰: 네 종류의 합계와 모델 호출별 목록 (감사 로그, audit.AuditLog.request_finished).
        실패한 질문도 그때까지 쓴 만큼 남긴다."""
        return {
            "tokens": {
                "input": self.total_input_tokens,
                "output": self.total_output_tokens,
                "cacheWrite": self.total_cache_write_tokens,
                "cacheRead": self.total_cache_read_tokens,
            },
            "calls": [dict(call) for call in self.model_calls],
        }

    def _usage_extra(self) -> Dict[str, int]:
        """토큰 사용량 중 입력·출력 밖의 것: 캐시에서 읽은·캐시에 쓴 입력과 도구 검색 횟수 (도구 검색 전후 비교용)."""
        return {
            "cache_read_input_tokens": self.total_cache_read_tokens,
            "cache_creation_input_tokens": self.total_cache_write_tokens,
            "tool_searches": self.tool_search_count,
        }

    def get_debug_log(self) -> List[Dict[str, Any]]:
        """
        디버그 로그 반환

        Returns:
            디버그 로그 배열
        """
        return self.debug_log

    def close(self) -> bool:
        """
        MCP 세션 종료

        Returns:
            성공 여부
        """
        return self.mcp_client.close()