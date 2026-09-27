import json
import boto3
import requests
import time
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# MCP 서버 연결을 컨테이너 안에서 다시 쓴다 (도구를 부를 때마다 TLS 연결을 새로 맺지 않게).
# 제한 시간: 연결 10초, 응답 180초 (MCP Lambda의 제한 시간과 같다)
HTTP = requests.Session()
HTTP_TIMEOUT = (10, 180)
_credentials = None  # Lambda 실행 Role 자격 증명 (컨테이너마다 한 번 읽는다. 만료가 가까우면 botocore가 새로 받는다)


def _aws_credentials():
    global _credentials
    if _credentials is None:
        _credentials = boto3.Session().get_credentials()
    return _credentials


class MCPClient:
    """MCP(Model Context Protocol) Streamable HTTP 클라이언트 구현"""

    def __init__(self, mcp_url: str, auth_token: str = None, session_id: str = None):
        """
        MCP 클라이언트 초기화

        Args:
            mcp_url: MCP 서버 URL (Lambda Function URL 또는 Fargate 서비스 URL)
            auth_token: 인증 토큰 (선택 사항)
            session_id: 기존 세션 ID (선택 사항)
        """
        self.mcp_url = mcp_url.rstrip('/')
        self.session_id = session_id
        self.headers = {
            'Content-Type': 'application/json',
            'MCP-Version': '0.6'
        }

        # Lambda Function URL(AuthType: AWS_IAM)이면 SigV4 서명, 그 외(로컬 서버 등)는 Bearer 토큰 사용
        host = urlparse(self.mcp_url).hostname or ''
        self.use_sigv4 = '.lambda-url.' in host
        if self.use_sigv4:
            # 호스트 형식: <url-id>.lambda-url.<region>.on.aws
            self.region = host.split('.lambda-url.')[1].split('.')[0]
        elif auth_token:
            self.headers['Authorization'] = f'Bearer {auth_token}'

    def _send(self, method: str, payload: Optional[Dict[str, Any]] = None) -> requests.Response:
        """MCP 서버로 요청 전송 (Function URL이면 Lambda 실행 Role 자격 증명으로 SigV4 서명)"""
        body = json.dumps(payload) if payload is not None else None
        headers = dict(self.headers)

        if self.use_sigv4:
            credentials = _aws_credentials()
            aws_request = AWSRequest(method=method, url=self.mcp_url, data=body, headers=headers)
            SigV4Auth(credentials, 'lambda', self.region).add_auth(aws_request)
            headers = dict(aws_request.headers)

        return HTTP.request(method, self.mcp_url, headers=headers, data=body, timeout=HTTP_TIMEOUT)

    def initialize(self) -> str:
        """
        MCP 서버와 세션 초기화

        Returns:
            생성된 세션 ID
        """
        payload = {
            "jsonrpc": "2.0",
            "id": str(int(time.time() * 1000)),  # 타임스탬프 기반 ID 생성
            "method": "initialize"
        }

        response = self._send('POST', payload)

        if response.status_code != 200:
            raise Exception(f"MCP 초기화 실패: {response.status_code} - {response.text}")

        # 세션 ID 추출 및 저장
        self.session_id = response.headers.get('MCP-Session-Id')
        if not self.session_id:
            raise Exception("MCP 세션 ID를 받지 못했습니다")

        # 세션 ID를 가진 새 헤더 설정
        self.headers['MCP-Session-Id'] = self.session_id

        return self.session_id

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        사용 가능한 도구 목록 조회

        Returns:
            도구 목록
        """
        # 세션 ID가 없으면 초기화 필요
        if not self.session_id:
            self.initialize()

        payload = {
            "jsonrpc": "2.0",
            "id": str(int(time.time() * 1000)),
            "method": "tools/list"
        }

        response = self._send('POST', payload)

        if response.status_code != 200:
            raise Exception(f"도구 목록 조회 실패: {response.status_code} - {response.text}")

        result = response.json()
        if 'error' in result:
            raise Exception(f"도구 목록 조회 오류: {result['error']['message']}")

        return result.get('result', {}).get('tools', [])

    def call_tool(self, tool_name: str, tool_args: Dict[str, Any] = None,
                  meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        특정 도구 호출

        Args:
            tool_name: 호출할 도구 이름
            tool_args: 도구에 전달할 인자 (선택 사항)
            meta: MCP params._meta (변경 도구의 승인된 작업 ID·미리 보기 요청, approvals.py)

        Returns:
            도구 실행 결과
        """
        # 세션 ID가 없으면 초기화 필요
        if not self.session_id:
            self.initialize()

        payload = {
            "jsonrpc": "2.0",
            "id": str(int(time.time() * 1000)),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": tool_args or {}
            }
        }
        if meta:
            payload["params"]["_meta"] = meta

        response = self._send('POST', payload)

        if response.status_code != 200:
            raise Exception(f"도구 호출 실패: {response.status_code} - {response.text}")

        result = response.json()
        if 'error' in result:
            raise Exception(f"도구 호출 오류: {result['error']['message']}")

        return result.get('result', {})

    def close(self) -> bool:
        """
        MCP 세션 종료

        Returns:
            성공 여부
        """
        if not self.session_id:
            return True  # 세션이 없으면 이미 종료된 것으로 간주

        response = self._send('DELETE')

        # 세션 ID 초기화
        self.session_id = None
        if 'MCP-Session-Id' in self.headers:
            del self.headers['MCP-Session-Id']

        return response.status_code == 204  # 세션 삭제 성공은 204 No Content