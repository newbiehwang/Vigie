"""setup: 할당량 요청과 SSM 파라미터 등록. 멱등성(두 번째 실행은 skipped)과 비밀 값 누출을 중점적으로 본다"""
import json

import pytest

from vigie_installer.steps.setup import mask_secret

from .helpers import events, run_cli

QUOTA_CODE = "L-E5AE38E3"
PREFIX = "/vigie/dev"
SECRETS = {"ANTHROPIC_API_KEY": "sk-ant-api03-SECRETVALUE", "SlackbotToken": "xoxb-SECRET-TOKEN",
           "SlackSigningSecret": "abcdef0123456789SIGNING"}


def quotas(value, adjustable=True):
    return json.dumps({"Quotas": [
        {"QuotaName": "Maximum number of resources", "QuotaCode": "L-OTHER", "Value": 300.0},
        {"QuotaName": "Maximum integration timeout in milliseconds", "QuotaCode": QUOTA_CODE,
         "Value": float(value), "Adjustable": adjustable}]})


def history(*statuses):
    return json.dumps({"RequestedQuotas": [
        {"Status": status, "DesiredValue": 120000.0, "Created": f"2026-09-{10 + i:02d}T00:00:00+00:00"}
        for i, status in enumerate(statuses)]})


def params(**types):
    return json.dumps({"Parameters": [{"Name": f"{PREFIX}/{key}", "Type": t} for key, t in types.items()]})


def account(fake, *, quota=29000, adjustable=True, requests=(), existing=None):
    """setup이 보는 AWS 상태를 흉내 낸다."""
    fake.add("aws", "list-service-quotas", quotas(quota, adjustable))
    fake.add("aws", "list-requested-service-quota-change-history-by-quota", history(*requests))
    fake.add("aws", "request-service-quota-increase", json.dumps({"RequestedQuota": {"Status": "PENDING"}}))
    fake.add("aws", "describe-parameters", params(**(existing or {})))
    fake.add("aws", "put-parameter", json.dumps({"Version": 1, "Tier": "Standard"}))


def confirm(id_, approved=True):
    return json.dumps({"type": "confirm_response", "id": id_, "approved": approved})


def secret(key, value):
    return json.dumps({"type": "secret_response", "id": f"secret_{key}", "value": value})


def choice(key, value):
    return json.dumps({"type": "choice_response", "id": f"existing_{key}", "choice": value})


def setup_json(fake, *responses, extra=()):
    return run_cli(fake, "setup", "--json", "--region", "ap-northeast-2", *extra,
                   input="\n".join(responses) + ("\n" if responses else ""))


def mutating_calls(fake):
    return [c["args"] for c in fake.calls("aws")
            if any(verb in " ".join(c["args"]) for verb in ("request-service-quota-increase", "put-parameter"))]


def finished(stdout):
    return {e["step"]: e for e in events(stdout) if e["type"] == "step_finished"}


FIRST_RUN = [confirm("request_quota"),
             secret("ANTHROPIC_API_KEY", SECRETS["ANTHROPIC_API_KEY"]), confirm("put_ANTHROPIC_API_KEY"),
             secret("SlackbotToken", SECRETS["SlackbotToken"]), confirm("put_SlackbotToken"),
             secret("SlackSigningSecret", SECRETS["SlackSigningSecret"]), confirm("put_SlackSigningSecret")]


def test_first_run_requests_quota_and_stores_secrets(fake):
    account(fake)
    result = setup_json(fake, *FIRST_RUN)
    assert result.returncode == 0, result.stdout + result.stderr
    evts = events(result.stdout)
    # 명령 전체의 시작·끝도 알린다 (읽는 쪽이 제목과 결과 요약을 보여 주는 데 쓴다)
    assert evts[0] == {"type": "step_started", "step": "setup", "title": "사전 설정 (dev, ap-northeast-2)"}
    assert evts[-1]["type"] == "step_finished" and evts[-1]["step"] == "setup" and evts[-1]["status"] == "ok"
    steps = finished(result.stdout)
    assert steps["quota"]["status"] == "ok" and "PENDING" in steps["quota"]["summary"]
    assert steps["ssm_parameters"] == {"type": "step_finished", "step": "ssm_parameters", "status": "ok",
                                       "summary": "3개 등록"}   # 0개(유지)는 말하지 않는다

    request = [c for c in mutating_calls(fake) if "request-service-quota-increase" in c]
    assert request == [["service-quotas", "request-service-quota-increase", "--service-code", "apigateway",
                        "--quota-code", QUOTA_CODE, "--desired-value", "120000", "--output", "json"]]

    stored = [json.loads(content) for _, content in fake.captures()]
    assert [(s["Name"], s["Type"], s["Overwrite"]) for s in stored] == [
        (f"{PREFIX}/{key}", "SecureString", False) for key in SECRETS]
    assert [s["Value"] for s in stored] == list(SECRETS.values())


