import json
import urllib.parse
import requests
import boto3
import os
import re
import threading
import time
from datetime import datetime, timezone
from common.config import get_config
from common.utils import invoke_bedrock_nova, cors_headers, cors_response
from slack_sdk import WebClient
from llm_progress import REQUEST_ID, ProgressReporter, read_progress
from redaction import Redactor
from audit import AuditLog, AuditQueryError, CloudWatchSink, groups_of, query_answer, query_audit
from audit_trace import query_trace
from approvals import (APPROVED, DENIED, FINISHED, ApprovalError, ApprovalRequester, ApprovalStore, approval_mode,
                       can_view, check_decision, execute_approved, follow_up_prompt, public_view)
from mcp_client import MCPClient
from common.dashboard_store import DashboardStore
from dashboard_view import app_changes_from_audit, build_view, pending_approvals
import metrics
from system_prompt import build_system_blocks, build_system_prompt
from timing import Stopwatch
from artifacts import Artifacts

# Lambda 환경에서 효율적인 재사용을 위한 클라이언트 캐싱
client = None

# 클라이언트 캐시 저장을 위한 전역 변수
client_cache = {}

# DynamoDB 설정 (안전하게 초기화)
try:
    CONFIG = get_config()
    CHAT_HISTORY_TABLE = CONFIG.get('db', {}).get('chat_history_table')

    if CHAT_HISTORY_TABLE:
        dynamodb = boto3.resource('dynamodb')
        chat_table = dynamodb.Table(CHAT_HISTORY_TABLE)
        print(f"DynamoDB 테이블 연결 성공: {CHAT_HISTORY_TABLE}")
    else:
        chat_table = None
        print("DynamoDB 테이블 설정이 없습니다. 캐싱 기능을 비활성화합니다.")
except Exception as e:
    print(f"DynamoDB 초기화 오류: {str(e)}")
    chat_table = None

# 답변을 만드는 동안의 진행 상황 (llm_progress.py). 테이블이 없으면 진행 상황 없이 답변만 만든다
LLM_PROGRESS_TABLE = os.environ.get("LLM_PROGRESS_TABLE")
progress_table = boto3.resource("dynamodb").Table(LLM_PROGRESS_TABLE) if LLM_PROGRESS_TABLE else None
# 이 Lambda가 속한 계정 ID (CloudFormation이 넣는다). 도구 결과 어디에 있든 가린다 (redaction.py)
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")
# 감사 로그 (audit.py). 테이블·로그 그룹이 없으면(로컬 등) 그쪽에는 기록하지 않는다
AUDIT_TABLE = os.environ.get("AUDIT_TABLE")
audit_table = boto3.resource("dynamodb").Table(AUDIT_TABLE) if AUDIT_TABLE else None
audit_sink = CloudWatchSink(os.environ.get("AUDIT_LOG_GROUP"))
# 변경 작업 승인 (approvals.py). 테이블이 없으면 변경 도구는 쓸 수 없다 (승인 요청을 만들지 못해 거절된다)
PENDING_ACTIONS_TABLE = os.environ.get("PENDING_ACTIONS_TABLE")
approval_store = ApprovalStore(boto3.resource("dynamodb").Table(PENDING_ACTIONS_TABLE)) if PENDING_ACTIONS_TABLE else None
# 홈 대시보드 (dashboard_view.py): 수집 Lambda(services/dashboard)가 구역마다 모아 둔 값. 없으면 GET /dashboard는 503
DASHBOARD_TABLE = os.environ.get("DASHBOARD_TABLE")
dashboard_store = DashboardStore(boto3.resource("dynamodb").Table(DASHBOARD_TABLE)) if DASHBOARD_TABLE else None


# ---------------------------------------------------------------- 모델: 요청할 때 최신 Sonnet
# 모든 요청(웹·Slack)이 같은 모델을 쓰고, 사용자가 모델을 고르는 기능은 없다.
# 모델 ID를 코드에 고정하지 않고, 요청할 때 Anthropic이 지금 제공하는 모델 목록(Models API)에서 가장 최근에 나온
# Sonnet을 고른다. 새 Sonnet이 나오면 배포 없이 저절로 넘어가고, 고정한 모델이 퇴역해 모든 요청이 실패하는 일이 없다
# (예전 기본값 claude-3-5-sonnet-20241022는 2025-10-28, claude-3-7-sonnet-20250219는 2026-02-19에 퇴역했다).
MODEL_FAMILY = "sonnet"
MODELS_CACHE_SECONDS = 3600  # 모델 목록은 자주 바뀌지 않는다. Lambda 컨테이너마다 한 시간 재사용
COLD_MODELS_TIMEOUT = 5  # 목록이 아직 없을 때(새 컨테이너) 기다리는 시간. 넘으면 FALLBACK_MODEL로 답한다
# 대화 기록은 최근 것만 보낸다 (오래된 대화까지 매번 보내면 입력이 커져 첫 응답이 늦어지고 요금이 는다)
MAX_HISTORY_MESSAGES = 20
# 인사·감사처럼 도구가 필요 없는 짧은 말. 도구 목록·MCP·사고 없이 바로 답한다 (is_small_talk).
# 말 전체가 이것뿐일 때만 (뒤에 질문이 붙으면 평소처럼): "안녕", "고마워요!", "hi" / 아님: "안녕, 로그 봐줘"
SMALL_TALK = re.compile(
    r"^(안녕(하세요|하십니까)?|하이|헬로|반가워(요)?|반갑습니다|고마워(요)?|감사(해요|합니다)?|땡큐|"
    r"수고(하셨습니다|했어요?|해요|하세요)?|잘\s?(가|있어)|좋은\s?(아침|하루)(이에요|입니다)?|"
    r"hi|hello|hey|thanks?|thank\s+you|good\s+(morning|night))"
    r"[\s!.~?^ㅎㅋㅠㅜ:)]*$", re.IGNORECASE)
