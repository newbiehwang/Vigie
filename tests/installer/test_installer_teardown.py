"""teardown: 되돌릴 수 없는 삭제. 보호 장치(prod 거부, 한 번뿐인 이름 입력 확인, --yes 무시)와 순서, 공유 자원 보호를 확인한다"""
import json

import pytest

from vigie_installer.steps import teardown

from .helpers import responses, run_cli, run_step

ACCOUNT = "123456789012"
REPO = "octo/Vigie"
ENV_BUCKETS = [f"vigie-{kind}-{ACCOUNT}-dev" for kind in (
    "deployment", "frontend", "outputbucket", "athenaoutputbucket", "guarddutyexportbucket", "dockerbuildbucket",
    "diagrambucket")]
SHARED_BUCKET = f"vigie-cloudformation-{ACCOUNT}"
DEV_STACKS = ["vigie-dev", "vigie-mcp-dev", "vigie-frontend-dev", "vigie-base-dev"]


def account(fake, *, stacks=None, buckets=None, ecr=True, log_groups=None, params=("ANTHROPIC_API_KEY",),
            owns_provider=False):
    stacks = DEV_STACKS + ["vigie-github-oidc-dev"] if stacks is None else stacks
    buckets = ENV_BUCKETS + [SHARED_BUCKET] if buckets is None else buckets
    fake.add("aws", "sts get-caller-identity", json.dumps({"Account": ACCOUNT, "Arn": "arn:aws:iam::1:user/x"}))
    fake.add("aws", "cloudformation describe-stacks",
             json.dumps({"Stacks": [{"StackName": name, "StackStatus": "UPDATE_COMPLETE"} for name in stacks]}))
    if owns_provider:
        fake.add("aws", "describe-stack-resource", json.dumps({"StackResourceDetail": {}}))
    else:
        fake.add("aws", "describe-stack-resource", stderr="Resource GitHubOidcProvider does not exist\n", exit=254)
    if ecr:
        fake.add("aws", "ecr describe-repositories", json.dumps({"repositories": [{"repositoryName": "vigie-mcp-dev"}]}))
    else:
        fake.add("aws", "ecr describe-repositories", stderr="RepositoryNotFoundException\n", exit=254)
    fake.add("aws", "ecr delete-repository", "{}")
    for bucket in buckets:
        fake.add("aws", f"head-bucket --bucket {bucket}")
    fake.add("aws", "head-bucket", stderr="An error occurred (404) when calling the HeadBucket operation: Not Found\n",
             exit=254)
    # 버킷마다 처음 한 번은 객체(버전·삭제 마커)가 있고, 지운 뒤에는 빈 목록
    for bucket in buckets:
        fake.add("aws", f"list-object-versions --bucket {bucket} ", json.dumps({
            "Versions": [{"Key": "a.txt", "VersionId": "v1"}, {"Key": "a.txt", "VersionId": "v2"}],
            "DeleteMarkers": [{"Key": "b.txt", "VersionId": "m1"}]}), times=1)
    fake.add("aws", "list-object-versions", json.dumps({}))
    fake.add("aws", "delete-objects", json.dumps({"Deleted": []}))
    fake.add("aws", "delete-bucket")
    groups = log_groups if log_groups is not None else ["/aws/lambda/vigie-llm-dev", "/aws/lambda/vigie-mcp-dev",
                                                        "/aws/lambda/vigie-llm-prod"]
    fake.add("aws", "describe-log-groups", json.dumps({"logGroups": [{"logGroupName": g} for g in groups]}))
    fake.add("aws", "delete-log-group")
    fake.add("aws", "describe-parameters", json.dumps({"Parameters": [
        {"Name": f"/vigie/dev/{key}", "Type": "SecureString"} for key in params]}))
    fake.add("aws", "delete-parameters", json.dumps({"DeletedParameters": []}))
    fake.add("aws", "delete-stack")
    fake.add("aws", "wait stack-delete-complete")


def github(fake, *, logged_in=True, variables=("AWS_DEPLOY_ROLE_ARN_DEV", "AWS_REGION"), environment=True):
    if not logged_in:
        fake.add("gh", "auth status", stderr="not logged in\n", exit=1)
        return
    fake.add("gh", "api -X", "{}")
    fake.add("gh", "auth status", "Logged in\n")
    fake.add("gh", "repo view", json.dumps({"nameWithOwner": REPO}))
    fake.add("gh", "variable list", json.dumps([{"name": v, "value": "x"} for v in variables]))
    fake.add("gh", "variable delete")
    if environment:
        fake.add("gh", f"api repos/{REPO}/environments/dev", json.dumps({"name": "dev"}))
    else:
        fake.add("gh", f"api repos/{REPO}/environments/dev", stderr="Not Found (HTTP 404)\n", exit=1)


