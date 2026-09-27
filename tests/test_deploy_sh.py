"""deploy.sh 검증: AWS 없이 가짜 aws CLI로 스택 업데이트 처리와 배포 산출물 키를 확인한다"""
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from conftest import ROOT

DEPLOY_SH = (ROOT / "deploy.sh").read_text()


def extract_function(name):
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", DEPLOY_SH, re.S | re.M)
    assert match, f"{name} 함수가 deploy.sh에 없습니다"
    return match.group(0)


@pytest.fixture
def fake_aws(tmp_path):
    """update-stack 동작을 스택 이름으로 바꿀 수 있는 가짜 aws CLI. 호출 내역을 calls.log에 남긴다"""
    log = tmp_path / "calls.log"
    script = tmp_path / "aws"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$*" >> "{log}"
        case "$*" in
          *"update-stack --stack-name noop-stack"*)
            echo "An error occurred (ValidationError) when calling the UpdateStack operation: No updates are to be performed." >&2; exit 254 ;;
          *"update-stack --stack-name broken-stack"*)
            echo "An error occurred (ValidationError) when calling the UpdateStack operation: Template format error" >&2; exit 254 ;;
          *"update-stack"*) echo '{{"StackId": "arn:aws:cloudformation:::stack/x"}}' ;;
          *"wait stack-update-complete"*) exit 0 ;;
        esac
        """))
    script.chmod(0o755)
    return tmp_path, log


def run_cfn_update(fake_aws, stack):
    bin_dir, log = fake_aws
    script = "set -e\n" + extract_function("cfn_update") + f'cfn_update {stack} --template-url x\necho "계속 진행"\n'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_cfn_update_waits_after_real_update(fake_aws):
    result, calls = run_cfn_update(fake_aws, "real-stack")
    assert result.returncode == 0 and "계속 진행" in result.stdout
    assert calls[-1] == "cloudformation wait stack-update-complete --stack-name real-stack"


def test_cfn_update_treats_no_changes_as_success(fake_aws):
    # set -e 환경에서도 "변경 없음"으로 배포가 중단되지 않아야 한다
    result, calls = run_cfn_update(fake_aws, "noop-stack")
    assert result.returncode == 0 and "계속 진행" in result.stdout
    assert "변경 사항 없음" in result.stdout
    assert not any("wait" in c for c in calls)   # 존재하지 않는 업데이트를 기다리지 않음


def test_cfn_update_fails_on_other_errors(fake_aws):
    result, _ = run_cfn_update(fake_aws, "broken-stack")
    assert result.returncode != 0 and "계속 진행" not in result.stdout
    assert "Template format error" in result.stderr


def test_deploy_does_not_empty_buckets():
    # 모든 버킷이 DeletionPolicy: Retain이라 비울 필요가 없고, 비우면 사용자 데이터가 사라진다
    assert "aws s3 rm" not in DEPLOY_SH
    for template in (ROOT / "cloudformation").glob("*.yaml"):
        text = template.read_text()
        for block in re.split(r"\n(?=  \w+:\n)", text):
            if "Type: AWS::S3::Bucket\n" in block:
                assert "DeletionPolicy: Retain" in block, f"{template.name}에 Retain이 없는 버킷이 있습니다"


def test_base_stack_updates_keep_previous_domain_values():
    # 기존 스택 업데이트에서 placeholder 도메인으로 되돌리지 않는다
    update_calls = re.findall(r"cfn_update \$BASE_STACK_NAME.*?--capabilities", DEPLOY_SH, re.S)
    assert len(update_calls) == 3
    for call in update_calls:
        assert "placeholder" not in call
        assert "McpFunctionUrl" in call


def test_uploaded_lambda_keys_match_template_keys():
    # deploy.sh가 올리는 S3 키와 템플릿이 참조하는 키가 어긋나면 배포가 실패한다
    uploaded = {m.replace("$ENV", "{env}").replace("$CODE_VERSION", "{version}")
                for m in re.findall(r'"s3://\$DEPLOYMENT_BUCKET/([^"]+)"', DEPLOY_SH)}
    referenced = set()
    for template in (ROOT / "cloudformation").glob("*.yaml"):
        for key in re.findall(r"S3Key: !Sub ['\"]([^'\"]+)['\"]", template.read_text()):
            referenced.add(key.replace("${Environment}", "{env}").replace("${CodeVersion}", "{version}"))
    assert referenced, "템플릿에서 S3Key를 찾지 못했습니다"
    assert referenced == uploaded
    assert all("{version}" in key for key in referenced)   # 버전이 없으면 코드 변경이 반영되지 않음


def test_mcp_image_uses_code_version_tag():
    assert 'ECR_IMAGE_TAG="$CODE_VERSION"' in DEPLOY_SH
    assert "name=MCP_ECR_IMAGE_TAG,value=$CODE_VERSION" in DEPLOY_SH


def test_stack_parameters_do_not_reference_secure_strings():
    # CloudFormation 템플릿 파라미터(AWS::SSM::Parameter::Value<String>)는 SecureString을 지원하지 않으므로,
    # README가 SecureString으로 만들라고 안내하는 비밀 값을 스택 파라미터로 넘기면 스택 생성이 실패한다.
    readme = (ROOT / "README.md").read_text()
    secure_names = set(re.findall(r'put-parameter --name "/vigie/\$\{Environment\}/([^"]+)"[^\n]*--type "SecureString"', readme))
    assert {"SlackbotToken", "SlackSigningSecret", "ANTHROPIC_API_KEY"} <= secure_names

    passed_ssm_paths = set(re.findall(r'ParameterValue="\$SSM_PATH_PREFIX/([^"]+)"', DEPLOY_SH))
    assert passed_ssm_paths, "deploy.sh에서 SSM 경로 파라미터를 찾지 못했습니다"
    assert not (passed_ssm_paths & secure_names), f"SecureString을 스택 파라미터로 전달: {passed_ssm_paths & secure_names}"


def test_region_is_not_hardcoded():
    # 배포 리전을 바꿔도 동작하도록 코드와 템플릿에 특정 리전·전역 S3 엔드포인트를 고정하지 않는다.
    # (전역 엔드포인트 s3.amazonaws.com은 us-east-1 외 리전 버킷의 템플릿 URL에서 실패할 수 있다)
    targets = [ROOT / "deploy.sh", *(ROOT / "cloudformation").glob("*.yaml"),
               *(ROOT / "frontend" / "src").rglob("*.ts"), *(ROOT / "frontend" / "src").rglob("*.tsx"),
               *(ROOT / "layers").rglob("*.py"), *(ROOT / "services").rglob("*.py"), *(ROOT / "mcp").rglob("*.py")]
    offenders = []
    for path in targets:
        text = path.read_text()
        for pattern in ("us-east-1", "https://s3.amazonaws.com/"):
            if pattern in text:
                offenders.append(f"{path.relative_to(ROOT)}: {pattern}")
    assert not offenders, offenders


def test_default_region_is_seoul():
    assert "REGION=${REGION:-ap-northeast-2}" in DEPLOY_SH
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "vars.AWS_REGION || 'ap-northeast-2'" in workflow


# ---------------------------------------------------------------- 배포 직후 MCP Lambda 미리 깨우기 (warm_up_mcp)

FAKE_LAMBDA = r'''#!/usr/bin/env python3
# 가짜 aws CLI: lambda invoke를 MCP Lambda처럼 흉내 낸다. MODE로 동작을 바꾸고, 부른 내용을 calls.log에 남긴다
import json, os, sys

args = sys.argv[1:]
with open(os.environ["CALLS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
mode = os.environ["MODE"]
if args[:2] == ["lambda", "wait"]:
    sys.exit(0)
if args[:2] != ["lambda", "invoke"]:
    sys.exit(0)
if mode == "cli-error":
    print("Could not connect to the endpoint URL", file=sys.stderr)
    sys.exit(255)

event = json.loads(args[args.index("--payload") + 1])
if mode == "function-error":  # Lambda 안에서 예외가 나도 aws CLI는 0으로 끝나고 결과 파일에 오류가 들어간다
    response = {"errorMessage": "Read-only file system", "errorType": "OSError"}
elif event["httpMethod"] == "DELETE":
    response = {"statusCode": 204}
else:
    method = json.loads(event["body"])["method"]
    if method == "initialize":
        response = {"statusCode": 200, "headers": {"MCP-Session-Id": "session-1"}, "body": "{}"}
    else:
        assert event["headers"]["mcp-session-id"] == "session-1"
        tools = [{"name": "describe_log_groups"}, {"name": "cost-explorer"}]
        response = {"statusCode": 200, "body": json.dumps({"result": {"tools": tools}})}
with open(args[-1], "w") as out:
    json.dump(response, out)
'''


def run_warm_up(tmp_path, mode):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(FAKE_LAMBDA)
    (bin_dir / "aws").chmod(0o755)
    log = tmp_path / "calls.log"
    script = "set -e\n" + extract_function("warm_up_mcp") + 'warm_up_mcp vigie-mcp-test\necho "계속 진행"\n'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS_LOG": str(log), "MODE": mode})
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def invoked(calls):
    """lambda invoke로 보낸 이벤트 (HTTP 메서드, JSON-RPC 메서드) 순서"""
    events = [json.loads(c[c.index("--payload") + 1]) for c in calls if c[:2] == ["lambda", "invoke"]]
    return [(e["httpMethod"], json.loads(e["body"])["method"] if e["httpMethod"] == "POST" else None) for e in events]


def test_warm_up_mcp_loads_tools_like_llm_lambda(tmp_path):
    result, calls = run_warm_up(tmp_path, "ok")
    assert result.returncode == 0 and "계속 진행" in result.stdout
    assert "MCP Lambda 준비 완료: 도구 2개" in result.stdout
    # 이미지가 바뀐 뒤 새 버전을 부르도록 먼저 기다린다
    assert calls[0][:3] == ["lambda", "wait", "function-updated-v2"] and "vigie-mcp-test" in calls[0]
    # LLM Lambda와 같은 순서로 부르고(tools/list에서 공식 서버를 불러온다), 만든 세션은 지운다
    assert invoked(calls) == [("POST", "initialize"), ("POST", "tools/list"), ("DELETE", None)]


@pytest.mark.parametrize("mode", ["function-error", "cli-error"])
def test_warm_up_mcp_failure_does_not_stop_deploy(tmp_path, mode):
    # 미리 깨우지 못해도 첫 질문이 느릴 뿐이므로, set -e 환경에서도 배포를 멈추지 않는다
    result, calls = run_warm_up(tmp_path, mode)
    assert result.returncode == 0 and "계속 진행" in result.stdout
    assert "MCP Lambda를 미리 깨우지 못했습니다" in result.stdout
    assert invoked(calls) == [("POST", "initialize")]  # 초기화가 실패하면 더 부르지 않는다


def test_deploy_warms_up_mcp_after_last_stack_update():
    # MCP 이미지를 바꾸는 메인 스택과 마지막 base 스택 업데이트가 끝난 뒤에 깨워야 새 이미지가 깨어난다
    call = DEPLOY_SH.index('warm_up_mcp "vigie-mcp-$ENV"')
    assert DEPLOY_SH.rindex("cfn_update $BASE_STACK_NAME") < call < DEPLOY_SH.index("# 7. 배포 완료 요약")


# ---------------------------------------------------------------- 루트 .env (환경 값과 Anthropic API 키)

def run_function(name, body, env, cwd=None):
    script = "set -e\n" + extract_function(name) + body + '\necho "계속 진행"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=cwd)


def test_dotenv_set_keeps_other_lines(tmp_path):
    # deploy.sh는 프론트엔드 값만 바꾸고, 직접 적은 ANTHROPIC_API_KEY와 주석은 그대로 둔다
    env_file = tmp_path / ".env"
    env_file.write_text("# 직접 적는 값\nANTHROPIC_API_KEY=sk-ant-KEEP\nVITE_API_DEST=https://old\n")
    result = run_function("dotenv_set",
                          f'dotenv_set "{env_file}" VITE_API_DEST "https://new"\n'
                          f'dotenv_set "{env_file}" USER_POOL_ID "pool-1"',
                          {"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == ("# 직접 적는 값\nANTHROPIC_API_KEY=sk-ant-KEEP\n"
                                    "VITE_API_DEST=https://new\nUSER_POOL_ID=pool-1\n")


FAKE_SSM = r'''#!/usr/bin/env python3
# 가짜 aws CLI: SSM get-parameter / put-parameter만 흉내 낸다. 부른 인자는 calls.log, 올린 요청은 put.json에 남긴다
import json, os, sys
args = sys.argv[1:]
work = os.environ["WORK"]
with open(os.path.join(work, "calls.log"), "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["ssm", "get-parameter"]:
    current = os.environ.get("CURRENT")
    if not current:
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print(json.dumps({"Parameter": {"Name": args[args.index("--name") + 1], "Value": current,
                                    "Type": "SecureString"}}))
elif args[:2] == ["ssm", "put-parameter"]:
    path = args[args.index("--cli-input-json") + 1].removeprefix("file://")
    with open(path) as src, open(os.path.join(work, "put.json"), "w") as dst:
        dst.write(src.read())
    print(json.dumps({"Version": 2}))
'''

KEY = "sk-ant-api03-FROM-DOTENV"


def run_sync(tmp_path, env_text=None, current=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(FAKE_SSM)
    (bin_dir / "aws").chmod(0o755)
    env_file = tmp_path / ".env"
    if env_text is not None:
        env_file.write_text(env_text)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "WORK": str(tmp_path)}
    if current:
        env["CURRENT"] = current
    body = f'ROOT_ENV_FILE="{env_file}"\nSSM_PATH_PREFIX=/vigie/dev\nsync_anthropic_key'
    result = run_function("sync_anthropic_key", body, env)
    log = tmp_path / "calls.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    put = tmp_path / "put.json"
    return result, calls, (json.loads(put.read_text()) if put.exists() else None)


def test_anthropic_key_from_dotenv_is_put_to_ssm_without_command_args(tmp_path):
    result, calls, put = run_sync(tmp_path, f'# 주석\nANTHROPIC_API_KEY="{KEY}"\nVITE_API_DEST=https://x\n')
    assert result.returncode == 0 and "계속 진행" in result.stdout, result.stderr
    assert put == {"Name": "/vigie/dev/ANTHROPIC_API_KEY", "Value": KEY, "Type": "SecureString", "Overwrite": True}
    # 키 값은 명령 인자(ps로 보인다)에도, 화면 출력에도 나오지 않는다
    assert all(KEY not in arg for call in calls for arg in call)
    assert KEY not in result.stdout + result.stderr
    # 요청을 담았던 임시 파일은 지운다
    request = [c for c in calls if c[:2] == ["ssm", "put-parameter"]][0]
    assert not Path(request[request.index("--cli-input-json") + 1].removeprefix("file://")).exists()


def test_same_key_is_not_put_again(tmp_path):
    result, calls, put = run_sync(tmp_path, f"ANTHROPIC_API_KEY={KEY}\n", current=KEY)
    assert result.returncode == 0 and put is None
    assert "SSM 값과 같아" in result.stdout


@pytest.mark.parametrize("env_text", [None, "ANTHROPIC_API_KEY=\nVITE_API_DEST=https://x\n"])
def test_missing_key_keeps_ssm_value(tmp_path, env_text):
    # GitHub Actions처럼 .env가 없거나 키를 비워 두면 SSM에 있는 값을 그대로 쓴다
    result, _, put = run_sync(tmp_path, env_text)
    assert result.returncode == 0 and "계속 진행" in result.stdout and put is None


def test_deploy_syncs_key_before_stacks_and_fills_root_env():
    assert DEPLOY_SH.index("\nsync_anthropic_key\n") < DEPLOY_SH.index("# 1. CloudFormation 버킷 확인")
    # 프론트엔드 값은 루트 .env에만 쓰고, 예전 frontend/.env.local과 쓰지 않는 값은 만들지 않는다
    assert "frontend/.env.local" not in DEPLOY_SH
    for unused in ("API_DEST=", "VITE_API_URL", "COGNITO_REDIRECT_URI", "COGNITO_IDENTITY_POOL_ID"):
        assert f"dotenv_set \"$ROOT_ENV_FILE\" {unused}" not in DEPLOY_SH
    written = set(re.findall(r'dotenv_set "\$ROOT_ENV_FILE" (\w+)', DEPLOY_SH))
    example = set(re.findall(r"^(\w+)=", (ROOT / ".env.example").read_text(), re.M))
    assert written == {"VITE_API_DEST", "AWS_REGION", "USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_DOMAIN"}
    # 직접 적는 값: API 키와 (선택) 이메일 두 개. 나머지는 deploy.sh가 채운다
    assert example == written | {"ANTHROPIC_API_KEY", "ADMIN_EMAIL", "ALARM_EMAIL"}


def test_root_env_is_ignored_but_example_is_committed():
    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0
    assert ignored(".env")
    assert not ignored(".env.example")


def test_frontend_bundle_takes_only_chosen_env_values():
    # 루트 .env에 ANTHROPIC_API_KEY가 함께 있으므로, 번들에 넣는 값을 정해 둔 것만 넣어야 한다.
    # envPrefix를 바꾸면 그 접두사로 시작하는 값이 모두 번들에 들어간다
    config = (ROOT / "frontend" / "vite.config.ts").read_text()
    assert "envPrefix:" not in config  # 설정으로 쓰지 않는다 (주석의 설명은 괜찮다)
    assert "env.ANTHROPIC" not in config  # 비밀 값을 읽어 쓰지 않는다
    assert set(re.findall(r"'import\.meta\.env\.(\w+)'", config)) == {
        "AWS_REGION", "USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_DOMAIN"}


@pytest.mark.parametrize("text", [
    'ANTHROPIC_API_KEY="sk-ant-q"\n', "  ANTHROPIC_API_KEY = sk-ant-s  \n", "ANTHROPIC_API_KEY=a\nANTHROPIC_API_KEY=b\n",
    "# ANTHROPIC_API_KEY=commented\n", "ANTHROPIC_API_KEY=\n",
])
def test_installer_reads_dotenv_like_deploy_sh(tmp_path, text):
    # 설치 마법사(vigie_installer/dotenv.py)가 "키가 있다"고 보면 deploy.sh도 같은 값을 SSM에 올려야 한다.
    # 두 쪽이 다르게 읽으면 설치 마법사는 배포를 진행시켰는데 deploy.sh는 키를 건너뛰는 일이 생긴다
    from vigie_installer import dotenv

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".env").write_text(text)
    expected = dotenv.read_value(tmp_path / "repo", "ANTHROPIC_API_KEY")
    _, _, put = run_sync(tmp_path, text)
    assert (put["Value"] if put else None) == expected


# ---------------------------------------------------------------- 관리자 계정 (ADMIN_EMAIL)

POOL = "ap-northeast-2_POOL"
ADMIN = "admin@example.com"


def run_admin(tmp_path, email, *, get_user="exists", create="ok", group="ok"):
    """ensure_admin_account를 가짜 aws로 실행한다. get_user: exists | missing | denied"""
    log = tmp_path / "cognito.log"
    responses = {
        "exists": "echo '{}'",
        "missing": "echo 'An error occurred (UserNotFoundException) when calling the AdminGetUser operation: "
                   "User does not exist.' >&2; exit 254",
        "denied": "echo 'An error occurred (AccessDeniedException) when calling the AdminGetUser operation' >&2; "
                  "exit 254",
    }
    fail = "echo 'An error occurred (LimitExceededException)' >&2; exit 254"
    script = tmp_path / "aws"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$*" >> "{log}"
        case "$*" in
          *"admin-get-user"*) {responses[get_user]} ;;
          *"admin-create-user"*) {"echo '{}'" if create == "ok" else fail} ;;
          *"admin-add-user-to-group"*) {"exit 0" if group == "ok" else fail} ;;
        esac
        """))
    script.chmod(0o755)
    body = "set -e\n" + extract_function("ensure_admin_account") + f'ensure_admin_account {POOL} "{email}"\necho "계속 진행"\n'
    result = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                            env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    calls = [line.split()[1] for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls, (log.read_text() if log.exists() else "")


def test_admin_account_is_created_and_put_in_both_groups(tmp_path):
    result, calls, log = run_admin(tmp_path, ADMIN, get_user="missing")
    assert result.returncode == 0 and "계속 진행" in result.stdout and "초대 메일" in result.stdout
    assert calls == ["admin-get-user", "admin-create-user", "admin-add-user-to-group", "admin-add-user-to-group"]
    # 이메일을 아이디로 만들고, 인증된 이메일로 표시하고, 초대 메일로 임시 비밀번호를 보낸다
    assert (f"admin-create-user --user-pool-id {POOL} --username {ADMIN} --user-attributes Name=email,Value={ADMIN} "
            "Name=email_verified,Value=true --desired-delivery-mediums EMAIL") in log
    assert "--group-name admins" in log and "--group-name approvers" in log


def test_existing_account_only_gets_the_groups(tmp_path):
    # 스스로 가입한 계정: 비밀번호·속성은 그대로 두고 권한만 더한다
    result, calls, _ = run_admin(tmp_path, ADMIN, get_user="exists")
    assert result.returncode == 0 and "이미 있습니다" in result.stdout
    assert calls == ["admin-get-user", "admin-add-user-to-group", "admin-add-user-to-group"]


def test_no_admin_email_changes_nothing(tmp_path):
    result, calls, _ = run_admin(tmp_path, "")
    assert result.returncode == 0 and calls == [] and "승인할 사람이 없습니다" in result.stdout


@pytest.mark.parametrize("case, expected_calls", [
    ({"get_user": "denied"}, ["admin-get-user"]),  # 권한이 없으면 만들려고 하지 않는다
    ({"get_user": "missing", "create": "fail"}, ["admin-get-user", "admin-create-user"]),
    ({"group": "fail"}, ["admin-get-user", "admin-add-user-to-group"]),
])
def test_admin_account_failure_does_not_stop_the_deploy(tmp_path, case, expected_calls):
    # 인프라는 이미 배포됐다: 경고만 남기고 이어서 프론트엔드를 배포한다
    result, calls, _ = run_admin(tmp_path, ADMIN, **case)
    assert result.returncode == 0 and "계속 진행" in result.stdout and "⚠️" in result.stderr
    assert calls == expected_calls


def test_deploy_never_deletes_users_and_runs_admin_after_the_user_pool():
    assert "admin-delete-user" not in DEPLOY_SH and "admin-remove-user-from-group" not in DEPLOY_SH
    # User Pool ID를 읽은 뒤에 부른다
    assert DEPLOY_SH.index('USER_POOL_ID=$(aws ssm get-parameter') < DEPLOY_SH.index(
        'ensure_admin_account "$USER_POOL_ID" "$ADMIN_EMAIL"')


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@example.com", "admin@example.com; rm -rf /"])
def test_bad_admin_email_stops_before_anything_changes(tmp_path, email):
    log = tmp_path / "calls.log"
    script = tmp_path / "aws"
    script.write_text(f'#!/bin/bash\necho "$*" >> "{log}"\necho 123456789012\n')
    script.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "deploy.sh"), "dev"], capture_output=True, text=True, cwd=tmp_path,
                            env={"PATH": f"{tmp_path}:/usr/bin:/bin", "ADMIN_EMAIL": email})
    assert result.returncode == 1 and "ADMIN_EMAIL" in result.stdout
    # 계정 확인(sts)과 리전 조회만 하고 멈춘다
    assert all(line.split()[0] in ("sts", "configure") for line in log.read_text().splitlines())


