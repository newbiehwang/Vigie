"""사전 점검(check): CLI를 실제처럼 별도 프로세스로 실행하고 가짜 aws·gh 등의 결과로 판정을 확인한다"""
import json

import pytest

from .helpers import checks_by_id, events, healthy_mac, run_cli

# check가 호출해도 되는 aws·gh 명령 (모두 읽기 전용). 이 밖의 명령을 부르면 테스트가 실패한다
READ_ONLY = {
    "aws": ("--version", "configure get region", "sts get-caller-identity",
            "cloudformation describe-stacks", "ssm describe-parameters", "service-quotas list-service-quotas",
            "s3api list-buckets", "iam simulate-principal-policy", "organizations describe-organization"),
    "gh": ("--version", "auth status"),
}


def check_json(fake, repo, *extra):
    return run_cli(fake, "check", "--json", "--repo", str(repo), *extra)


def test_healthy_mac_passes(fake, repo):
    healthy_mac(fake)
    result = check_json(fake, repo)
    assert result.returncode == 0, result.stdout + result.stderr

    evts = events(result.stdout)   # 모든 줄이 JSON이어야 읽는 쪽이 파싱할 수 있다
    assert evts[0] == {"type": "step_started", "step": "check", "title": "사전 점검"}
    assert evts[-1]["type"] == "step_finished" and evts[-1]["status"] == "ok"

    checks = checks_by_id(result.stdout)
    assert {"system", "aws_cli", "gh_cli", "git", "node", "npm", "zip", "unzip", "pip", "python",
            "homebrew", "repo", "aws_credentials", "root_account", "aws_permissions", "aws_deploy_permissions",
            "region", "github_auth"} == checks.keys()
    assert {c["status"] for c in checks.values()} <= {"ok", "info"}
    assert checks["aws_credentials"]["detail"] == "계정 123456789012 · arn:aws:iam::123456789012:user/vigie-installer"
    assert checks["region"]["detail"] == "ap-northeast-2 (기본값)"
    assert checks["github_auth"]["detail"] == "octocat(으)로 로그인됨"
    assert checks["system"]["detail"] == "macOS 14.5 · Apple Silicon (arm64)"


def test_check_only_runs_read_only_commands(fake, repo):
    healthy_mac(fake)
    check_json(fake, repo)
    for tool, allowed in READ_ONLY.items():
        for call in fake.calls(tool):
            joined = " ".join(call["args"])
            assert any(a in joined for a in allowed), f"점검에서 허용되지 않은 명령: {tool} {joined}"


def test_dry_run_gives_same_result(fake, repo):
    healthy_mac(fake)
    normal = check_json(fake, repo)
    dry = check_json(fake, repo, "--dry-run")
    assert dry.returncode == normal.returncode == 0
    assert checks_by_id(dry.stdout) == checks_by_id(normal.stdout)


def test_aws_commands_get_region_and_no_pager(fake, repo):
    healthy_mac(fake)
    check_json(fake, repo, "--region", "us-west-2")
    sts = [c for c in fake.calls("aws") if "sts" in c["args"]][0]
    assert sts["env"]["AWS_REGION"] == sts["env"]["AWS_DEFAULT_REGION"] == "us-west-2"
    assert sts["env"]["AWS_PAGER"] == ""


def test_profile_is_passed_to_aws(fake, repo):
    fake.add("aws", "configure get region --profile vigie-dev", "ap-northeast-2\n")
    healthy_mac(fake)
    result = run_cli(fake, "check", "--json", "--repo", str(repo), "--profile", "vigie-dev",
                     env=fake.env(AWS_ACCESS_KEY_ID="AKIAOTHER"))
    sts = [c for c in fake.calls("aws") if "sts" in c["args"]][0]
    assert sts["env"]["AWS_PROFILE"] == "vigie-dev" and "AWS_ACCESS_KEY_ID" not in sts["env"]
    checks = checks_by_id(result.stdout)
    assert checks["aws_credentials"]["title"] == "AWS 자격 증명 (프로필 vigie-dev)"
    assert checks["region"]["detail"] == "ap-northeast-2 (AWS CLI 프로필 설정)"