def test_secrets_only_travel_in_private_temp_files(fake):
    account(fake)
    result = setup_json(fake, *FIRST_RUN)
    # 1) 화면·로그(이벤트)에 비밀 값이 없다
    for value in SECRETS.values():
        assert value not in result.stdout and value not in result.stderr
    # 2) 명령 인자로 넘기지 않는다 (ps로 보이므로)
    for call in fake.calls():
        assert not any(value in arg for arg in call["args"] for value in SECRETS.values())
    # 3) 파일은 소유자만 읽을 수 있었고, 끝난 뒤 남지 않았다
    assert [mode for mode, _ in fake.captures()] == ["-rw-------"] * 3
    assert list(fake.tmp.iterdir()) == []


def test_second_run_changes_nothing(fake):
    # 멱등성: 할당량이 이미 충분하고 파라미터가 모두 있으면 아무것도 바꾸지 않는다 (응답이 없으면 기본값 "유지")
    account(fake, quota=120000, existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake)
    steps = finished(result.stdout)
    assert result.returncode == 0
    # 이미 되어 있으면 "할 일을 마친 상태"라 ok이고, 요약 없이 [완료]로만 보인다
    assert steps["quota"]["status"] == steps["ssm_parameters"]["status"] == "ok"
    assert steps["quota"]["summary"] == steps["ssm_parameters"]["summary"] == ""
    assert mutating_calls(fake) == []
    assert [e["type"] for e in events(result.stdout)].count("choice_required") == 3


def test_previously_raised_higher_quota_is_enough(fake):
    # 예전에 180000ms로 올려 둔 계정은 다시 요청하지 않는다
    account(fake, quota=180000, existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake)
    assert finished(result.stdout)["quota"] == {"type": "step_finished", "step": "quota", "status": "ok",
                                                "summary": ""}
    assert mutating_calls(fake) == []


def test_pending_request_is_not_repeated(fake):
    account(fake, requests=("CASE_OPENED",), existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake)
    quota = finished(result.stdout)["quota"]
    assert quota["status"] == "ok" and quota["summary"] == "승인 대기 중 (CASE_OPENED) — 승인되면 deploy를 실행하세요"
    assert mutating_calls(fake) == []


def test_rejected_request_is_retried(fake):
    account(fake, requests=("DENIED",), existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake, confirm("request_quota"))
    assert any(e["type"] == "log" and "거절" in e["line"] for e in events(result.stdout))
    assert len(mutating_calls(fake)) == 1


def test_unadjustable_quota_fails_but_ssm_still_runs(fake):
    account(fake, adjustable=False, existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake)
    steps = finished(result.stdout)
    assert result.returncode == 1
    assert steps["quota"]["status"] == "failed" and steps["ssm_parameters"]["status"] == "ok"
    assert steps["setup"]["status"] == "failed"


def test_quota_lookup_error_is_reported(fake):
    fake.add("aws", "list-service-quotas", stderr="An error occurred (AccessDeniedException): not authorized\n",
             exit=254)
    account(fake, existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake)
    errors = [e for e in events(result.stdout) if e["type"] == "error"]
    # 무엇이 실패했는지(message)와 AWS가 낸 오류 원문(raw)을 나눠 보낸다
    assert result.returncode == 1 and errors[0]["message"] == "할당량을 조회하지 못했습니다"
    assert "AccessDeniedException" in errors[0]["raw"]


def test_quota_found_in_default_list(fake):
    # 조정한 적 없는 할당량은 적용 값 목록에 없을 수 있다 → AWS 기본값 목록에서 찾는다
    fake.add("aws", "list-service-quotas", json.dumps({"Quotas": []}))
    fake.add("aws", "list-aws-default-service-quotas", quotas(29000))
    account(fake, existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake, confirm("request_quota"))
    assert finished(result.stdout)["quota"]["status"] == "ok"


def test_overwrite_existing_parameter(fake):
    account(fake, quota=120000, existing={key: "SecureString" for key in SECRETS})
    result = setup_json(fake, choice("ANTHROPIC_API_KEY", "overwrite"),
                        secret("ANTHROPIC_API_KEY", "sk-ant-NEW"), confirm("put_ANTHROPIC_API_KEY"),
                        choice("SlackbotToken", "keep"), choice("SlackSigningSecret", "keep"))
    stored = [json.loads(content) for _, content in fake.captures()]
    assert [(s["Name"], s["Value"], s["Overwrite"]) for s in stored] == [
        (f"{PREFIX}/ANTHROPIC_API_KEY", "sk-ant-NEW", True)]
    assert finished(result.stdout)["ssm_parameters"]["summary"] == "1개 등록"


