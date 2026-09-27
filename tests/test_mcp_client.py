"""LLM → MCP 서버 호출: Function URL(AWS_IAM)에는 SigV4 서명, 로컬 서버에는 Bearer 토큰"""
import json
from unittest.mock import patch

import pytest
from botocore.credentials import Credentials

from conftest import load_service_module


class FakeResponse:
    status_code = 200
    headers = {"MCP-Session-Id": "sess-1"}
    text = ""

    def json(self):
        return {"result": {"tools": [{"name": "list_cloudwatch_dashboards"}]}}


@pytest.fixture
def mcp_client():
    return load_service_module("services/llm", "mcp_client")


@pytest.fixture
def sent(mcp_client):
    """MCP 서버로 나가는 요청 (컨테이너 안에서 다시 쓰는 연결 mcp_client.HTTP로 보낸다)."""
    captured = []

    def fake_request(method, url, headers=None, data=None, timeout=None):
        captured.append({"method": method, "url": url, "headers": headers, "data": data, "timeout": timeout})
        return FakeResponse()

    with patch.object(mcp_client.HTTP, "request", fake_request):
        yield captured


def test_function_url_requests_are_sigv4_signed(mcp_client, sent):
    creds = Credentials("AKIDEXAMPLE", "secret", "session-token")
    with patch.object(mcp_client.boto3, "Session") as session:
        session.return_value.get_credentials.return_value = creds
        client = mcp_client.MCPClient("https://abc123.lambda-url.ap-northeast-2.on.aws/")
        client.initialize()
        client.list_tools()
        client.close()

    init, tools, close = sent
    auth = init["headers"]["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/ap-northeast-2/lambda/aws4_request" in auth
    assert init["headers"]["X-Amz-Security-Token"] == "session-token"
    # 서명한 본문이 그대로 전송되고, 세션 헤더도 서명 대상에 포함된다
    assert json.loads(init["data"])["method"] == "initialize"
    assert "mcp-session-id" in tools["headers"]["Authorization"].lower()
    assert close["method"] == "DELETE" and close["data"] is None
    assert init["timeout"] == mcp_client.HTTP_TIMEOUT  # 응답이 오지 않아도 영원히 기다리지 않는다


def test_non_lambda_url_uses_bearer_token(mcp_client, sent):
    client = mcp_client.MCPClient("http://localhost:8080", auth_token="local-token")
    client.initialize()
    assert not client.use_sigv4
    assert sent[0]["headers"]["Authorization"] == "Bearer local-token"