def test_missing_aws_cli_fails_and_skips_sts(fake, repo):
    healthy_mac(fake)
    fake.remove("aws")
    result = check_json(fake, repo)
    checks = checks_by_id(result.stdout)
    assert result.returncode == 1
    assert checks["aws_cli"] == {"type": "check", "id": "aws_cli", "title": "AWS CLI", "status": "fail",
                                 "detail": "설치되어 있지 않습니다",
                                 "hint": "brew install awscli v1이 설치되어 있다면 먼저 제거하세요 (pip uninstall awscli)"}
    assert checks["aws_credentials"]["status"] == "fail"
    assert "root_account" not in checks
    assert events(result.stdout)[-1]["status"] == "failed"


def test_aws_cli_v1_is_rejected(fake, repo):
    fake.add("aws", "--version", stderr="aws-cli/1.32.0 Python/3.9.6 Darwin/23.5.0 botocore/1.34.0\n")
    healthy_mac(fake)
    checks = checks_by_id(check_json(fake, repo).stdout)
    assert checks["aws_cli"]["status"] == "fail"
    assert checks["aws_cli"]["detail"] == "1.32.0 (필요: 2.0.0 이상)"
    assert checks["aws_credentials"]["status"] == "fail"   # v1으로는 진행하지 않는다


def test_old_node_is_rejected(fake, repo):
    fake.add("node", "--version", "v16.20.2\n")
    healthy_mac(fake)
    result = check_json(fake, repo)
    assert result.returncode == 1
    assert checks_by_id(result.stdout)["node"]["detail"] == "16.20.2 (필요: 18.0.0 이상)"


def test_unparseable_version_is_only_a_warning(fake, repo):
    fake.add("node", "--version", "나중에 바뀐 출력 형식\n")
    healthy_mac(fake)
    result = check_json(fake, repo)
    assert result.returncode == 0
    assert checks_by_id(result.stdout)["node"]["status"] == "warn"


def test_gh_is_optional(fake, repo):
    # gh는 GitHub 자동 배포 단계에만 필요하므로 없어도 점검은 통과한다
    healthy_mac(fake)
    fake.remove("gh")
    result = check_json(fake, repo)
    checks = checks_by_id(result.stdout)
    assert result.returncode == 0
    assert checks["gh_cli"]["status"] == checks["github_auth"]["status"] == "warn"


def test_gh_not_logged_in_is_a_warning(fake, repo):
    fake.add("gh", "auth status", stderr="You are not logged into any GitHub hosts.\n", exit=1)
    healthy_mac(fake)
    result = check_json(fake, repo)
    assert result.returncode == 0
    assert "gh auth login" in checks_by_id(result.stdout)["github_auth"]["hint"]


def test_pip_found_the_same_way_as_deploy_sh(fake, repo):
    healthy_mac(fake)
    fake.remove("pip")
    fake.add("python3", "-m pip --version", "pip 24.0 from /x (python 3.12)\n")
    checks = checks_by_id(check_json(fake, repo).stdout)
    assert checks["pip"]["detail"] == "24.0"


def test_missing_pip_fails(fake, repo):
    healthy_mac(fake)
    fake.remove("pip")
    result = check_json(fake, repo)
    assert result.returncode == 1 and checks_by_id(result.stdout)["pip"]["status"] == "fail"


def test_root_account_is_warned(fake, repo):
    healthy_mac(fake, identity={"UserId": "123456789012", "Account": "123456789012",
                                "Arn": "arn:aws:iam::123456789012:root"})
    result = check_json(fake, repo)
    root = checks_by_id(result.stdout)["root_account"]
    assert result.returncode == 0   # 배포는 가능하지만 경고한다
    assert root["status"] == "warn" and "IAM 사용자" in root["hint"]