def test_plain_string_parameter_is_flagged(fake):
    account(fake, quota=120000, existing={"ANTHROPIC_API_KEY": "String", "SlackbotToken": "SecureString",
                                          "SlackSigningSecret": "SecureString"})
    result = setup_json(fake)
    assert any(e["type"] == "log" and "String 형식" in e["line"] for e in events(result.stdout))


def test_value_is_trimmed(fake):
    # 복사해 붙여 넣을 때 딸려 온 공백·줄바꿈은 지우고 저장한다
    account(fake, quota=120000, existing={"SlackbotToken": "SecureString", "SlackSigningSecret": "SecureString"})
    setup_json(fake, secret("ANTHROPIC_API_KEY", "  sk-ant-TRIM \n"), confirm("put_ANTHROPIC_API_KEY"))
    assert json.loads(fake.captures()[0][1])["Value"] == "sk-ant-TRIM"


def test_empty_slack_values_are_skipped(fake):
    account(fake, quota=120000, existing={"ANTHROPIC_API_KEY": "SecureString"})
    result = setup_json(fake, choice("ANTHROPIC_API_KEY", "keep"),
                        secret("SlackbotToken", ""), secret("SlackSigningSecret", ""))
    assert result.returncode == 0
    assert mutating_calls(fake) == []
    assert finished(result.stdout)["ssm_parameters"]["summary"] == "2개 건너뜀"


def test_empty_anthropic_key_fails(fake):
    account(fake, quota=120000, existing={"SlackbotToken": "SecureString", "SlackSigningSecret": "SecureString"})
    result = setup_json(fake, secret("ANTHROPIC_API_KEY", ""))
    assert result.returncode == 1 and mutating_calls(fake) == []
    assert finished(result.stdout)["ssm_parameters"]["status"] == "failed"


def test_declined_put_is_not_executed(fake):
    account(fake, quota=120000, existing={"SlackbotToken": "SecureString", "SlackSigningSecret": "SecureString"})
    result = setup_json(fake, secret("ANTHROPIC_API_KEY", "sk-ant-x"), confirm("put_ANTHROPIC_API_KEY", False))
    assert mutating_calls(fake) == [] and list(fake.tmp.iterdir()) == []
    steps = finished(result.stdout)
    # 필수 값(API 키)을 저장하지 않기로 했다: 이대로는 배포할 수 없으므로 전체도 "완료"라 하지 않는다
    assert steps["ssm_parameters"]["status"] == "skipped"
    assert steps["ssm_parameters"]["summary"] == "1개 건너뜀 — API 키가 없으면 배포할 수 없습니다"
    assert steps["setup"]["status"] == "skipped" and "아직 배포할 수 없습니다" in steps["setup"]["summary"]


def test_put_failure_is_reported(fake):
    fake.add("aws", "put-parameter", stderr="An error occurred (AccessDeniedException) when calling the "
                                            "PutParameter operation\n", exit=254)
    account(fake, quota=120000, existing={"SlackbotToken": "SecureString", "SlackSigningSecret": "SecureString"})
    result = setup_json(fake, secret("ANTHROPIC_API_KEY", "sk-ant-x"), confirm("put_ANTHROPIC_API_KEY"))
    assert result.returncode == 1
    assert finished(result.stdout)["ssm_parameters"]["status"] == "failed"


def test_dry_run_asks_no_secrets_and_changes_nothing(fake):
    account(fake)
    result = setup_json(fake, extra=("--dry-run",))
    kinds = [e["type"] for e in events(result.stdout)]
    assert result.returncode == 0
    assert "input_required" not in kinds and "confirm_required" not in kinds
    assert kinds.count("dry_run") == 4   # 할당량 요청 1 + 파라미터 3
    assert mutating_calls(fake) == []


@pytest.mark.parametrize("answer, expected_calls", [("y\n", 1), ("n\n", 0)])
def test_text_mode(fake, answer, expected_calls):
    # 터미널에서는 y/N 질문, 번호 선택, 보이지 않는 입력(파이프일 때는 한 줄 읽기)을 쓴다
    account(fake, quota=120000, existing={"SlackbotToken": "SecureString", "SlackSigningSecret": "SecureString"})
    result = run_cli(fake, "setup", "--region", "ap-northeast-2", input=f"sk-ant-TEXT\n{answer}\n\n")
    assert "[y/N]" in result.stdout and "번호를 입력하세요" in result.stdout
    # API 키는 보이게 입력받으므로 설명 문구도, 입력 뒤 확인 줄도 붙이지 않는다 (aws configure처럼)
    assert "    Anthropic API 키: " in result.stdout
    assert "입력 내용이 화면에 보입니다" not in result.stdout and "입력됨" not in result.stdout
    assert "sk-ant-TEXT" not in result.stdout
    assert len(mutating_calls(fake)) == expected_calls