def test_ci_passes_admin_email_to_both_deploys():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert workflow.count("ADMIN_EMAIL: ${{ vars.ADMIN_EMAIL }}") == 2


def test_self_signup_stays_open_and_invite_has_the_required_placeholders():
    import yaml
    from test_injection import CfnLoader
    pool = yaml.load((ROOT / "cloudformation" / "base.yaml").read_text(encoding="utf-8"), Loader=CfnLoader)[
        "Resources"]["UserPool"]["Properties"]["AdminCreateUserConfig"]
    assert pool["AllowAdminCreateUserOnly"] is False  # 일반 사용자는 스스로 가입한다
    message = pool["InviteMessageTemplate"]["EmailMessage"]  # 관리자 초대 메일. Cognito는 두 자리가 모두 있어야 받는다
    assert "{username}" in message and "{####}" in message


# ---------------------------------------------------------------- .env의 이메일 (ADMIN_EMAIL, ALARM_EMAIL)

def run_copied_deploy(tmp_path, dotenv_text, environ=None):
    """deploy.sh를 임시 폴더로 복사해 그 옆의 .env로 실행한다 (저장소의 진짜 .env는 건드리지 않는다).
    잘못된 이메일이면 맨 앞에서 멈추므로, 멈출 때 찍는 값으로 어느 값을 읽었는지 본다."""
    (tmp_path / "deploy.sh").write_text(DEPLOY_SH)
    if dotenv_text is not None:
        (tmp_path / ".env").write_text(dotenv_text, encoding="utf-8")
    script = tmp_path / "aws"
    script.write_text("#!/bin/bash\necho 123456789012\n")
    script.chmod(0o755)
    return subprocess.run(["bash", str(tmp_path / "deploy.sh"), "dev"], capture_output=True, text=True, cwd=tmp_path,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin", **(environ or {})})


def test_admin_email_is_read_from_dotenv(tmp_path):
    result = run_copied_deploy(tmp_path, "ANTHROPIC_API_KEY=\nADMIN_EMAIL=from-dotenv\n")
    assert result.returncode == 1 and "'from-dotenv'" in result.stdout


def test_command_line_value_wins_over_dotenv(tmp_path):
    result = run_copied_deploy(tmp_path, "ADMIN_EMAIL=admin@example.com\n", {"ADMIN_EMAIL": "from-command"})
    assert result.returncode == 1 and "'from-command'" in result.stdout


@pytest.mark.parametrize("dotenv_text, expected", [
    ('ADMIN_EMAIL="quoted"\n', "quoted"),                      # 감싼 따옴표를 벗긴다
    ("  ADMIN_EMAIL = spaced  \n", "spaced"),                  # 앞뒤 공백
    ("ADMIN_EMAIL=first\nADMIN_EMAIL=last\n", "last"),         # 같은 키는 마지막 줄 (설치 도구와 같다)
    ("# ADMIN_EMAIL=commented\nADMIN_EMAIL=real\n", "real"),   # 주석 줄은 키가 다르다
    ("ADMIN_EMAIL=$(touch pwned)\n", "$(touch pwned)"),        # 셸로 실행하지 않고 글자 그대로
])
def test_dotenv_values_follow_the_installer_rules(tmp_path, dotenv_text, expected):
    result = run_copied_deploy(tmp_path, dotenv_text)
    assert result.returncode == 1 and f"'{expected}'" in result.stdout
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("dotenv_text", [None, "ANTHROPIC_API_KEY=\n", "ADMIN_EMAIL=\n", b"\xff\xfe broken"])
def test_missing_or_unreadable_dotenv_means_no_admin_email(tmp_path, dotenv_text):
    # .env가 없거나 값이 비었거나 읽지 못해도 멈추지 않는다 (그다음 단계까지 간다)
    body = "set -e\n" + f'ROOT_ENV_FILE="{tmp_path}/.env"\n' + extract_function("dotenv_get") + \
        'echo "[$(dotenv_get ADMIN_EMAIL)]"\n'
    if isinstance(dotenv_text, bytes):
        (tmp_path / ".env").write_bytes(dotenv_text)
    elif dotenv_text is not None:
        (tmp_path / ".env").write_text(dotenv_text)
    result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0 and result.stdout.strip() == "[]"


def test_dotenv_example_lists_the_emails():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "\nADMIN_EMAIL=\n" in example and "\nALARM_EMAIL=\n" in example