def test_assumed_role_is_ok(fake, repo):
    healthy_mac(fake, identity={"UserId": "AROA:x", "Account": "123456789012",
                                "Arn": "arn:aws:sts::123456789012:assumed-role/Admin/x"})
    assert checks_by_id(check_json(fake, repo).stdout)["root_account"]["detail"] == "IAM 역할(임시 자격 증명)"


@pytest.mark.parametrize("stderr, detail", [
    ("Unable to locate credentials. You can configure credentials by running \"aws configure\".",
     "자격 증명이 설정되어 있지 않습니다"),
    ("An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation: "
     "The security token included in the request is invalid.", "Access Key가 올바르지 않거나 비활성화되었습니다"),
    ("An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: expired",
     "자격 증명이 만료되었습니다"),
    ("The config profile (nope) could not be found", "지정한 AWS 프로필이 없습니다"),
    ("알 수 없는 오류 메시지", "확인하지 못했습니다"),
])
def test_credential_errors_are_explained(fake, repo, stderr, detail):
    fake.add("aws", "sts get-caller-identity", stderr=stderr + "\n", exit=255)
    healthy_mac(fake)
    result = check_json(fake, repo)
    checks = checks_by_id(result.stdout)
    assert result.returncode == 1
    assert checks["aws_credentials"]["detail"] == detail
    # 설명과 따로, AWS CLI가 낸 오류 원문을 그대로 보여 준다
    assert checks["aws_credentials"]["raw"] == stderr
    assert "root_account" not in checks


def test_malformed_identity_response_fails(fake, repo):
    fake.add("aws", "sts get-caller-identity", "{}")
    healthy_mac(fake)
    assert checks_by_id(check_json(fake, repo).stdout)["aws_credentials"]["status"] == "fail"


def test_invalid_region_fails(fake, repo):
    healthy_mac(fake)
    result = check_json(fake, repo, "--region", "seoul")
    assert result.returncode == 1 and checks_by_id(result.stdout)["region"]["status"] == "fail"


def test_non_mac_is_reported_without_blocking(fake, repo):
    fake.add("uname", "-s", "Linux\n").add("uname", "-m", "x86_64\n")
    healthy_mac(fake)
    result = check_json(fake, repo)
    system = checks_by_id(result.stdout)["system"]
    assert result.returncode == 0
    assert system["status"] == "info" and system["detail"] == "Linux (x86_64)"
    assert fake.calls("sw_vers") == []


def test_macos_version_is_reported(fake, repo):
    fake.add("sw_vers", "-productVersion", "12.7.4\n")
    fake.add("uname", "-m", "x86_64\n")
    healthy_mac(fake)
    system = checks_by_id(check_json(fake, repo).stdout)["system"]
    assert system["status"] == "ok" and system["detail"] == "macOS 12.7.4 · Intel (x86_64)"


def test_repo_not_found(fake, tmp_path):
    healthy_mac(fake)
    result = run_cli(fake, "check", "--json", cwd=tmp_path)   # --repo 없이 저장소 밖에서 실행
    repo_check = checks_by_id(result.stdout)["repo"]
    assert result.returncode == 1 and repo_check["status"] == "fail" and "--repo" in repo_check["hint"]


def test_repo_found_from_subdirectory(fake, repo):
    healthy_mac(fake)
    result = run_cli(fake, "check", "--json", cwd=repo / "cloudformation")
    assert checks_by_id(result.stdout)["repo"]["detail"] == f"{repo.resolve()} (커밋 abcdef123456)"


def test_deploy_sh_must_be_executable(fake, repo):
    healthy_mac(fake)
    (repo / "deploy.sh").chmod(0o644)
    repo_check = checks_by_id(check_json(fake, repo).stdout)["repo"]
    assert repo_check["status"] == "fail" and repo_check["hint"].startswith("chmod +x")


def test_repo_without_git_is_a_warning(fake, repo):
    fake.add("git", "rev-parse", stderr="fatal: not a git repository\n", exit=128)
    healthy_mac(fake)
    result = check_json(fake, repo)
    assert result.returncode == 0 and checks_by_id(result.stdout)["repo"]["status"] == "warn"