def test_hidden_secret_is_confirmed_with_a_masked_preview(fake):
    # 보이지 않게 입력한 값(Slack)은 들어갔는지 알 수 있게 앞 7글자·끝 4글자·길이만 보여 준다.
    # 보이게 입력한 API 키는 확인할 필요가 없어 보여 주지 않는다
    account(fake)
    result = setup_json(fake, *FIRST_RUN)
    logs = [e["line"] for e in events(result.stdout) if e["type"] == "log"]
    assert [line for line in logs if line.startswith("입력됨")] == ["입력됨: (17자)", "입력됨: abcdef0…NING (23자)"]
    for value in SECRETS.values():
        assert value not in result.stdout


@pytest.mark.parametrize("value, shown", [
    ("sk-ant-api03-" + "x" * 90 + "ABCD", "sk-ant-…ABCD (107자)"),
    ("short-secret", "(12자)"),   # 짧으면 일부만 보여도 추측하기 쉬워 길이만
])
def test_mask_secret(value, shown):
    assert mask_secret(value) == shown


# ---------------------------------------------------------------- 저장소 루트 .env의 Anthropic API 키

DOTENV_KEY = "sk-ant-api03-FROM-DOTENV-abcdefghijklmnop"


def with_dotenv(repo, text):
    (repo / ".env").write_text(text)
    return ("--repo", str(repo))


def prompts(stdout):
    return [e["id"] for e in events(stdout) if e["type"] in ("input_required", "choice_required")]


def test_key_in_dotenv_is_stored_without_asking(fake, repo):
    # .env에 키가 있으면 묻지 않고 그 값으로 등록한다. 등록 전 승인은 그대로 받는다
    account(fake, quota=120000, existing={"SlackbotToken": "SecureString", "SlackSigningSecret": "SecureString"})
    extra = with_dotenv(repo, f'# 직접 적는 값\nANTHROPIC_API_KEY="{DOTENV_KEY}"\nVITE_API_DEST=https://x\n')
    result = setup_json(fake, confirm("put_ANTHROPIC_API_KEY"), extra=extra)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "secret_ANTHROPIC_API_KEY" not in prompts(result.stdout)
    assert json.loads(fake.captures()[0][1]) == {"Name": f"{PREFIX}/ANTHROPIC_API_KEY", "Value": DOTENV_KEY,
                                                 "Type": "SecureString", "Overwrite": False}
    # 키는 출력에 나오지 않는다 (앞뒤 몇 글자와 길이만)
    assert DOTENV_KEY not in result.stdout + result.stderr
    assert any("저장소 루트 .env" in e.get("line", "") and mask_secret(DOTENV_KEY) in e["line"]
               for e in events(result.stdout))
    assert finished(result.stdout)["ssm_parameters"]["status"] == "ok"


def test_key_in_dotenv_keeps_existing_parameter_without_asking(fake, repo):
    # SSM에 이미 있으면 덮어쓸지 묻지 않고 그대로 둔다 (배포할 때 deploy.sh가 .env 값으로 맞춘다)
    account(fake, quota=120000, existing={"ANTHROPIC_API_KEY": "SecureString", "SlackbotToken": "SecureString",
                                          "SlackSigningSecret": "SecureString"})
    result = setup_json(fake, extra=with_dotenv(repo, f"ANTHROPIC_API_KEY={DOTENV_KEY}\n"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "existing_ANTHROPIC_API_KEY" not in prompts(result.stdout)
    assert not any("put-parameter" in " ".join(call) for call in mutating_calls(fake))
    assert any("deploy.sh가" in e.get("line", "") for e in events(result.stdout))


def test_empty_key_in_dotenv_still_asks(fake, repo):
    account(fake, quota=120000, existing={"SlackbotToken": "SecureString", "SlackSigningSecret": "SecureString"})
    result = setup_json(fake, secret("ANTHROPIC_API_KEY", "sk-ant-TYPED"), confirm("put_ANTHROPIC_API_KEY"),
                        extra=with_dotenv(repo, "ANTHROPIC_API_KEY=\n"))
    assert "secret_ANTHROPIC_API_KEY" in prompts(result.stdout)
    assert json.loads(fake.captures()[0][1])["Value"] == "sk-ant-TYPED"
