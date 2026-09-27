# layers/common/utils.py
import json
import boto3
import requests
import logging
from botocore.exceptions import ClientError
from common.config import AWS_REGION
from common.config import get_config

# 로깅 설정
logger = logging.getLogger("vigie-utils")
logger.setLevel(logging.INFO)

def format_api_response(status_code, body, headers=None):
    """
    API Gateway 응답을 포맷팅합니다.

    Args:
        status_code (int): HTTP 상태 코드
        body (dict/list/str): 응답 본문
        headers (dict, optional): 추가 헤더

    Returns:
        dict: API Gateway 응답 객체
    """
    # 기본 Content-Type 헤더 설정
    default_headers = {
        'Content-Type': 'application/json; charset=utf-8',
    }

    # 사용자 정의 헤더 추가
    if headers:
        default_headers.update(headers)

    response = {
        'statusCode': status_code,
        'headers': default_headers,
        'body': json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
    }
    return response

def handle_api_exception(e, status_code=500):
    """
    API 예외를 처리합니다.

    Args:
        e (Exception): 예외 객체
        status_code (int): HTTP 상태 코드

    Returns:
        dict: API Gateway 응답 객체
    """
    error_message = str(e)
    logger.error(f"API 오류: {error_message}", exc_info=True)

    return format_api_response(
        status_code,
        {'error': error_message}
    )

def normalize_event(event):
    """
    API Gateway 이벤트를 정규화합니다.

    Args:
        event (dict): API Gateway 이벤트

    Returns:
        dict: 정규화된 이벤트
    """
    # 쿼리 파라미터 처리
    query_params = event.get('queryStringParameters', {}) or {}

    # 요청 바디 처리
    body = None
    if 'body' in event and event['body']:
        try:
            body = json.loads(event['body'])
        except (json.JSONDecodeError, TypeError):
            body = event['body']

    # 정규화된 이벤트 생성
    normalized_event = {
        'path': event.get('path', '/'),
        'httpMethod': event.get('httpMethod', 'GET'),
        'queryParameters': query_params,
        'body': body,
        'headers': event.get('headers', {}),
        'requestContext': event.get('requestContext', {}),
        'claims': event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    }
    
    return normalized_event

def create_error_response(message, status_code=400):
    """
    오류 응답을 생성합니다.
    
    Args:
        message (str): 오류 메시지
        status_code (int): HTTP 상태 코드
        
    Returns:
        dict: API Gateway 응답 객체
    """
    return format_api_response(
        status_code, 
        {"error": True, "message": message}
    )

def create_success_response(data=None, message=None):
    """
    성공 응답을 생성합니다.
    
    Args:
        data: 응답 데이터
        message (str, optional): 성공 메시지
        
    Returns:
        dict: API Gateway 응답 객체
    """
    response_body = {"success": True}
    
    if data is not None:
        response_body["data"] = data
    
    if message:
        response_body["message"] = message
    
    return format_api_response(200, response_body)

def get_allowed_origins():
    """CORS 허용 Origin: 배포된 프론트엔드(Amplify) 도메인, dev 환경에서는 로컬 개발 서버 포함"""
    config = get_config()
    origins = set()
    if config['amplify'].get('default_domain_with_env'):
        origins.add(f"https://{config['amplify']['default_domain_with_env']}")
    if config['frontend'].get('redirect_domain'):
        origins.add(f"https://{config['env']}.{config['frontend']['redirect_domain']}")
    if config['env'] == 'dev':
        origins.update({"http://localhost:5173", "http://localhost:3000"})
    return origins


def cors_headers(origin):
    # 요청 Origin을 그대로 반사하지 않고 허용 목록에 있을 때만 돌려준다 (credentials 허용과 함께 쓰이므로 필수)
    allowed = get_allowed_origins()
    if origin not in allowed:
        origin = next((o for o in sorted(allowed) if o.startswith("https://")), "")
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Access-Control-Allow-Headers": "Authorization,Content-Type",
        "Access-Control-Allow-Credentials": "true"
    }

def cors_response(status_code, body, origin):
    return {
        "statusCode": status_code,
        "headers": cors_headers(origin),
        "body": json.dumps(body) if isinstance(body, dict) else body
    }

def invoke_bedrock_nova(prompt):
    CONFIG = get_config()
    bedrock = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
    # Knowledge Base ID 설정
    kb_id = CONFIG["kb"]["kb_id"]
    if not kb_id:
        raise RuntimeError("KB_ID(SM) 가 설정되지 않았습니다.")
    
    # Knowledge Base 사용하도록 수정
    response = bedrock.retrieve_and_generate(
        input={"text": prompt},
        retrieveAndGenerateConfiguration={
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": f"arn:aws:bedrock:{AWS_REGION}::foundation-model/amazon.nova-pro-v1:0"
            },
            "type": "KNOWLEDGE_BASE",
        }
    )
    return {
        "output": {
            "message": {
                "content": [
                    {"text": response["output"]["text"]}
                ]
            }
        },
        "citations": response.get("citations", [])
    }