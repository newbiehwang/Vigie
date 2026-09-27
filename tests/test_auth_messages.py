"""Cognito 인증 메일 (services/auth_messages/lambda_function.py, cloudformation/base.yaml, deploy.sh).
- 메일마다 제목·본문이 다르고, Cognito가 실제 값으로 바꿀 자리 표시({####}, 초대는 {username}도)가 늘 들어 있다
  (빠지면 Cognito가 메일을 보내지 않는다)
- 꾸미지 않는 메일(MFA 등)은 건드리지 않는다
- 본문은 Cognito 한도(20,000자) 안이다
- 템플릿: 코드 버전이 있을 때만 Lambda를 만들어 사용자 풀에 연결하고, 이 사용자 풀만 부를 수 있다
- deploy.sh: base 스택을 업데이트할 때마다 코드 버전을 넘긴다 (관리자 초대 메일보다 먼저 연결된다)
실제 AWS는 부르지 않는다."""
import pytest
import yaml

from conftest import ROOT, load_service_module

APP = "https://dev.d1234.amplifyapp.com"
LOGO = f"{APP}/vigie-email-logo.png"
CODE_TRIGGERS = ["CustomMessage_SignUp", "CustomMessage_ResendCode", "CustomMessage_ForgotPassword",
                 "CustomMessage_UpdateUserAttribute", "CustomMessage_VerifyUserAttribute"]


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("APP_URL", APP)
    monkeypatch.setenv("LOGO_URL", LOGO)
    return load_service_module("services/auth_messages", "lambda_function")


def event(trigger, **request):
    return {"triggerSource": trigger, "userPoolId": "ap-southeast-2_x", "userName": "abc",
            "request": {"userAttributes": {"email": "kim@example.com"}, "codeParameter": "{####}", **request},
            "response": {"smsMessage": None, "emailMessage": None, "emailSubject": None}}


@pytest.mark.parametrize("trigger", CODE_TRIGGERS)
def test_code_mails_carry_the_code_placeholder(mod, trigger):
    out = mod.lambda_handler(event(trigger), None)["response"]
    assert out["emailSubject"].startswith("[Vigie] ")
    assert "{####}" in out["emailMessage"]
    assert f'src="{LOGO}"' in out["emailMessage"]
    assert len(out["emailMessage"]) < 20000


def test_each_mail_has_its_own_subject(mod):
    subjects = {trigger: mod.lambda_handler(event(trigger), None)["response"]["emailSubject"]
                for trigger in ["CustomMessage_SignUp", "CustomMessage_ForgotPassword", "CustomMessage_AdminCreateUser"]}
    assert subjects == {"CustomMessage_SignUp": "[Vigie] 가입 인증 코드",
                        "CustomMessage_ForgotPassword": "[Vigie] 비밀번호 재설정 코드",
                        "CustomMessage_AdminCreateUser": "[Vigie] 계정이 만들어졌습니다"}
    reset = mod.lambda_handler(event("CustomMessage_ForgotPassword"), None)["response"]["emailMessage"]
    assert "1시간" in reset and "비밀번호는 바뀌지 않습니다" in reset


def test_invite_has_username_password_and_app_link(mod):
    body = mod.lambda_handler(event("CustomMessage_AdminCreateUser", usernameParameter="{username}"),
                              None)["response"]["emailMessage"]
    assert "{username}" in body and "{####}" in body
    assert f'href="{APP}"' in body and "7일" in body


def test_other_triggers_keep_cognito_defaults(mod):
    original = event("CustomMessage_Authentication")
    out = mod.lambda_handler(original, None)["response"]
    assert out == {"smsMessage": None, "emailMessage": None, "emailSubject": None}


def test_without_logo_url_the_name_is_written_as_text(mod, monkeypatch):
    monkeypatch.setenv("LOGO_URL", "")
    body = mod.lambda_handler(event("CustomMessage_SignUp"), None)["response"]["emailMessage"]
    assert "<img" not in body and ">Vigie</p>" in body


# ---------------------------------------------------------------- 템플릿 · deploy.sh

class CfnLoader(yaml.SafeLoader):
    pass


CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                                if isinstance(node, yaml.ScalarNode)
                                else loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
                                else loader.construct_mapping(node))


def base():
    return yaml.load((ROOT / "cloudformation" / "base.yaml").read_text(encoding="utf-8"), Loader=CfnLoader)


def test_lambda_is_created_only_with_a_code_key_and_only_this_pool_can_call_it():
    template = base()
    res = template["Resources"]
    assert template["Parameters"]["CodeVersion"]["Default"] == ""
    for name in ("AuthMessagesRole", "AuthMessagesLogGroup", "AuthMessagesFunction", "AuthMessagesPermission"):
        assert res[name]["Condition"] == "HasAuthMessages"
    config = res["UserPool"]["Properties"]["LambdaConfig"]
    assert config[0] == "HasAuthMessages" and config[1] == {"CustomMessage": "AuthMessagesFunction.Arn"}
    permission = res["AuthMessagesPermission"]["Properties"]
    assert permission["Principal"] == "cognito-idp.amazonaws.com" and permission["SourceArn"] == "UserPool.Arn"
    env = res["AuthMessagesFunction"]["Properties"]["Environment"]["Variables"]
    assert env["LOGO_URL"].endswith("/vigie-email-logo.png")
    assert (ROOT / "frontend" / "public" / "vigie-email-logo.png").exists()


def test_deploy_passes_the_code_key_on_every_base_update():
    script = (ROOT / "deploy.sh").read_text()
    assert "upload_auth_messages() {" in script
    assert script.count("ParameterKey=CodeVersion,ParameterValue=$AUTH_MESSAGES_VERSION") == 3
    # 관리자 초대 메일(ensure_admin_account)보다 먼저 Lambda가 연결된다
    assert script.index("upload_auth_messages\ncfn_update $BASE_STACK_NAME") < script.index('ensure_admin_account "$USER_POOL_ID"')