SMALL_TALK_MAX_CHARS = 20


def is_small_talk(text: str) -> bool:
    """도구가 필요 없는 인사·감사인가 (짧고, 말 전체가 SMALL_TALK일 때만)."""
    text = (text or "").strip()
    return 0 < len(text) <= SMALL_TALK_MAX_CHARS and bool(SMALL_TALK.match(text))
# 목록을 한 번도 받지 못했을 때만 쓰는 모델 (Models API 장애 등). 평소에는 쓰이지 않는다
FALLBACK_MODEL = {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "thinking": "adaptive"}
_models_cache = {"at": 0.0, "models": []}


def get_anthropic_models(timeout=10):
    """
    Anthropic Models API(GET /v1/models)에서 지금 사용할 수 있는 모델 목록을 끝까지 조회한다.

    Returns:
        list: [{"id", "display_name", "created_at", "thinking"}] (실패하면 빈 목록)
    """
    try:
        CONFIG = get_config()
        anthropic_api_key = os.environ.get('ANTHROPIC_API_KEY') or CONFIG.get('anthropic', {}).get('api_key')

        if not anthropic_api_key:
            print("Anthropic API 키가 설정되지 않았습니다.")
            return []

        headers = {
            "x-api-key": anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        # 목록 순서(최신부터)에 기대지 않도록 마지막 페이지까지 받는다
        model_list = []
        params = {"limit": 1000}
        while True:
            response = requests.get("https://api.anthropic.com/v1/models", headers=headers, params=params,
                                    timeout=timeout)
            if response.status_code != 200:
                print(f"Anthropic Models API 오류: {response.status_code} - {response.text}")
                return []
            page = response.json()
            for model in page.get('data', []):
                model_list.append({
                    "id": model.get("id", ""),
                    "display_name": model.get("display_name", model.get("id", "")),
                    "created_at": model.get("created_at", ""),   # 출시일 (RFC 3339). 최신 Sonnet을 고르는 데 쓴다
                    "thinking": thinking_mode(model),             # 지원하는 사고 방식 (thinking_config에 쓴다)
                })
            if not page.get('has_more') or not page.get('last_id'):
                break
            params = {"limit": 1000, "after_id": page['last_id']}

        print(f"Anthropic 모델 {len(model_list)}개 조회 완료")
        return model_list

    except Exception as e:
        print(f"Anthropic 모델 조회 중 오류: {str(e)}")
        return []


def thinking_mode(model):
    """
    Models API가 알려 주는 모델의 사고(extended thinking) 지원 방식: "adaptive" | "enabled" | None.

    모델마다 받는 설정이 다르다. 최신 모델(Sonnet 5, Opus 4.6 이후 등)은 adaptive만 받고 budget_tokens를
    보내면 400이다. 예전 모델(Haiku 4.5 등)은 enabled + budget_tokens만 받는다. 모델 ID로 나누면 새 모델이
    나올 때마다 고쳐야 하므로, Models API의 capabilities를 그대로 따른다.
    """
    types = (((model.get("capabilities") or {}).get("thinking") or {}).get("types")) or {}
    if (types.get("adaptive") or {}).get("supported"):
        return "adaptive"
    if (types.get("enabled") or {}).get("supported"):
        return "enabled"
    return None


THINKING_BUDGET_TOKENS = 4000  # enabled 방식(예전 모델)의 사고 예산. MAX_TOKENS(16000)보다 작아야 한다


def thinking_config(model):
    """
    요청에 넣을 사고 설정 (고른 모델이 지원하는 방식을 따른다). 화면에 사고 과정을 보여 주려면 사고 요약을 받아야 한다.
    - adaptive: 사고 요약은 요청해야 온다(display: summarized). 없으면 최신 모델은 빈 사고 블록을 준다
    - enabled: 예전 모델은 사고 요약을 기본으로 준다
    - 모델이 사고를 지원하지 않으면 넣지 않는다
    """
    mode = (model or {}).get("thinking")
    if mode == "adaptive":
        return {"type": "adaptive", "display": "summarized"}
    if mode == "enabled":
        return {"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS}
    return None


def _refresh_models(timeout=10):
    models = get_anthropic_models(timeout=timeout)
    if models:
        _models_cache.update(at=time.time(), models=models)


_models_refresh = None  # 뒤에서 목록을 새로 받는 스레드 (한 번에 하나)


def available_models():
    """모델 목록 (캐시). 질문이 목록 조회를 기다리지 않게 한다.
    - 한 시간 안: 캐시를 그대로 쓴다
    - 오래됨: 캐시를 바로 쓰고, 뒤에서(스레드) 새로 받는다. 다음 질문부터 새 목록이 쓰인다
      (Lambda는 응답 뒤 멈추므로 이번 요청 안에 못 끝나면 다음 호출 때 이어서 끝난다)
    - 없음(새 컨테이너): 받아야 고를 수 있으니 기다리되, COLD_MODELS_TIMEOUT을 넘기면 FALLBACK_MODEL로 답한다
    새로 받지 못하면 마지막으로 받은 목록을 계속 쓴다 (일시적인 오류로 채팅이 멈추지 않게)."""
    global _models_refresh
    now = time.time()
    if _models_cache["models"]:
        if now - _models_cache["at"] >= MODELS_CACHE_SECONDS and not (_models_refresh and _models_refresh.is_alive()):
            _models_refresh = threading.Thread(target=_refresh_models, daemon=True)
            _models_refresh.start()
        return _models_cache["models"]
    _refresh_models(timeout=COLD_MODELS_TIMEOUT)
    return _models_cache["models"]


def latest_sonnet(models):
    """
    Sonnet 계열 중 가장 최근에 나온 모델 (없으면 None).

    출시일(created_at)로 비교한다. 모델 ID 형식이 세대마다 달라서(claude-3-5-sonnet-20241022,
    claude-sonnet-4-5-20250929, claude-sonnet-5 …) ID를 잘라 버전을 비교하면 틀리기 쉽다.
    """
    candidates = [m for m in models if MODEL_FAMILY in m.get("id", "")]
    if not candidates:
        return None
    # 출시일이 없는 항목은 고르지 않도록 가장 이른 값("")으로 둔다. 같은 날이면 ID 순으로 정해 결과가 매번 같게 한다
    return max(candidates, key=lambda m: (m.get("created_at") or "", m["id"]))


def current_model():
    """이번 요청에 쓸 모델: 지금 목록의 최신 Sonnet. 목록을 한 번도 받지 못했으면 FALLBACK_MODEL."""
    model = latest_sonnet(available_models())
    if model is None:
        print(f"모델 목록이 없어 {FALLBACK_MODEL['id']}을(를) 씁니다")
        return FALLBACK_MODEL
    return model


def tool_step(entry):
    """
    디버그 로그 항목 하나를 화면에 보여 줄 도구 호출 한 건으로 바꾼다. 해당하지 않으면 None.

    AnthropicMCPClient는 도구 한 번 호출에 항목을 두 개 남긴다: 호출 직전의 tool_result(output 없음)와
    끝난 뒤의 tool_result(output 있음) 또는 tool_error. 끝난 쪽만 쓰면 호출 한 번이 한 건이 된다
    (예전에는 둘 다 넣어서 같은 도구가 두 번씩 보였다).

    Returns:
        dict | None: {"tool_name", "input", "status": "ok"|"error", "error"?}
    """
    entry_type = entry.get("type")
    if entry_type == "tool_error":
        return {"tool_name": entry.get("tool_name"), "input": entry.get("input"),
                "status": "error", "error": entry.get("error")}
    if entry_type == "tool_result" and "output" in entry:
        output = entry.get("output")
        # MCP 도구는 실패를 예외 대신 결과의 isError로 알리기도 한다
        failed = isinstance(output, dict) and output.get("isError") is True
        return {"tool_name": entry.get("tool_name"), "input": entry.get("input"),
                "status": "error" if failed else "ok"}
    return None


def recent_history(messages: list) -> list:
    """최근 MAX_HISTORY_MESSAGES개만 남긴다. 첫 메시지는 사용자 질문이어야 한다 (Messages API 규칙)."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    recent = messages[-MAX_HISTORY_MESSAGES:]
    while recent and recent[0]["role"] != "user":
        recent = recent[1:]
    return recent


def get_session_messages_as_array(session_id: str, user_id: str) -> list:
    """
    DynamoDB에서 세션의 메시지 히스토리를 messages 배열 형식으로 가져옴

    Args:
        session_id: 채팅 세션 ID
        user_id: 요청자(Cognito sub). 세션 소유자와 다르면 히스토리를 사용하지 않음

    Returns:
        messages 배열 형식의 대화 기록
    """
    try:
        if not chat_table:
            print("DynamoDB 테이블이 초기화되지 않았습니다.")
            return []

        response = chat_table.get_item(
            Key={'sessionId': session_id}
        )

        session = response.get('Item')
        if not session or not user_id or session.get('userId') != user_id:
            print(f"세션을 찾을 수 없거나 요청자 소유가 아닙니다: {session_id}")
            return []

        messages = session.get('messages', [])

        # 메시지를 시간순으로 정렬
        messages.sort(key=lambda x: x.get('timestamp', ''))

        # Claude/Anthropic API 형식으로 변환
        formatted_messages = []
        for msg in messages:
            sender = msg.get('sender', 'user')
            text = msg.get('text', '')

            if sender == 'user':
                formatted_messages.append({
                    "role": "user",
                    "content": text
                })
            elif sender == 'assistant':
                formatted_messages.append({
                    "role": "assistant",
                    "content": text
                })

        formatted_messages = recent_history(formatted_messages)
        print(f"세션 {session_id}에서 {len(formatted_messages)}개 메시지를 배열 형식으로 로드됨")
        return formatted_messages

    except Exception as e:
        print(f"세션 메시지 조회 실패: {str(e)}")
        return []


def get_client(timer=None):
    """
    MCP 클라이언트 인스턴스를 가져오거나 생성. 요청할 때의 최신 Sonnet을 쓰고, 모델 ID별로 캐시한다
    (새 Sonnet이 나오면 새 클라이언트가 만들어진다). timer(timing.Stopwatch)가 있으면 모델 고르기·MCP 준비 시간을 잰다
    """
    global client_cache
    timer = timer or Stopwatch.off()

    # 사용할 클라이언트 유형 결정 (Bedrock 또는 Anthropic)
    use_anthropic = os.environ.get('USE_ANTHROPIC_API', 'true').lower() == 'true'
    # Bedrock은 모델 ID 형식이 달라(anthropic.claude-…) Anthropic 목록으로 고르지 않고 BedrockMCPClient의 기본값을 쓴다
    with timer.step("models"):
        model = current_model() if use_anthropic else None
    model_id = model["id"] if model else None

    # 캐시에 해당 모델 ID의 클라이언트가 없으면 생성
    if model_id not in client_cache:
        # 환경 변수에서 구성 가져오기
        CONFIG = get_config()
        mcp_url = os.environ.get('MCP_URL') or CONFIG.get('mcp', {}).get('function_url')

        if use_anthropic:
            # Anthropic API 설정
            anthropic_api_key = os.environ.get('ANTHROPIC_API_KEY') or CONFIG.get('anthropic', {}).get('api_key')

            # Anthropic 클라이언트 초기화
            from mcp_anthropic_client import AnthropicMCPClient
            client_cache[model_id] = AnthropicMCPClient(
                mcp_url=mcp_url,
                api_key=anthropic_api_key,
                model_id=model_id,
                thinking=thinking_config(model),
            )
        else:
            # Bedrock 설정
            mcp_token = os.environ.get('MCP_TOKEN', '')
            region = os.environ.get('AWS_REGION', 'ap-northeast-2')

            # Bedrock 클라이언트 초기화
            from mcp_bedrock_client import BedrockMCPClient
            client_cache[model_id] = BedrockMCPClient(
                mcp_url=mcp_url,
                region=region,
                auth_token=mcp_token,
                model_id=model_id
            )

        # 세션 초기화 및 도구 로드. Anthropic 클라이언트는 도구가 필요한 질문에서 처음 준비한다 (lazy_tools:
        # 인사에는 MCP를 부르지 않는다). 그렇지 않은 클라이언트(Bedrock)는 여기서 준비한다
        if not getattr(client_cache[model_id], "lazy_tools", False):
            with timer.step("mcp_init"):
                client_cache[model_id].initialize()

        print(f"클라이언트 초기화 완료 - 모델 ID: {model_id}")

    return client_cache[model_id]


def handle_llm1_with_mcp(body, origin, caller_id=None, caller_email=None):
    """
    MCP 클라이언트를 사용하여 llm1 요청을 처리하고 도구 사용 과정 및 결과 포함
    세션 기반 메시지 캐싱 지원 (개선된 messages 배열 방식)

    Args:
        body: 요청 본문
        origin: CORS origin
        caller_id: Cognito Authorizer가 검증한 요청자 sub (웹 요청), Slack 봇 직접 호출은 None
        caller_email: 요청자 이메일 (ID 토큰의 email, 감사 로그에 남긴다)

    Returns:
        응답 객체 (도구 사용 과정 및 결과 포함)
    """
    try:
        # 요청 데이터 추출
        user_input = body.get('question') or body.get('text') or body.get('input', {}).get('text', '')
        session_id = body.get('sessionId')
        is_cached = body.get('isCached', False)
        model_id = None  # 모델은 get_client가 정한다 (최신 Sonnet). 예전 화면·Slack이 보내던 modelId는 무시한다
        slack_user_id = body.get("user_id")
        slack_previous_questions = body.get("previous_questions")
        # 현재시간(한국)
        now = datetime.now(timezone.utc)
        print(f"=== 요청 분석 ===")
        print(f"user_input: {user_input}")
        print(f"session_id: {session_id}")
        print(f"is_cached: {is_cached}")
        print(f"model_id: {model_id}")
        print(f"slack_user_id: {slack_user_id}")
        print(f"slack 과거 기록: {len(slack_previous_questions) if slack_previous_questions else '없음'}")
        print(f"chat_table 상태: {chat_table is not None}")
        print(f"전체 body: {json.dumps(body, ensure_ascii=False)}")

        # 승인한(또는 거절한) 변경 작업의 결과를 설명해 달라는 요청: 질문은 서버가 저장된 기록으로 만든다
        action_id = body.get("actionId")
        if action_id:
            action = approval_store.get(action_id) if approval_store else None
            if not action or not caller_id or action.get("requesterId") != caller_id:
                return cors_response(404, {"error": "변경 작업을 찾을 수 없습니다."}, origin)
            if public_view(action)["status"] not in FINISHED:
                return cors_response(409, {"error": "아직 결정되지 않은 변경 작업입니다."}, origin)
            user_input = follow_up_prompt(action)

        if not user_input:
            return cors_response(400, {"error": "사용자 입력이 제공되지 않았습니다."}, origin)


        # 단계마다 걸린 시간 (끝에 로그 한 줄, timing.py)
        timer = Stopwatch()

        # 진행 상황: 화면이 보낸 requestId로 단계마다 기록한다 (웹 요청만. Slack 봇은 기록할 곳 없이 단계만 모은다)
        request_id = body.get('requestId')
        can_save = bool(progress_table is not None and caller_id and REQUEST_ID.match(request_id or ""))
        progress = ProgressReporter(progress_table if can_save else None, request_id, caller_id)

        # 민감정보 가리기: 요청마다 새로 만든다 (계정 ID·이메일의 가명 표가 요청마다 따로여야 한다)
        redactor = Redactor([ACCOUNT_ID])

        # 감사 로그: 웹은 Cognito sub, Slack은 Slack 사용자 ID로 요청자를 남긴다
        if caller_id:
            requester, source = caller_id, "web"
        elif slack_user_id:
            requester, source = f"slack:{slack_user_id}", "slack"
        else:
            requester, source = "unknown", "direct"
        audit = AuditLog(audit_table, audit_sink, redactor, user_id=requester, email=caller_email, source=source,
                         request_id=request_id, session_id=session_id, model_id=model_id, question=user_input)

        # MCP 클라이언트 가져오기
        client = get_client(timer)
        client.timer = timer
        # 시스템 프롬프트: 블록을 받는 클라이언트(Anthropic)에는 캐시되는 본문 + 요청 시각으로 나눠 보낸다 (system_prompt.py)
        system_prompt = (build_system_blocks(now) if getattr(client, "supports_system_blocks", False)
                         else build_system_prompt(now))
        client.progress = progress
        client.redactor = redactor
        client.audit = audit
        # 결과물(차트·다이어그램): 모델에는 참조만, 주소와 그릴 내용은 답변 정보로 (artifacts.py)
        artifacts = Artifacts()
        client.artifacts = artifacts
        # 변경 도구는 승인 화면이 있는 웹 요청에서만 요청할 수 있다 (Slack 봇·요청자를 모르는 경로는 None → 거절)
        approvals = (ApprovalRequester(approval_store, requester_id=caller_id, requester_email=caller_email,
                                       request_id=request_id, session_id=session_id, audit=audit)
                     if caller_id and approval_store is not None else None)
        client.approvals = approvals
        audit.model_id = getattr(client, "model_id", None) or model_id

        # 사용자 입력 처리 시작 시간 기록
        question_time = datetime.now(timezone.utc)

        # 인사·감사: 도구 목록·MCP·사고 없이 바로 답한다 (받는 클라이언트만. 변경 작업 설명 요청은 평소처럼)
        small_talk = (not action_id and is_small_talk(user_input)
                      and getattr(client, "supports_small_talk", False))
        answer_options = {"use_tools": False} if small_talk else {}

        # 세션 기반 처리 (개선된 방식 - messages 배열 사용)
        if slack_user_id and slack_previous_questions:
            print("=== slack 유저 확인 ===")
            response_text = client.process_user_input_with_history(
                user_input,
                system_prompt,
                slack_previous_questions,
                **answer_options
            )
        elif is_cached and session_id and chat_table:
            try:
                print(f"=== 세션 캐싱 모드 시작 (개선된 방식) ===")
                print(f"세션 ID: {session_id}")

                # 세션 메시지 히스토리를 messages 배열로 로드
                with timer.step("history"):
                    previous_messages = get_session_messages_as_array(session_id, caller_id)

                if previous_messages:
                    print(f"=== 히스토리 발견 ===")
                    print(f"로드된 메시지 수: {len(previous_messages)}")

                    # 메시지 샘플 출력 (디버깅용)
                    for i, msg in enumerate(previous_messages[-3:]):  # 마지막 3개만
                        print(f"최근 메시지 {i + 1}: {msg.get('role')} - {msg.get('content', '')[:50]}...")

                    # 이전 대화 + 새 사용자 입력으로 처리
                    response_text = client.process_user_input_with_history(
                        user_input,
                        system_prompt,
                        previous_messages,
                        **answer_options
                    )
                
                else:
                    print("=== 세션 메시지 없음 ===")
                    print("일반 모드로 처리")
                    response_text = client.process_user_input(user_input, system_prompt, **answer_options)

            except Exception as e:
                print(f"=== 세션 캐싱 오류 ===")
                print(f"오류: {str(e)}")
                print("일반 모드로 폴백")
                response_text = client.process_user_input(user_input, system_prompt, **answer_options)
        else:
            # 일반 처리 (기존 방식)
            print("=== 일반 모드 ===")
            print(f"is_cached: {is_cached}, session_id: {session_id}, chat_table: {chat_table is not None}")
            response_text = client.process_user_input(user_input, system_prompt, **answer_options)

        # 디버그 로그 가져오기 (추가된 get_debug_log 메서드 사용)
        debug_log = client.get_debug_log() if hasattr(client, "get_debug_log") else []

        # 도구 사용 및 사고 과정 정리
        tools_used = []
        reasoning_steps = []
        token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        for entry in debug_log:
            entry_type = entry.get("type")

            if entry_type == "model_reasoning":
                reasoning_steps.append({
                    "content": entry.get("content"),
                    "input_tokens": entry.get("input_tokens", 0),
                    "output_tokens": entry.get("output_tokens", 0),
                    "timestamp": entry.get("timestamp")
                })
            elif entry_type in ("tool_result", "tool_error"):
                step = tool_step(entry)
                if step:
                    tools_used.append(step)
            elif entry_type in ["final_response", "final_response_with_history"]:
                # 최종 응답에서 총 토큰 사용량 추출
                token_usage = {
                    "input_tokens": entry.get("input_tokens", 0),
                    "output_tokens": entry.get("output_tokens", 0),
                }
                # 캐시에서 읽은·캐시에 쓴 입력과 도구 검색 횟수 (Anthropic 클라이언트만. 도구 검색 전후 비교에 쓴다)
                for key in ("cache_read_input_tokens", "cache_creation_input_tokens", "tool_searches"):
                    if key in entry:
                        token_usage[key] = entry[key]
                print(f"최종 토큰 사용량 추출: {token_usage}")

        # 시간 순으로 정렬
        reasoning_steps.sort(key=lambda x: x.get("timestamp", 0))
        tools_used.sort(key=lambda x: x.get("timestamp", 0))

        # 타임스탬프 정보는 제거
        for step in reasoning_steps:
            if "timestamp" in step:
                del step["timestamp"]

        for tool in tools_used:
            if "timestamp" in tool:
                del tool["timestamp"]

        reasoning_content = []
        for step in reasoning_steps:
            reasoning_content.append({
                "content": step.get("content"),
                "input_tokens": step.get("input_tokens", 0),
                "output_tokens": step.get("output_tokens", 0)
            })

        progress.finished(True)
        # 답변과 추론 데이터는 화면에 보이고 대화 기록에 저장된다. 모델은 가린 값만 봤지만,
        # 도구 오류 메시지처럼 모델을 거치지 않고 들어오는 값이 있어 한 번 더 가린다
        response_text = redactor.text(response_text)
        debug_info = redactor.redact({
            "tools_used": tools_used,
            # 사고 요약과 도구 호출을 일어난 순서대로 (화면이 Claude Code처럼 순서대로 보여 준다)
            "steps": progress.saved_steps(),
            "reasoning": reasoning_content,
        })
        debug_info.update({
            "session_cached": is_cached and session_id is not None and chat_table is not None,
            "session_id": session_id if is_cached else None,
            "token_usage": token_usage,
            # 이번 요청에서 가린 값의 수 (종류별, 서로 다른 값 기준)
            "redacted": dict(redactor.counts),
            # 승인을 기다리는 변경 작업. 화면이 승인 카드로 보여 준다. 실제로 실행될 인자를 그대로 보여 줘야 하므로 가리지 않는다
            "pendingActions": [public_view(item) for item in approvals.created] if approvals else [],
            # 답변 속 ![제목](artifact://…)의 실제 주소와 그릴 내용. 화면에서 이미지로 열어야 하므로 가리지 않는다
            # (서버가 만든 presigned URL이고, 모델이 쓴 주소가 아니다)
            "artifacts": artifacts.public(),
        })
        # 감사 로그에는 사용자가 받은 답변(가린 뒤의 글자) 그대로. Slack으로 보낼 때 바꾸는 주소는 곧 만료되므로 넣지 않는다
        audit.request_finished(True, answer=response_text, usage=usage_of(client))
        emit_request_metrics(progress, redactor, approvals)
        timer.log(ok=True, model=getattr(client, "model_id", None), tools=len(tools_used), smallTalk=small_talk)

        # 응답 시간 기록 및 경과 시간 계산
        response_time = datetime.now(timezone.utc)
        elapsed = response_time - question_time
        minutes, seconds = divmod(elapsed.total_seconds(), 60)
        elapsed_str = f"{int(minutes)}분 {int(seconds)}초" if minutes else f"{int(seconds)}초"

        # 최종 결과를 Slack으로 전송
        if slack_user_id:
            try:
                # Slack은 참조를 풀 수 없어 실제 주소로 바꿔 보낸다
                send_slack_dm(slack_user_id, artifacts.with_urls(response_text))
                # 성공 응답 반환 (도구 사용 과정 및 결과 포함)
                return cors_response(200, {
                    "answer": response_text,
                    "elapsed_time": elapsed_str,
                    "inference": debug_info,  # 디버그 정보 추가
                    "llm_processing_status": "success"
                }, origin)
            except Exception as e:
                print(f"Slack 전송 실패: {str(e)}")
                return cors_response(500, {
                    "error": "Slack 전송 실패",
                    "answer": str(e),
                    "llm_processing_status": "success"
                }, origin)

        # 성공 응답 반환 (도구 사용 과정 및 결과 포함)
        return cors_response(200, {
            "answer": response_text,
            "elapsed_time": elapsed_str,
            "inference": debug_info  # 디버그 정보 추가
        }, origin)

    except Exception as e:
        print(f"MCP 처리 중 오류: {str(e)}")
        if 'timer' in locals():
            timer.log(ok=False)
        if 'progress' in locals():
            progress.finished(False)
        if 'audit' in locals():
            # 실패해도 그때까지 쓴 토큰의 요금은 나가므로 남긴다
            audit.request_finished(False, str(e), usage=usage_of(locals().get('client')))
        if 'progress' in locals() and 'redactor' in locals():
            emit_request_metrics(progress, redactor, locals().get('approvals'))
        return cors_response(500, {
            "error": "MCP 처리 중 오류 발생",
            # 오류 메시지에도 도구 결과 일부가 들어 있을 수 있다
            "answer": (redactor if 'redactor' in locals() else Redactor([ACCOUNT_ID])).text(str(e))
        }, origin)


def handle_progress(request_id, caller_id, origin):
    """GET /llm1/progress/{requestId}: 답변을 만드는 동안의 진행 상황 (요청한 사람만 볼 수 있다)."""
    try:
        progress = read_progress(progress_table, request_id, caller_id)
    except Exception as e:
        print(f"진행 상황 조회 오류: {str(e)}")
        progress = None
    if progress is None:
        # 아직 시작 전이거나(첫 기록 전) 남의 것이다. 화면은 404면 조금 뒤 다시 묻는다
        return cors_response(404, {"error": "진행 상황이 없습니다."}, origin)
    return cors_response(200, progress, origin)


def handle_audit(params, caller_id, claims, origin):
    """GET /audit: 감사 로그. admins 그룹만 볼 수 있다 (audit.query_audit, 일반 사용자는 403).
    trace=<actionId>가 있으면 그 변경 작업의 역추적 (audit_trace.query_trace),
    answer=<질문 행의 at>&user=<요청자>가 있으면 그 질문의 답변 전체 (audit.query_answer)."""
    try:
        if (params or {}).get("trace"):
            return cors_response(200, query_trace(audit_table, caller_id, claims, params), origin)
        if (params or {}).get("answer"):
            return cors_response(200, query_answer(audit_table, caller_id, claims, params), origin)
        return cors_response(200, query_audit(audit_table, caller_id, claims, params), origin)
    except AuditQueryError as error:
        return cors_response(error.status, {"error": str(error)}, origin)


def handle_dashboard(claims, origin):
    """GET /dashboard: 홈 대시보드 (dashboard_view.py). 로그인한 사용자 누구나 본다 (조회만, 대화로 물을 수 있는 것과 같은 범위).
    AWS는 부르지 않고 모아 둔 구역과 승인 대기·감사 로그만 읽는다. 승인 대기는 이 사용자가 볼 수 있는 것만 센다."""
    caller_id = (claims or {}).get("sub")
    if not caller_id:
        return cors_response(401, {"error": "로그인이 필요합니다."}, origin)
    if dashboard_store is None:
        return cors_response(503, {"error": "대시보드 테이블이 설정되지 않았습니다."}, origin)
    now = int(time.time())
    try:
        approvals = pending_approvals(approval_store, can_view, caller_id, groups_of(claims), now)
    except Exception as error:  # 승인 대기를 못 읽어도 나머지는 보인다
        print(f"대시보드 승인 대기 조회 실패: {error}")
        approvals = {"pending": 0, "unavailable": True}
    try:
        app_changes = app_changes_from_audit(audit_table, now)
    except Exception as error:
        print(f"대시보드 변경 기록 조회 실패: {error}")
        app_changes = []
    view = build_view(dashboard_store.get_all(), env=os.environ.get("ENV", "dev"),
                      region=os.environ.get("AWS_REGION", ""), approvals=approvals, app_changes=app_changes, now=now)
    return cors_response(200, view, origin)


def usage_of(client):
    """질문 하나에 쓴 토큰 (감사 로그용). usage_summary가 없는 클라이언트(Bedrock)나 클라이언트를 만들기 전이면 None."""
    summary = getattr(client, "usage_summary", None)
    try:
        return summary() if callable(summary) else None
    except Exception as error:
        print(f"토큰 사용량을 읽지 못함 (감사 로그에 토큰 없이 남김): {error}")
        return None


def emit_request_metrics(progress, redactor, approvals):
    """질문 하나의 거버넌스 지표 (metrics.py). 진행 상황에 쌓인 도구 단계와 가리기·승인 요청 수로 센다."""
    tools = [step for step in progress.steps if step.get("type") == "tool"]
    metrics.emit({
        "ToolCalls": len(tools),
        "ToolErrors": sum(step.get("status") == "error" for step in tools),
        "InjectionSuspected": sum(bool(step.get("suspicious")) for step in tools),
        "RedactedValues": sum(redactor.counts.values()),
        "ApprovalRequested": len(approvals.created) if approvals else 0,
    })


def _mcp_url():
    return os.environ.get('MCP_URL') or get_config().get('mcp', {}).get('function_url')


def call_mcp_tool(tool, args, meta=None):
    """MCP 도구 하나를 부른다 (승인된 변경 작업 실행용). 세션을 열고 부른 뒤 닫는다."""
    mcp = MCPClient(_mcp_url())
    try:
        return mcp.call_tool(tool, args, meta=meta)
    finally:
        try:
            mcp.close()
        except Exception as error:
            print(f"MCP 세션 닫기 실패 (무시): {error}")


def handle_action(action_id, operation, claims, origin):
    """변경 작업 승인 API (approvals.py).
    GET /actions/{id}: 상태 · POST /actions/{id}/approve: 승인하고 실행 · POST /actions/{id}/deny: 거절"""
    caller_id = (claims or {}).get("sub")
    groups = groups_of(claims)
    try:
        if approval_store is None:
            raise ApprovalError(503, "변경 작업 승인 테이블이 설정되지 않았습니다")
        item = approval_store.get(action_id)
        if operation == "get":
            if not caller_id or not item or not can_view(item, caller_id, groups):
                raise ApprovalError(404, "승인 요청을 찾을 수 없습니다")
            return cors_response(200, public_view(item), origin)

        approve = operation == "approve"
        item = check_decision(item, caller_id, groups, approve, approval_mode(os.environ.get("ENV", "dev")))
        auditor = AuditLog(audit_table, audit_sink, Redactor([ACCOUNT_ID]), user_id=caller_id,
                           email=(claims or {}).get("email"), source="web", request_id=item.get("requestId"),
                           session_id=item.get("sessionId"), model_id=None, question="")
        # 결정을 감사 로그에 먼저 남긴다. 남기지 못하면 승인하지 않는다 (기록 없이 AWS가 바뀌지 않게)
        try:
            auditor.action_event("approved" if approve else "denied", item, decided_by=caller_id)
        except Exception as error:
            print(f"감사 로그 저장 실패로 결정을 멈춤: {error}")
            raise ApprovalError(503, "감사 로그를 남기지 못해 처리하지 않았습니다. 잠시 뒤 다시 시도해 주세요")
        approval_store.set_decision(action_id, APPROVED if approve else DENIED, caller_id)
        metrics.emit({"ApprovalApproved" if approve else "ApprovalDenied": 1})
        if not approve:
            return cors_response(200, public_view(approval_store.get(action_id) or item), origin)

        latest = execute_approved(approval_store, item, call_mcp_tool)
        if latest.get("status") != "executed":
            metrics.emit({"ActionFailed": 1})
        try:
            auditor.action_event(latest.get("status", "failed"), latest, decided_by=caller_id,
                                 result=latest.get("result"))
        except Exception as error:  # 이미 실행되었다. 결과는 승인 테이블과 MCP 로그에 남아 있다
            print(f"실행 결과 감사 로그 저장 실패: {error}")
        return cors_response(200, public_view(latest), origin)
    except ApprovalError as error:
        return cors_response(error.status, {"error": str(error)}, origin)


def get_table_registry():
    dynamodb = boto3.resource("dynamodb")
    table_name = os.environ.get("ATHENA_TABLE_REGISTRY_TABLE")
    table = dynamodb.Table(table_name)
    response = table.scan()
    return {item["log_type"]: item for item in response.get("Items", [])}


def trans_eng_to_kor(text):
    prompt = f"""
You are a professional translator.
The following text may include a list of citations in dictionary-like format.
Translate only the explanation sentences into Korean.

- Remove any technical field names like 'rank_order', 'context', 'title', 'url'.
- Preserve any URLs and titles.
- Do NOT translate URLs or titles.
- Format the result cleanly so that each citation appears as:

한국어 번역된 설명
원래 제목
원래 URL

Translate the text below accordingly.
Only Generate Translate.

Text to translate:
{text}
"""
    response = invoke_bedrock_nova(prompt)
    trans_response = response["output"]["message"]["content"][0]["text"]
    return trans_response


def build_llm1_prompt(user_input):
    registry = get_table_registry()
    # 레지스트리에 정보가 있는지 확인
    if "cloudtrail" in registry and "guardduty" in registry:
        ct_table = registry["cloudtrail"]["table_name"]
        ct_location = registry["cloudtrail"]["s3_path"]
        gd_table = registry["guardduty"]["table_name"]
        gd_location = registry["guardduty"]["s3_path"]

        # 테이블 정보를 프롬프트에 명시적으로 포함
        tables_info = f"""
Available tables:
1. {ct_table} - CloudTrail logs at {ct_location}
2. {gd_table} - GuardDuty logs at {gd_location}
        """
    else:
        # 테이블 정보가 없는 경우에 대한 기본값
        print("WARNING: Table registry information missing")
        tables_info = """
Available tables:
1. cloudtrail_logs - CloudTrail logs 
2. guardduty_logs - GuardDuty logs
        """

    return f'''
You are a SQL generation expert for AWS Athena (Presto SQL).
Generate ONLY SQL code that is valid in Athena with no explanation.

{tables_info}

Task:
Convert the following natural language question into an SQL query using the available tables.

Model Instructions:
    # Output Requirements:
        - Return only the SQL code, no explanations.
        - If filtering by date, use the `"partition_date"` field only.
        - If counting unique users, use `COUNT(DISTINCT userIdentity.userName)`.
        - If you use a field that is not aggregated (like username), you must include it in the GROUP BY clause.
        - Avoid using non-aggregated expressions in SELECT unless they are grouped.
        - If filtering by user name, exclude records where useridentity.username is null or empty string.
	    - Use IS NOT NULL AND useridentity.username != '' to ensure only valid user names are considered.
	    - If partition_date is a string like yyyy/MM/dd, use date_parse(partition_date, '%Y/%m/%d') to convert it before filtering by date.


User Question:
{user_input}
'''


def build_llm2_prompt(user_input, query_result):
    return f'''
You are an assistant that provides clear and accurate natural language explanations based on database query results.

Task:
Generate a human-readable answer based on the original user question and the SQL query result.

Original User Question:
{user_input}

SQL Query Result (as JSON):
{json.dumps(query_result, indent=2)}

Instructions:
- Be specific using the data.
- Use concise, professional language.
- Answer in Korean
- Do not ask user for clarification.
- If the original query includes grouping or aggregation, make sure to reflect the logic accurately.
- Highlight any anomalies or low counts if the data is sparse.
'''


def parse_body(event):
    content_type = event.get("headers", {}).get("Content-Type", "") or \
                   event.get("headers", {}).get("content-type", "")

    raw_body = event.get("body") or ""

    if "application/json" in content_type:
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            print("❗ 잘못된 JSON body:", raw_body)
            return {}

    elif "application/x-www-form-urlencoded" in content_type:
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw_body).items()}

    return {}


def markdown_to_slack_mrkdwn(text):
    # 헤더를 볼드로 치환 (모든 헤더 레벨)
    text = re.sub(r'^(#{1,6})\s*(.*)', r'*\2*', text, flags=re.MULTILINE)

    # 볼드: **텍스트** → *텍스트*
    text = re.sub(r'\*\*(\S(.*?\S)?)\*\*', r'*\1*', text)

    # 이탤릭: *텍스트* 또는 _텍스트_ → _텍스트_
    text = re.sub(r'\*(\S(.*?\S)?)\*', r'_\1_', text)

    # 취소선: ~~텍스트~~ → ~텍스트~
    text = re.sub(r'~~(.*?)~~', r'~\1~', text)

    # 인라인 코드(`code`) 및 코드블록(``````)은 그대로 유지

    # 링크: [텍스트](URL) → <URL|텍스트>
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<\2|\1>', text)

    # 표: Slack mrkdwn에서 지원하지 않으므로 제거
    text = re.sub(r'\|.*\|', '', text)

    # 블록 인용: > 인용문은 그대로 유지

    # 이미지: Slack mrkdwn에서 지원하지 않으므로 제거
    text = re.sub(r'!\[(.*?)\]\((.*?)\)', '', text)

    return text


def send_slack_dm(user_id, response_text):
    CONFIG = get_config()
    client = WebClient(token=CONFIG['slackbot']['token'])  # 여기에 Slack Bot Token
    response_text = markdown_to_slack_mrkdwn(response_text)

    response = client.chat_postMessage(
        channel=user_id,  # 여기서 user_id 그대로 DM 채널로 사용 가능
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🧠 분석 결과:\n{response_text}"
                    )
                }
            }
        ]
    )
    if not response["ok"]:
        print("❌ Slack 메시지 실패 사유:", response["error"])
    return response