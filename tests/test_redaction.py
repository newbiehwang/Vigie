"""민감정보 가리기 (redaction.py, mcp_anthropic_client.py, llm_service.py)

- 비밀 값은 [REDACTED:종류]로 바꾸고 되돌리지 않는다
- 계정 ID·액세스 키 ID·이메일은 요청마다 같은 가명으로 바꾸고, 도구를 부를 때만 원래 값으로 되돌린다
- 오탐: 요청 ID·바이트 수·커밋 해시·역할 고유 ID 등은 가리지 않는다
- 적용 위치: Claude로 나가는 질문·이전 대화·도구 결과, 진행 상황, 최종 답변과 추론 데이터

키 모양 값은 저장소 비밀 값 검사(test_secret_patterns.py)에 걸리지 않도록 실행할 때 조각을 이어 붙여 만든다.
"""
import copy
import json

import pytest

from conftest import load_service_module

ACCOUNT = "111122223333"  # 이 Lambda의 계정 (AWS 문서 예시 값)
OTHER_ACCOUNT = "444455556666"
ACCESS_KEY = "AK" + "IA" + "Z7QW4ERTY6UIOP2A"
TEMP_KEY = "AS" + "IA" + "Z7QW4ERTY6UIOP2B"
KEY_ALIAS = "AKIA********OP2A"  # ACCESS_KEY의 가명 (키 종류와 끝 네 자리)
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYzEXAMPLEKE"  # 40자
ANTHROPIC_KEY = "sk-" + "ant-api03-" + "a" * 90
SLACK_TOKEN = "xo" + "xb-1234567890-0987654321-" + "AbCdEfGhIjKlMnOp"
SLACK_WEBHOOK = "https://hooks.slack.com/" + "services/T000/B000/XXXXXXXX"
GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
JWT = "eyJhbGciOiJSUzI1NiJ9" + ".eyJzdWIiOiJhbGljZSJ9" + ".c2lnbmF0dXJlLXZhbHVl"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA " + "PRIVATE KEY-----"


@pytest.fixture
def redaction(aws):
    load_service_module("services/llm", "llm_service")
    import redaction
    return redaction


# ---------------------------------------------------------------- 비밀 값

@pytest.mark.parametrize("secret, kind", [
    (ANTHROPIC_KEY, "anthropic_api_key"),
    (SLACK_TOKEN, "slack_token"),
    (SLACK_WEBHOOK, "slack_webhook"),
    (GITHUB_TOKEN, "github_token"),
    (JWT, "jwt"),
    (PRIVATE_KEY, "private_key"),
])
def test_secrets_are_replaced_and_not_restored(redaction, secret, kind):
    redactor = redaction.Redactor()
    masked = redactor.text(f"값: {secret} 끝")
    assert masked == f"값: [REDACTED:{kind}] 끝"
    assert redactor.restore(masked) == masked  # 비밀 값은 되돌리지 않는다
    assert redactor.counts == {kind: 1}


def test_secret_key_and_session_token_are_masked_only_after_their_names(redaction):
    redactor = redaction.Redactor()
    config = f'aws_secret_access_key = {SECRET_KEY}\n{{"SessionToken": "{"F" * 120}"}}'
    assert redactor.text(config) == ('aws_secret_access_key = [REDACTED:aws_secret_key]\n'
                                     '{"SessionToken": "[REDACTED:aws_session_token]"}')
    # 이름 없는 40자 값(커밋 해시 등)은 비밀 키인지 알 수 없어 가리지 않는다
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert redactor.text(f"commit {sha}") == f"commit {sha}"


def test_values_followed_by_korean_are_masked(redaction):
    # 한국어 조사가 값 바로 뒤에 붙는 경우 (유니코드 기준이면 한국어도 단어 글자라 경계가 없어 놓친다)
    redactor = redaction.Redactor([ACCOUNT])
    assert (redactor.text(f"키 {ACCESS_KEY}가 {ACCOUNT}에서 alice@example.com으로 보였다")
            == f"키 {KEY_ALIAS}가 ********3333에서 a***@example.com으로 보였다")


def test_password_in_connection_url_is_masked(redaction):
    redactor = redaction.Redactor()
    assert (redactor.text("DB_URL=postgres://vigie:p4ss-w0rd@db.internal:5432/app")
            == "DB_URL=postgres://vigie:[REDACTED:url_password]@db.internal:5432/app")
    # 포트만 있는 주소는 그대로
    assert redactor.text("https://example.com:8443/path") == "https://example.com:8443/path"


# ---------------------------------------------------------------- 계정 ID

def test_own_account_id_is_masked_anywhere(redaction):
    redactor = redaction.Redactor([ACCOUNT])
    bucket = f"s3://vigie-diagrambucket-{ACCOUNT}-dev/a.png"
    assert redactor.text(bucket) == "s3://vigie-diagrambucket-********3333-dev/a.png"


