# llm/lambda_function.py
import requests
from llm_service import (current_model, parse_body, handle_llm1_with_mcp, handle_progress, handle_audit,
                         handle_action, handle_dashboard)
from common.config import get_config
from common.utils import cors_response


def lambda_handler(event, context):
    CONFIG = get_config()
    path = event.get("path", "")
    http_method = event.get("httpMethod", "")
    origin = event.get("headers", {}).get("origin", f"https://{CONFIG['amplify']['default_domain_with_env']}")

    if http_method == "OPTIONS":
        response = cors_response(200, "", origin)
        return response

    try:
        body = parse_body(event) or {}
        if path == "/health" and http_method == "GET":
            # 상태와 지금 쓰는 모델 (최신 Sonnet. 고르는 기능은 없다)
            model = current_model()
            return cors_response(200, {"status": "ok", "model": {"id": model["id"],
                                                                "display_name": model.get("display_name")}}, origin)

        elif path == "/llm1" and http_method == "POST":
            claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
            caller_id = claims.get("sub")
            if caller_id:
                # API Gateway(웹) 요청은 Slack 전용 필드를 사용할 수 없다 (임의 Slack 사용자에게 DM 전송 방지).
                # Slack 봇은 API Gateway를 거치지 않고 Lambda를 직접 호출한다.
                body.pop("user_id", None)
                body.pop("previous_questions", None)
            return handle_llm1_with_mcp(body, origin, caller_id, claims.get("email"))

        elif path.startswith("/llm1/progress/") and http_method == "GET":
            # 답변을 만드는 동안의 진행 상황 (화면이 /llm1 응답을 기다리며 1초마다 묻는다)
            claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
            return handle_progress(path.rsplit("/", 1)[-1], claims.get("sub"), origin)

        elif path == "/audit" and http_method == "GET":
            # 감사 로그 (누가 어떤 도구를 불렀나). 관리자 여부는 ID 토큰의 cognito:groups로 판단한다
            claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
            return handle_audit(event.get("queryStringParameters") or {}, claims.get("sub"), claims, origin)

        elif path == "/dashboard" and http_method == "GET":
            # 홈 대시보드 (모아 둔 운영 현황 + 승인 대기). 로그인한 사용자 누구나
            claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
            return handle_dashboard(claims, origin)

        elif path.startswith("/actions/"):
            # 변경 작업 승인: GET /actions/{id}, POST /actions/{id}/approve, POST /actions/{id}/deny
            claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
            parts = path.strip("/").split("/")
            if len(parts) == 2 and http_method == "GET":
                return handle_action(parts[1], "get", claims, origin)
            if len(parts) == 3 and parts[2] in ("approve", "deny") and http_method == "POST":
                return handle_action(parts[1], parts[2], claims, origin)
            return cors_response(404, {"error": f"Route {http_method} {path} not found."}, origin)

        else:
            return cors_response(404, {"error": f"Route {http_method} {path} not found."}, origin)

    except Exception as e:
        return cors_response(500, {"error": "Internal server error", "detail": str(e)}, origin)