def test_text_mode_is_human_readable(fake, repo):
    healthy_mac(fake)
    result = run_cli(fake, "check", "--repo", str(repo))
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert "사전 점검" in lines and "  [ OK ] AWS CLI: 2.17.0" in lines
    assert lines[-1] == "✓ 17개 통과"   # 0개인 주의·오류는 말하지 않는다
    assert not any(line.startswith("{") for line in lines)


def test_text_mode_shows_error_source_under_the_item(fake, repo):
    # [오류] 바로 아랫줄에 명령이 낸 오류 원문, 그 아래에 해결 안내
    fake.add("aws", "sts get-caller-identity", exit=254,
             stderr="An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation: bad\n")
    healthy_mac(fake)
    lines = run_cli(fake, "check", "--repo", str(repo)).stdout.splitlines()
    at = lines.index("  [오류] AWS 자격 증명: Access Key가 올바르지 않거나 비활성화되었습니다")
    assert lines[at + 1] == ("         An error occurred (InvalidClientTokenId) when calling the "
                             "GetCallerIdentity operation: bad")
    assert lines[at + 2] == "         → IAM 콘솔에서 키 상태를 확인하고 다시 입력하세요"
    assert lines[-1] == "✗ 1개 오류 — 해결한 뒤 다시 점검하세요"


def test_usage_error_exit_code(fake):
    result = run_cli(fake, "check", "--env", "staging")
    assert result.returncode == 2 and "invalid choice" in result.stderr


def test_unexpected_error_becomes_error_event(fake, repo):
    # sts 응답의 Arn이 문자열이 아닌 경우처럼 예상하지 못한 예외도 JSON 이벤트로 알려야 읽는 쪽이 멈추지 않는다
    fake.add("aws", "sts get-caller-identity", json.dumps({"Account": "1", "Arn": 123}))
    healthy_mac(fake)
    result = check_json(fake, repo)
    last = events(result.stdout)[-1]
    assert result.returncode == 1
    assert last["type"] == "error" and last["message"].startswith("예상하지 못한 오류: AttributeError")
    assert "Traceback" in result.stderr


# --- 권한 점검 -------------------------------------------------------------------------------------

DENIED = ("An error occurred (AccessDenied) when calling the DescribeStacks operation: User: "
          "arn:aws:iam::123456789012:user/vigie-installer is not authorized to perform: cloudformation:DescribeStacks\n")


def deny_all_reads(fake):
    """정책이 하나도 없는 IAM 사용자: 자격 증명은 유효하지만 모든 조회가 거부된다"""
    for match in ("cloudformation describe-stacks", "ssm describe-parameters", "service-quotas list-service-quotas",
                  "s3api list-buckets"):
        fake.add("aws", match, stderr=DENIED, exit=254)


def test_user_without_policy_fails_at_check(fake, repo):
    # 실제로 있었던 상황: 키만 만들고 정책을 붙이지 않은 사용자. sts는 권한 없이도 성공하므로
    # 자격 증명 항목은 통과하지만, 권한 항목이 실패해 점검 단계에서 바로 알 수 있어야 한다
    deny_all_reads(fake)
    healthy_mac(fake)
    result = check_json(fake, repo)
    checks = checks_by_id(result.stdout)
    assert result.returncode == 1
    assert checks["aws_credentials"]["status"] == "ok"
    perm = checks["aws_permissions"]
    assert perm["status"] == "fail"
    assert perm["detail"] == "권한이 없습니다: CloudFormation, SSM Parameter Store, Service Quotas, S3"
    assert "사용자 → vigie-installer → 권한 탭" in perm["hint"] and "AdministratorAccess" in perm["hint"]
    # 조회가 막혔으면 쓰기 권한은 따로 묻지 않는다 (결과가 뻔하고 항목만 늘어난다)
    assert "aws_deploy_permissions" not in checks
    assert not [c for c in fake.calls("aws") if "simulate-principal-policy" in c["args"]]


