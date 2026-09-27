"""공통 테스트 설정

- 실제 AWS에 요청이 나가지 않도록 가짜 자격 증명을 쓰고, AWS 호출은 moto로 모킹한다.
- Lambda 코드가 SSM에서 읽는 설정(common.config)은 고정된 테스트 설정으로 대체한다.
- 서비스마다 lambda_function.py처럼 같은 이름의 모듈이 있어서, load_service_module로 경로 기준으로 새로 불러온다.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent

# boto3 클라이언트가 만들어지기 전에 설정해야 한다
os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "ENV": "test",
})

# layers/common → `import common`
sys.path.insert(0, str(ROOT / "layers"))

TEST_CONFIG = {
    "aws_region": "us-east-1",
    "env": "test",
    "amplify": {"app_id": "", "default_domain": "abc.amplifyapp.com",
                "default_domain_with_env": "test.abc.amplifyapp.com"},
    "api": {"endpoint": "https://api.example.com/test"},
    "frontend": {"redirect_domain": "abc.amplifyapp.com"},
    "cognito": {"user_pool_id": "us-east-1_TEST", "client_id": "test-client-id",
                "domain": "https://auth.example.com", "identity_pool_id": ""},
    "slackbot": {"token": "xoxb-test", "signing_secret": "test-signing-secret"},
    "mcp": {"function_url": "https://abc123.lambda-url.us-east-1.on.aws/"},
    "kb": {"kb_id": ""},
    "db": {"chat_history_table": "vigie-chat-history-test"},
    "anthropic": {"api_key": ""},
    "s3": {},
}


@pytest.fixture(autouse=True)
def test_config(monkeypatch):
    import common.config
    monkeypatch.setattr(common.config, "_config", TEST_CONFIG)
    return TEST_CONFIG


@pytest.fixture
def aws():
    with mock_aws():
        yield


# 서비스 디렉터리 간에 이름이 겹치는 모듈
_SERVICE_MODULES = {"lambda_function", "llm_service", "mcp_client", "chat_history_service",
                    "slackbot_service", "slack_security", "app", "lambda_mcp"}


def load_service_module(service_dir, module_name):
    """services/<dir> 또는 mcp 디렉터리를 import 경로 맨 앞에 두고 모듈을 새로 불러온다."""
    path = str(ROOT / service_dir)
    for name in list(sys.modules):
        if name.split(".")[0] in _SERVICE_MODULES:
            del sys.modules[name]
    sys.path[:] = [p for p in sys.path if not p.startswith(str(ROOT / "services")) and p != str(ROOT / "mcp")]
    sys.path.insert(0, path)
    return importlib.import_module(module_name)