def test_other_account_ids_are_masked_only_in_known_places(redaction):
    redactor = redaction.Redactor([ACCOUNT])
    text = (f"arn:aws:iam::{OTHER_ACCOUNT}:role/Admin "
            f"{OTHER_ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com/app "
            f'"AccountId": "{OTHER_ACCOUNT}" account_id={OTHER_ACCOUNT} OwnerId {OTHER_ACCOUNT}')
    masked = redactor.text(text)
    assert OTHER_ACCOUNT not in masked
    assert masked.startswith("arn:aws:iam::********6666:role/Admin ********6666.dkr.ecr.")
    assert redactor.counts == {"account_id": 1}  # 같은 값은 한 번만 센다


def test_account_id_inside_json_encoded_tool_result_is_masked(redaction):
    # 도구 결과는 JSON 문자열 안에 또 JSON 문자열이 들어 있기도 하다 (\"AccountId\": \"…\")
    redactor = redaction.Redactor()
    inner = json.dumps({"AccountId": OTHER_ACCOUNT})
    outer = json.dumps({"content": [{"type": "text", "text": inner}]})
    assert OTHER_ACCOUNT not in redactor.text(outer)


@pytest.mark.parametrize("text", [
    "RequestId: 0f8fad5b-d9cb-469f-a165-708677289501",  # UUID의 마지막 12자리가 숫자뿐인 경우
    "UUID 11111111-2222-4333-8444-555555555555",
    "BytesScanned 123456789012",  # 12자리 숫자 자체
    "timestamp 1727000000000",  # 밀리초 시각 (13자리)
    "RoleId AROAZ7QW4ERTY6UIOP2AB",  # 역할 고유 ID는 비밀이 아니다
    "AKIA 형식을 설명하는 문서",
    "/aws/lambda/vigie-llm-dev 로그 그룹에서 오류 3건",
    "비용 $12.34, 호출 1,234,567회",
])
def test_ordinary_values_are_not_masked(redaction, text):
    redactor = redaction.Redactor([ACCOUNT])
    assert redactor.text(text) == text
    assert not redactor.counts


# ---------------------------------------------------------------- 가명과 되돌리기