def test_service_not_activated_yet(fake, repo):
    fake.add("aws", "service-quotas list-service-quotas", exit=254,
             stderr="An error occurred (SubscriptionRequiredException) when calling the ListServiceQuotas operation\n")
    healthy_mac(fake)
    perm = checks_by_id(check_json(fake, repo).stdout)["aws_permissions"]
    assert perm["status"] == "fail" and perm["detail"] == "아직 쓸 수 없는 서비스: Service Quotas"
    assert "24시간" in perm["hint"]


def test_unknown_read_error_is_only_a_warning(fake, repo):
    fake.add("aws", "ssm describe-parameters", exit=255,
             stderr="Could not connect to the endpoint URL: \"https://ssm.ap-northeast-2.amazonaws.com/\"\n")
    healthy_mac(fake)
    result = check_json(fake, repo)
    perm = checks_by_id(result.stdout)["aws_permissions"]
    assert result.returncode == 0
    assert perm["status"] == "warn" and perm["detail"] == "확인하지 못했습니다 (SSM Parameter Store)"
    assert perm["raw"].startswith("Could not connect to the endpoint URL")


def test_permission_checks_use_profile_and_region(fake, repo):
    fake.add("aws", "configure get region --profile vigie-installer", "ap-northeast-2\n")
    healthy_mac(fake)
    run_cli(fake, "check", "--json", "--repo", str(repo), "--profile", "vigie-installer")
    probe = [c for c in fake.calls("aws") if "describe-parameters" in c["args"]][0]
    assert probe["env"]["AWS_PROFILE"] == "vigie-installer" and probe["env"]["AWS_REGION"] == "ap-northeast-2"


def test_deploy_permissions_are_simulated_for_the_caller(fake, repo):
    healthy_mac(fake)
    result = check_json(fake, repo)
    checks = checks_by_id(result.stdout)
    assert checks["aws_permissions"]["status"] == "ok"
    assert checks["aws_deploy_permissions"]["status"] == "ok"

    sims = [c["args"] for c in fake.calls("aws") if "simulate-principal-policy" in c["args"]]
    assert len(sims) == 2
    for args in sims:
        assert args[args.index("--policy-source-arn") + 1] == "arn:aws:iam::123456789012:user/vigie-installer"
    general, iam = sims
    assert "cloudformation:CreateStack" in general and "iam:CreateRole" not in general
    assert "--resource-arns" not in general
    # IAM 작업은 템플릿이 쓰는 vigie-* Role 이름으로 묻는다 (vigie-*로 좁힌 정책도 통과하도록)
    assert "iam:CreateRole" in iam and "iam:PassRole" in iam
    assert iam[iam.index("--resource-arns") + 1] == "arn:aws:iam::123456789012:role/vigie-permission-check"


def test_read_only_policy_fails_deploy_permissions(fake, repo):
    # ReadOnlyAccess만 붙은 사용자: 조회는 되지만 배포는 첫 리소스를 만들 때 실패한다
    denied = [{"EvalActionName": action, "EvalDecision": "implicitDeny"}
              for action in ("ssm:PutParameter", "cloudformation:CreateStack", "s3:CreateBucket",
                             "lambda:CreateFunction", "dynamodb:CreateTable", "sns:CreateTopic")]
    # 첫 번째 시뮬레이션(IAM 밖의 작업)에만 맞는 규칙. IAM 작업 시뮬레이션은 healthy_mac의 "허용"을 받는다
    fake.add("aws", "cloudformation:CreateStack", json.dumps({"EvaluationResults": denied}))
    healthy_mac(fake)
    result = check_json(fake, repo)
    deploy = checks_by_id(result.stdout)["aws_deploy_permissions"]
    assert result.returncode == 1
    assert deploy["status"] == "fail"
    assert deploy["detail"] == ("ap-northeast-2에서 허용되지 않는 작업: ssm:PutParameter, cloudformation:CreateStack, "
                                "s3:CreateBucket, lambda:CreateFunction, dynamodb:CreateTable 외 1개")
    assert "AdministratorAccess" in deploy["hint"]