ALL_GROUPS = ["delete_role_variable", "delete_stacks", "delete_ecr", "delete_buckets", "delete_log_groups",
              "delete_parameters", "delete_github_environment"]


def approve_all(env="dev"):
    # 확인은 환경 이름 입력 한 번뿐이다 (단계마다 다시 묻지 않는다)
    return responses(("text", "confirm_env", env))


def deletions(fake):
    verbs = ("variable delete", "delete-stack", "delete-repository", "delete-objects", "delete-bucket",
             "delete-log-group", "delete-parameters", "api -X DELETE")
    return [" ".join(c["args"]) for c in fake.calls() if any(v in " ".join(c["args"]) for v in verbs)]


def test_full_teardown_in_order(fake):
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, stdin=approve_all())
    assert code == 0, [e for e in evts if e["type"] in ("error", "step_finished")]

    done = deletions(fake)
    order = [next(i for i, call in enumerate(done) if marker in call) for marker in (
        "variable delete AWS_DEPLOY_ROLE_ARN_DEV", "delete-stack --stack-name vigie-dev", "delete-stack --stack-name "
        "vigie-mcp-dev", "delete-stack --stack-name vigie-frontend-dev", "delete-stack --stack-name vigie-base-dev",
        "delete-stack --stack-name vigie-github-oidc-dev", "delete-repository", "delete-bucket --bucket",
        "delete-log-group", "delete-parameters", "api -X DELETE")]
    assert order == sorted(order)   # 저장소 변수 → 스택(역순, OIDC 마지막) → ECR → 버킷 → 로그 → SSM → Environment

    # 모든 버킷(공유 버킷 포함, 다른 환경이 없으므로)을 비운 뒤 지운다
    assert [c.split("--bucket ")[1] for c in done if c.startswith("s3api delete-bucket")] == ENV_BUCKETS + [SHARED_BUCKET]
    objects = json.loads(fake.captures()[0][1])
    assert objects == {"Objects": [{"Key": "a.txt", "VersionId": "v1"}, {"Key": "a.txt", "VersionId": "v2"},
                                   {"Key": "b.txt", "VersionId": "m1"}], "Quiet": True}
    # 다른 환경(prod)의 로그 그룹은 건드리지 않는다
    assert [c for c in done if "delete-log-group" in c] == [
        "logs delete-log-group --log-group-name /aws/lambda/vigie-llm-dev",
        "logs delete-log-group --log-group-name /aws/lambda/vigie-mcp-dev"]
    assert "ssm delete-parameters --names /vigie/dev/ANTHROPIC_API_KEY --output json" in done


def test_prod_requires_allow_prod(fake):
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, env="prod", stdin=approve_all("prod"))
    assert code == 1 and fake.calls() == []
    assert any("--allow-prod" in e.get("message", "") for e in evts)


def test_wrong_environment_name_deletes_nothing(fake):
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, stdin=responses(("text", "confirm_env", "prod")))
    assert code == 1 and deletions(fake) == []
    assert any(e["type"] == "input_required" and e["secret"] is False for e in evts)


def test_yes_flag_does_not_skip_confirmation(fake):
    # --yes여도 환경 이름은 사람이 입력해야 한다 (응답이 없으면 아무것도 지우지 않음)
    account(fake)
    github(fake)
    code, _ = run_step(fake, teardown.run, assume_yes=True)
    assert code == 1 and deletions(fake) == []


def test_asks_only_once_and_logs_each_step(fake):
    # 이름을 한 번 입력하면 단계마다 다시 묻지 않고 끝까지 지운다. 무엇을 지우는지는 한 줄씩 알린다
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, stdin=approve_all())
    assert code == 0
    questions = [e for e in evts if e["type"] in ("input_required", "confirm_required", "choice_required")]
    assert [e["id"] for e in questions] == ["confirm_env"]
    logged = [e["line"] for e in evts if e["type"] == "log"]
    assert "자동 배포를 끄기 위해 저장소 변수 AWS_DEPLOY_ROLE_ARN_DEV을(를) 지웁니다" in logged
    assert "GitHub Environment dev을(를) 지웁니다" in logged


def test_shared_resources_are_kept_while_other_env_exists(fake):
    # prod가 남아 있으면 공유 버킷을 남기고, 이 환경의 OIDC 스택이 공급자를 가지고 있으면 OIDC 스택도 남긴다
    account(fake, stacks=DEV_STACKS + ["vigie-github-oidc-dev", "vigie-base-prod", "vigie-github-oidc-prod"],
            owns_provider=True)
    github(fake)
    code, evts = run_step(fake, teardown.run, stdin=approve_all())
    done = deletions(fake)
    assert code == 0
    assert not any(SHARED_BUCKET in c for c in done)
    assert not any("vigie-github-oidc-dev" in c for c in done)
    assert not any("-prod" in c for c in done)   # prod 환경의 스택·버킷은 지우지 않는다
    kept = next(e for e in evts if e.get("id") == "target_oidc")
    assert kept["status"] == "warn" and "vigie-github-oidc-prod" in kept["hint"]