def test_same_value_gets_same_alias_and_is_restored_for_tool_calls(redaction):
    redactor = redaction.Redactor([ACCOUNT])
    first = redactor.text(f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/x owner alice@example.com")
    second = redactor.text(f"{ACCOUNT} alice@example.com")
    assert first == "arn:aws:logs:us-east-1:********3333:log-group:/aws/lambda/x owner a***@example.com"
    assert second == "********3333 a***@example.com"

    tool_input = {"arn": "arn:aws:logs:us-east-1:********3333:log-group:/aws/lambda/x",
                  "users": ["a***@example.com"], "limit": 5}
    assert redactor.restore(tool_input) == {
        "arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/x",
        "users": ["alice@example.com"], "limit": 5}


def test_aliases_with_same_ending_stay_distinct(redaction):
    # 끝 네 자리가 같은 두 계정, 첫 글자·도메인이 같은 두 이메일
    redactor = redaction.Redactor()
    a, b = "999900003333", "888800003333"
    masked = redactor.text(f"arn:aws:iam::{a}:root arn:aws:iam::{b}:root ann@x.com amy@x.com")
    assert masked == "arn:aws:iam::********3333:root arn:aws:iam::********3333#2:root a***@x.com a***@x.com#2"
    assert redactor.restore(masked) == f"arn:aws:iam::{a}:root arn:aws:iam::{b}:root ann@x.com amy@x.com"
    # 가린 글자를 다시 가려도 바뀌지 않는다 (답변을 한 번 더 가릴 때)
    assert redactor.text(masked) == masked


def test_access_key_ids_get_aliases_and_are_restored_for_diagnosis(redaction):
    # 액세스 키 ID는 비밀이 아니라 키의 이름이다. "키 AKIA…가 유출됐대요"에 진단 도구가 그 키를 조회할 수 있어야 한다
    redactor = redaction.Redactor()
    masked = redactor.text(f"액세스 키 {ACCESS_KEY}가 유출됐대요. 임시 키 {TEMP_KEY}도 보여요")
    assert masked == f"액세스 키 {KEY_ALIAS}가 유출됐대요. 임시 키 ASIA********OP2B도 보여요"
    assert ACCESS_KEY not in masked and TEMP_KEY not in masked
    assert redactor.counts == {"aws_access_key_id": 2}
    # 모델이 가명으로 진단을 부르면 도구는 원래 키 ID를 받는다
    assert (redactor.restore({"service": "credential", "resource": KEY_ALIAS})
            == {"service": "credential", "resource": ACCESS_KEY})
    # 같은 키는 같은 가명, 이미 가린 글자는 다시 가려도 그대로
    assert redactor.text(f"AccessKeyId: {ACCESS_KEY}") == f"AccessKeyId: {KEY_ALIAS}"
    assert redactor.text(masked) == masked and redactor.counts == {"aws_access_key_id": 2}


def test_access_key_id_alias_does_not_collide_with_account_alias(redaction):
    # 끝 네 자리가 같은 계정 ID와 키 ID: 가명이 달라서 각자 제 값으로 돌아간다
    key = "AK" + "IA" + "Z7QW4ERTY6UI3333"
    redactor = redaction.Redactor([ACCOUNT])
    masked = redactor.text(f"{ACCOUNT} {key}")
    assert masked == "********3333 AKIA********3333"
    assert redactor.restore(masked) == f"{ACCOUNT} {key}"


def test_secret_key_is_still_masked_next_to_its_key_id(redaction):
    # 키 ID는 가명, 짝이 되는 비밀 액세스 키는 되돌릴 수 없게 지운다
    redactor = redaction.Redactor()
    masked = redactor.text(f"aws_access_key_id = {ACCESS_KEY}\naws_secret_access_key = {SECRET_KEY}")
    assert masked == f"aws_access_key_id = {KEY_ALIAS}\naws_secret_access_key = [REDACTED:aws_secret_key]"
    assert redactor.restore(masked) == f"aws_access_key_id = {ACCESS_KEY}\naws_secret_access_key = [REDACTED:aws_secret_key]"


def test_secrets_only_keeps_identifiers_for_the_audit_log(redaction):
    # 감사 로그는 "어느 키를 조회했나"를 추적해야 하므로 키 ID·계정 ID·이메일은 남기고 비밀 값만 지운다
    redactor = redaction.Redactor([ACCOUNT])
    text = f"{ACCOUNT} {ACCESS_KEY} alice@example.com aws_secret_access_key={SECRET_KEY}"
    assert (redactor.secrets_only(text)
            == f"{ACCOUNT} {ACCESS_KEY} alice@example.com aws_secret_access_key=[REDACTED:aws_secret_key]")
    assert not redactor.counts


def test_aliases_are_per_request(redaction):
    # 요청마다 새 Redactor를 쓴다: 다른 요청의 가명으로는 원래 값을 얻을 수 없다
    first = redaction.Redactor()
    first.text("alice@example.com")
    assert redaction.Redactor().restore("a***@example.com") == "a***@example.com"


def test_redact_walks_nested_values_without_changing_the_input(redaction):
    redactor = redaction.Redactor()
    value = {"steps": [{"error": f"Invalid key {ACCESS_KEY}", "ms": 12, "ok": False}]}
    original = copy.deepcopy(value)
    assert redactor.redact(value) == {"steps": [{"error": f"Invalid key {KEY_ALIAS}",
                                                 "ms": 12, "ok": False}]}
    assert value == original


# ---------------------------------------------------------------- 적용 위치: Claude로 나가는 요청

class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


THINKING = {"type": "thinking", "thinking": "로그 그룹을 찾는다.", "signature": "sig-1"}
ALIAS_ARN = "arn:aws:logs:us-east-1:********3333:log-group:/aws/lambda/vigie-llm-dev"


@pytest.fixture
def client_run(aws, monkeypatch):
    load_service_module("services/llm", "llm_service")
    import mcp_anthropic_client
    from llm_progress import ProgressReporter
    from redaction import Redactor

    sent, calls = [], []
    replies = [
        {"content": [THINKING, {"type": "tool_use", "id": "toolu_1", "name": "describe_log_groups",
                                "input": {"log_group_name_prefix": "/aws/lambda"}}], "usage": {}},
        # 모델은 가명이 든 ARN으로 다음 도구를 부른다
        {"content": [{"type": "tool_use", "id": "toolu_2", "name": "analyze_log_group",
                      "input": {"log_group_arn": ALIAS_ARN}}], "usage": {}},
        {"content": [{"type": "text", "text": f"{ALIAS_ARN} 로그 그룹에 오류가 없습니다."}], "usage": {}},
    ]

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse(replies[len(sent) - 1])

    def fake_call_tool(name, args):
        calls.append((name, args))
        if name == "describe_log_groups":
            return {"content": [{"type": "text", "text": json.dumps({"logGroups": [{
                "arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/vigie-llm-dev",
                "env": f"AWS_ACCESS_KEY_ID={ACCESS_KEY} AWS_SECRET_ACCESS_KEY={SECRET_KEY}"}]})}]}
        return {"isError": True, "content": [{"type": "text", "text": f"AccessDenied for {ACCESS_KEY}"}]}

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(
        mcp_url="https://example.invalid", api_key="k", model_id="claude-sonnet-5",
        thinking={"type": "adaptive", "display": "summarized"})
    read = {"vigie/risk": "read", "vigie/access": "all"}  # MCP tools/list가 붙이는 위험도·쓸 수 있는 사람
    client.tools = [{"name": "describe_log_groups", "description": "", "inputSchema": {}, "_meta": read},
                    {"name": "analyze_log_group", "description": "", "inputSchema": {}, "_meta": read}]
    monkeypatch.setattr(client.mcp_client, "call_tool", fake_call_tool)
    client.progress = ProgressReporter()
    client.redactor = Redactor([ACCOUNT])
    history = [{"role": "user", "content": f"내 키는 {ACCESS_KEY}"},
               {"role": "assistant", "content": [THINKING, {"type": "text", "text": "확인했습니다."}]}]
    answer = client.process_user_input_with_history(f"{ACCOUNT} 계정 로그 봐줘", "system", history)
    return answer, sent, calls, client


def test_nothing_sensitive_is_sent_to_claude(client_run):
    _, sent, _, _ = client_run
    for payload in sent:
        body = json.dumps(payload, ensure_ascii=False)
        assert ACCOUNT not in body and ACCESS_KEY not in body and SECRET_KEY not in body
    last = sent[-1]["messages"]
    # 질문과 이전 대화도 가린다
    assert last[0]["content"] == f"내 키는 {KEY_ALIAS}"
    assert last[2]["content"] == "********3333 계정 로그 봐줘"
    # 도구 결과: 계정 ID·액세스 키 ID는 가명, 비밀 키는 [REDACTED]
    tool_result = last[4]["content"][0]["content"]
    assert ALIAS_ARN in tool_result and KEY_ALIAS in tool_result
    assert "[REDACTED:aws_secret_key]" in tool_result


def test_thinking_blocks_in_history_are_sent_unchanged(client_run):
    _, sent, _, _ = client_run
    assert sent[0]["messages"][1]["content"][0] == THINKING


def test_tool_gets_the_original_value_behind_an_alias(client_run):
    _, _, calls, _ = client_run
    assert calls[1] == ("analyze_log_group",
                        {"log_group_arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/vigie-llm-dev"})


def test_progress_does_not_keep_sensitive_values(client_run):
    _, _, _, client = client_run
    steps = json.dumps(client.progress.steps, ensure_ascii=False)
    assert ACCOUNT not in steps and ACCESS_KEY not in steps
    assert client.progress.steps[-1]["error"] == f"AccessDenied for {KEY_ALIAS}"


# ---------------------------------------------------------------- 적용 위치: 최종 답변과 추론 데이터

class FakeClient:
    """모델을 거치지 않고 들어온 값(도구 오류)과 답변에 민감정보가 섞인 경우."""
    progress = None
    redactor = None

    def process_user_input(self, text, system_prompt):
        self.progress.tool_started("toolu_1", "get_active_alarms", {})
        self.progress.tool_finished("toolu_1", False, f"AccessDenied for {ACCESS_KEY}")
        return f"계정 {ACCOUNT}에서 키 {ACCESS_KEY}가 보입니다."

    def get_debug_log(self):
        return [{"type": "tool_error", "tool_name": "get_active_alarms", "input": {},
                 "error": f"AccessDenied for {ACCESS_KEY}"}]


def test_llm1_answer_and_inference_are_redacted(aws, monkeypatch):
    llm = load_service_module("services/llm", "llm_service")
    monkeypatch.setattr(llm, "ACCOUNT_ID", ACCOUNT)
    fake = FakeClient()
    monkeypatch.setattr(llm, "get_client", lambda *_: fake)

    response = llm.handle_llm1_with_mcp({"text": "알람 알려줘"}, "https://test.abc.amplifyapp.com", caller_id="alice")
    result = json.loads(response["body"])

    assert result["answer"] == f"계정 ********3333에서 키 {KEY_ALIAS}가 보입니다."
    inference = json.dumps(result["inference"], ensure_ascii=False)
    assert ACCOUNT not in inference and ACCESS_KEY not in inference
    assert result["inference"]["tools_used"][0]["error"] == f"AccessDenied for {KEY_ALIAS}"
    assert result["inference"]["redacted"] == {"aws_access_key_id": 1, "account_id": 1}
    # 요청마다 새 Redactor를 클라이언트에 넣는다
    assert fake.redactor is not None and fake.redactor.counts == result["inference"]["redacted"]


def test_llm_lambda_knows_its_account_id():
    # CloudFormation이 ACCOUNT_ID를 넣어야 이 계정 ID를 문맥 없이 가릴 수 있다
    from conftest import ROOT
    assert "ACCOUNT_ID: !Ref AWS::AccountId" in (ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8")