def test_explicit_deny_counts_as_denied(fake, repo):
    fake.add("aws", "iam:CreateRole", json.dumps({"EvaluationResults": [
        {"EvalActionName": "iam:CreateRole", "EvalDecision": "explicitDeny"}]}))
    healthy_mac(fake)
    deploy = checks_by_id(check_json(fake, repo).stdout)["aws_deploy_permissions"]
    assert deploy["status"] == "fail" and deploy["detail"] == "ap-northeast-2에서 허용되지 않는 작업: iam:CreateRole"


def test_simulator_not_allowed_is_only_a_warning(fake, repo):
    # PowerUserAccess처럼 IAM 조회 권한이 없으면 시뮬레이터도 못 쓴다. 조회는 통과했으니 막지는 않는다
    fake.add("aws", "iam simulate-principal-policy", exit=254,
             stderr="An error occurred (AccessDenied) when calling the SimulatePrincipalPolicy operation: "
                    "User is not authorized to perform: iam:SimulatePrincipalPolicy\n")
    healthy_mac(fake)
    result = check_json(fake, repo)
    deploy = checks_by_id(result.stdout)["aws_deploy_permissions"]
    assert result.returncode == 0
    assert deploy["status"] == "warn" and "iam:SimulatePrincipalPolicy" in deploy["detail"]


def test_assumed_role_skips_simulation(fake, repo):
    healthy_mac(fake, identity={"Account": "123456789012", "UserId": "AROAEXAMPLE:me",
                                "Arn": "arn:aws:sts::123456789012:assumed-role/vigie-github-deploy-dev/me"})
    checks = checks_by_id(check_json(fake, repo).stdout)
    assert checks["aws_permissions"]["status"] == "ok"
    assert checks["aws_deploy_permissions"]["status"] == "info"
    assert not [c for c in fake.calls("aws") if "simulate-principal-policy" in c["args"]]


def test_invalid_credentials_skip_permission_checks(fake, repo):
    fake.add("aws", "sts get-caller-identity", exit=254,
             stderr="An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation\n")
    healthy_mac(fake)
    checks = checks_by_id(check_json(fake, repo).stdout)
    assert checks["aws_credentials"]["status"] == "fail"
    assert "aws_permissions" not in checks and "aws_deploy_permissions" not in checks
    assert not [c for c in fake.calls("aws") if "describe-stacks" in c["args"]]


# 실제로 만난 메시지 (교육·회사 등 다른 조직이 만든 구성원 계정). 계정 번호는 예시로 바꿨다
SCP_DENIED = ("An error occurred (AccessDeniedException) when calling the DescribeParameters operation: User: "
              "arn:aws:iam::123456789012:user/vigie-installer is not authorized to perform: ssm:DescribeParameters "
              "on resource: arn:aws:ssm:ap-northeast-2:123456789012:* with an explicit deny in a service control "
              "policy: arn:aws:organizations::999988887777:policy/o-example111/service_control_policy/p-example1\n")
MEMBER_ORG = json.dumps({"Organization": {"Id": "o-example111", "MasterAccountId": "999988887777",
                                          "FeatureSet": "ALL"}})


def test_scp_denial_points_to_the_organization_not_iam(fake, repo):
    # AdministratorAccess를 붙여도 SCP는 풀리지 않는다. "정책을 붙이세요"라고 안내하면 헛수고를 시킨다
    fake.add("aws", "organizations describe-organization", MEMBER_ORG)
    for match in ("cloudformation describe-stacks", "ssm describe-parameters", "service-quotas list-service-quotas"):
        fake.add("aws", match, stderr=SCP_DENIED, exit=254)
    healthy_mac(fake)
    result = check_json(fake, repo)
    checks = checks_by_id(result.stdout)
    assert result.returncode == 1

    org = checks["organization"]
    assert org["status"] == "info"
    assert org["detail"] == "o-example111의 구성원 계정 (관리 계정 999988887777)"
    perm = checks["aws_permissions"]
    assert perm["detail"] == "권한이 없습니다: CloudFormation, SSM Parameter Store, Service Quotas (조직 SCP가 거부)"
    assert "서비스 제어 정책(SCP)" in perm["hint"] and "조직 관리 계정(999988887777)의 관리자" in perm["hint"]
    assert "--region" in perm["hint"]
    assert perm["raw"] == SCP_DENIED.strip()   # 거부 원문 (어느 SCP가 막았는지 들어 있다)   # 리전 제한이 가장 흔한 원인이다 (AWS가 관리하는 프로젝트 계정)
    assert "권한 추가에서" not in perm["hint"]