def test_mcp_stack_blocked_by_ecr_images_is_retried(fake):
    fake.add("aws", "wait stack-delete-complete --stack-name vigie-mcp-dev", exit=255, times=1)
    fake.add("aws", "describe-stack-events --stack-name vigie-mcp-dev", json.dumps({"StackEvents": [{
        "StackName": "vigie-mcp-dev", "StackId": "arn:stack/vigie-mcp-dev", "LogicalResourceId": "MCPRepo",
        "ResourceType": "AWS::ECR::Repository", "PhysicalResourceId": "vigie-mcp-dev", "ResourceStatus": "DELETE_FAILED",
        "ResourceStatusReason": "The repository with name 'vigie-mcp-dev' cannot be deleted because it still contains "
                                "images"}]}))
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, stdin=approve_all())
    done = deletions(fake)
    assert code == 0, [e for e in evts if e["type"] == "error"]
    mcp = [i for i, c in enumerate(done) if "vigie-mcp-dev" in c and not c.startswith("logs")]
    assert [done[i] for i in mcp] == ["cloudformation delete-stack --stack-name vigie-mcp-dev",
                                      "ecr delete-repository --repository-name vigie-mcp-dev --force",
                                      "cloudformation delete-stack --stack-name vigie-mcp-dev"]


def test_stack_failure_stops_before_buckets(fake):
    fake.add("aws", "wait stack-delete-complete --stack-name vigie-dev", exit=255)
    fake.add("aws", "describe-stack-events", json.dumps({"StackEvents": [{
        "StackName": "vigie-dev", "StackId": "arn:stack/vigie-dev", "LogicalResourceId": "ChatTable",
        "ResourceType": "AWS::DynamoDB::Table", "PhysicalResourceId": "t", "ResourceStatus": "DELETE_FAILED",
        "ResourceStatusReason": "Table is being used"}]}))
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, stdin=approve_all())
    assert code == 1
    assert not any("delete-bucket" in c for c in deletions(fake))
    assert any("ChatTable" in e.get("message", "") for e in evts if e["type"] == "error")


def test_partial_delete_objects_failure_stops(fake):
    fake.add("aws", "delete-objects", json.dumps({"Errors": [{"Key": "a.txt", "Code": "AccessDenied"}]}))
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, stdin=approve_all())
    assert code == 1 and not any("delete-bucket" in c for c in deletions(fake))
    assert any("AccessDenied" in e.get("raw", "") for e in evts if e["type"] == "error")   # 원문은 raw에


def test_dry_run_deletes_nothing_and_shows_every_step(fake):
    account(fake)
    github(fake)
    code, evts = run_step(fake, teardown.run, dry_run=True)
    assert code == 0 and deletions(fake) == []
    assert [e["id"] for e in evts if e["type"] == "dry_run"] == ALL_GROUPS
    assert not any(e["type"] == "input_required" for e in evts)   # dry-run에서는 이름 입력을 묻지 않는다


def test_nothing_to_delete(fake):
    account(fake, stacks=[], buckets=[], ecr=False, log_groups=[], params=())
    github(fake, variables=(), environment=False)
    code, evts = run_step(fake, teardown.run)
    assert code == 0 and evts[-1]["status"] == "ok" and deletions(fake) == []   # 지울 것이 없음 = 할 일 없음


def test_github_required_when_oidc_stack_exists(fake):
    # 저장소 변수가 남으면 main push가 배포를 다시 시작하므로, GitHub에 접근할 수 없으면 아무것도 지우지 않는다
    account(fake)
    github(fake, logged_in=False)
    code, _ = run_step(fake, teardown.run, stdin=approve_all())
    assert code == 1 and deletions(fake) == []


def test_github_optional_without_oidc_stack(fake):
    account(fake, stacks=DEV_STACKS)
    github(fake, logged_in=False)
    code, evts = run_step(fake, teardown.run, stdin=approve_all())
    assert code == 0
    assert next(e for e in evts if e.get("id") == "target_github")["status"] == "warn"
    assert not any(c.startswith(("variable", "api")) for c in deletions(fake))


@pytest.mark.parametrize("command, option", [("teardown", "--allow-prod"), ("oidc", "--block-test")])
def test_cli_registration(fake, command, option):
    result = run_cli(fake, command, "--help")
    assert result.returncode == 0 and option in result.stdout and "--github-repo" in result.stdout