def test_scp_hint_without_organization_details(fake, repo):
    # 구성원 계정은 보통 describe-organization을 볼 수 있지만, 막혀 있어도 안내는 SCP 기준이어야 한다
    fake.add("aws", "organizations describe-organization", exit=254, stderr=DENIED)
    fake.add("aws", "ssm describe-parameters", stderr=SCP_DENIED, exit=254)
    healthy_mac(fake)
    checks = checks_by_id(check_json(fake, repo).stdout)
    assert "organization" not in checks
    assert "조직 관리 계정의 관리자" in checks["aws_permissions"]["hint"]


def test_permissions_boundary_denial(fake, repo):
    fake.add("aws", "ssm describe-parameters", exit=254,
             stderr="An error occurred (AccessDeniedException) when calling the DescribeParameters operation: User: "
                    "arn:aws:iam::123456789012:user/vigie-installer is not authorized to perform: ssm:DescribeParameters "
                    "with an explicit deny in a permissions boundary\n")
    healthy_mac(fake)
    perm = checks_by_id(check_json(fake, repo).stdout)["aws_permissions"]
    assert perm["detail"] == "권한이 없습니다: SSM Parameter Store (권한 경계가 거부)"
    assert "사용자 → vigie-installer → 권한 탭 → 권한 경계" in perm["hint"]


def test_management_account_is_reported(fake, repo):
    fake.add("aws", "organizations describe-organization", json.dumps(
        {"Organization": {"Id": "o-example111", "MasterAccountId": "123456789012"}}))
    healthy_mac(fake)
    checks = checks_by_id(check_json(fake, repo).stdout)
    assert checks["organization"]["detail"] == "o-example111의 관리 계정"
    assert checks["aws_permissions"]["status"] == "ok"



def test_simulation_is_told_the_target_region(fake, repo):
    # 리전을 제한하는 SCP는 요청 리전을 모르면 모두 거부로 나온다. 실제로 AWS 관리 프로젝트 계정에서
    # 허용된 리전(시드니)으로 점검했는데도 배포 작업 18개가 거부로 나왔다
    healthy_mac(fake)
    check_json(fake, repo, "--region", "ap-southeast-2")
    general, iam = [c["args"] for c in fake.calls("aws") if "simulate-principal-policy" in c["args"]]
    region = "ContextKeyName=aws:RequestedRegion,ContextKeyValues={},ContextKeyType=string"
    assert general[general.index("--context-entries") + 1] == region.format("ap-southeast-2")
    # IAM은 전역 서비스라 실제 요청 리전이 us-east-1이다
    assert iam[iam.index("--context-entries") + 1] == region.format("us-east-1")


def test_scp_denial_in_simulation_gets_the_scp_hint(fake, repo):
    fake.add("aws", "organizations describe-organization", MEMBER_ORG)
    fake.add("aws", "cloudformation:CreateStack", json.dumps({"EvaluationResults": [
        {"EvalActionName": "cloudformation:CreateStack", "EvalDecision": "explicitDeny",
         "OrganizationsDecisionDetail": {"AllowedByOrganizations": False}}]}))
    healthy_mac(fake)
    deploy = checks_by_id(check_json(fake, repo).stdout)["aws_deploy_permissions"]
    assert deploy["detail"] == "ap-northeast-2에서 허용되지 않는 작업: cloudformation:CreateStack (조직 SCP가 거부)"
    assert "서비스 제어 정책(SCP)" in deploy["hint"] and "999988887777" in deploy["hint"]
