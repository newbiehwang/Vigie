"""oidc: 배포 Role 스택, GitHub Environment, 저장소 변수, 워크플로 확인. 가짜 aws·gh로 명령과 순서를 확인한다"""
import json
from datetime import datetime, timezone

import pytest

from vigie_installer.steps import oidc

from .helpers import ROOT, responses, run_step

REPO = "octo/WGA_production"
ROLE_ARN = "arn:aws:iam::123456789012:role/vigie-github-deploy-dev"
PROVIDER = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
TEMPLATE_TEXT = (ROOT / "cloudformation" / "github-oidc.yaml").read_text()
NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(oidc, "POLL_INTERVAL", 0)
    monkeypatch.setattr(oidc, "RUN_APPEAR_TIMEOUT", 2)
    monkeypatch.setattr(oidc, "BLOCK_TEST_TIMEOUT", 2)
    monkeypatch.setattr(oidc, "TEST_RUN_TIMEOUT", 2)


@pytest.fixture
def repo_dir(repo):
    # 실제 템플릿을 가짜 저장소에 복사한다 (배포한 템플릿과 같은지 비교하기 위해)
    (repo / "cloudformation" / "github-oidc.yaml").write_text(TEMPLATE_TEXT)
    return repo


def stack(env="dev", provider="", status="CREATE_COMPLETE", repo=REPO):
    return {"StackName": f"vigie-github-oidc-{env}", "StackStatus": status,
            "Parameters": [{"ParameterKey": "Environment", "ParameterValue": env},
                           {"ParameterKey": "GitHubRepository", "ParameterValue": repo},
                           {"ParameterKey": "ExistingOidcProviderArn", "ParameterValue": provider}],
            "Outputs": [{"OutputKey": "DeployRoleArn", "OutputValue": ROLE_ARN.replace("-dev", f"-{env}")}]}


def github(fake, *, admin=True, environment=None, policies=None, variables=None, user_id=42):
    """gh가 보는 GitHub 상태. environment=None이면 Environment 없음(404)."""
    fake.add("gh", "api -X", "{}")   # 변경 API 호출 (PUT·POST·DELETE)은 성공한 것으로
    fake.add("gh", "auth status", "Logged in to github.com account octo\n")
    fake.add("gh", "repo view", json.dumps({"nameWithOwner": REPO}))
    fake.add("gh", "variable list", json.dumps([{"name": k, "value": v} for k, v in (variables or {}).items()]))
    fake.add("gh", "variable set")
    fake.add("gh", "deployment-branch-policies", json.dumps(
        {"branch_policies": [{"name": name} for name in (policies or [])]}))
    if environment is None:
        fake.add("gh", f"api repos/{REPO}/environments/", stderr="gh: Not Found (HTTP 404)\n", exit=1)
    else:
        fake.add("gh", f"api repos/{REPO}/environments/", json.dumps(environment))
    fake.add("gh", "api user", json.dumps({"id": user_id, "login": "octo"}))
    fake.add("gh", f"api repos/{REPO}/git/ref/heads/main", json.dumps({"object": {"sha": "abc123"}}))
    # 가장 짧은 경로는 마지막에 (먼저 추가한 규칙이 우선이라, 앞에 두면 위의 구체적인 경로까지 가로챈다)
    fake.add("gh", f"api repos/{REPO}", json.dumps({"permissions": {"admin": admin}}))


def aws(fake, *, before=None, after=None, providers=(), owns_provider=False, same_template=True):
    """aws가 보는 계정 상태. before: 처음 조회한 스택(None이면 없음), after: 배포 뒤 조회되는 스택."""
    if before is None:
        fake.add("aws", "describe-stacks", stderr="Stack with id vigie-github-oidc-dev does not exist\n", exit=254,
                 times=1)
    else:
        fake.add("aws", "describe-stacks", json.dumps({"Stacks": [before]}), times=1)
    fake.add("aws", "describe-stacks", json.dumps({"Stacks": [after or before or stack()]}))
    if owns_provider:
        fake.add("aws", "describe-stack-resource", json.dumps({"StackResourceDetail": {"LogicalResourceId": "x"}}))
    else:
        fake.add("aws", "describe-stack-resource",
                 stderr="Resource GitHubOidcProvider does not exist for stack vigie-github-oidc-dev\n", exit=254)
    fake.add("aws", "list-open-id-connect-providers",
             json.dumps({"OpenIDConnectProviderList": [{"Arn": arn} for arn in providers]}))
    fake.add("aws", "get-template", json.dumps({"TemplateBody": TEMPLATE_TEXT if same_template else "old"}))
    fake.add("aws", "cloudformation deploy", "Successfully created/updated stack\n")


GOOD_ENV = {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            "protection_rules": [{"type": "branch_policy"}]}


def mutating(fake):
    verbs = ("cloudformation deploy", "variable set", "api -X", "workflow run", "run cancel")
    return [(c["tool"], " ".join(c["args"])) for c in fake.calls() if any(v in " ".join(c["args"]) for v in verbs)]


def _dump(evts):
    """실패했을 때 원인을 보기 쉽게 오류와 단계 결과만 추린다."""
    return [e for e in evts if e['type'] in ('error','step_finished')]


def finished(evts):
    return {e["step"]: e for e in evts if e["type"] == "step_finished"}


FRESH = responses(("confirm", "deploy_oidc_role"), ("confirm", "github_environment"),
                  ("confirm", "variable_AWS_REGION"), ("confirm", "variable_AWS_DEPLOY_ROLE_ARN_DEV"))


def test_fresh_setup(fake, repo_dir):
    aws(fake, before=None, after=stack())
    github(fake)
    code, evts = run_step(fake, oidc.run, stdin=FRESH, repo=repo_dir)
    assert code == 0, evts

    calls = mutating(fake)
    deploy = calls[0][1]
    assert "--parameter-overrides Environment=dev GitHubRepository=octo/WGA_production ExistingOidcProviderArn=" in deploy
    assert deploy.endswith("--tags Project=Vigie Environment=dev")
    assert calls[1:] == [
        ("gh", f"api -X PUT repos/{REPO}/environments/dev --input {calls[1][1].split('--input ')[1]}"),
        ("gh", f"api -X POST repos/{REPO}/environments/dev/deployment-branch-policies -f name=main -f type=branch"),
        ("gh", f"variable set AWS_REGION --body ap-northeast-2 --repo {REPO}"),
        # Role 변수는 마지막: 등록되는 순간부터 main push가 배포를 일으킨다
        ("gh", f"variable set AWS_DEPLOY_ROLE_ARN_DEV --body {ROLE_ARN} --repo {REPO}")]
    body = json.loads(fake.captures()[0][1])
    assert body == {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}

    confirm = next(e for e in evts if e["type"] == "confirm_required" and e["id"] == "variable_AWS_DEPLOY_ROLE_ARN_DEV")
    assert "main에 push할 때마다" in confirm["reason"]


def test_rerun_changes_nothing(fake, repo_dir):
    aws(fake, before=stack())
    github(fake, environment=GOOD_ENV, policies=["main"],
           variables={"AWS_REGION": "ap-northeast-2", "AWS_DEPLOY_ROLE_ARN_DEV": ROLE_ARN})
    code, evts = run_step(fake, oidc.run, repo=repo_dir)
    assert code == 0 and mutating(fake) == []
    # 이미 되어 있으면 할 일을 마친 상태(ok)이고, 요약 없이 [완료]로만 보인다
    assert {s: (e["status"], e["summary"]) for s, e in finished(evts).items() if s != "oidc"} == {
        "oidc_role": ("ok", ""), "github_environment": ("ok", ""), "github_variables": ("ok", "")}


def test_template_change_is_redeployed(fake, repo_dir):
    aws(fake, before=stack(), same_template=False)
    github(fake, environment=GOOD_ENV, policies=["main"],
           variables={"AWS_REGION": "ap-northeast-2", "AWS_DEPLOY_ROLE_ARN_DEV": ROLE_ARN})
    code, _ = run_step(fake, oidc.run, stdin=responses(("confirm", "deploy_oidc_role")), repo=repo_dir)
    assert code == 0 and [c for c in mutating(fake) if "cloudformation deploy" in c[1]]


def test_existing_provider_of_other_env_is_reused(fake, repo_dir):
    aws(fake, before=None, after=stack(provider=PROVIDER), providers=[PROVIDER])
    github(fake)
    run_step(fake, oidc.run, stdin=FRESH, repo=repo_dir)
    assert f"ExistingOidcProviderArn={PROVIDER}" in mutating(fake)[0][1]


def test_stack_that_owns_provider_keeps_it(fake, repo_dir):
    # 이 스택이 공급자를 만들었다면 ARN을 넘기면 안 된다 (넘기면 CloudFormation이 공급자를 지운다)
    aws(fake, before=stack(repo="octo/old-name"), providers=[PROVIDER], owns_provider=True)
    github(fake, environment=GOOD_ENV, policies=["main"],
           variables={"AWS_REGION": "ap-northeast-2", "AWS_DEPLOY_ROLE_ARN_DEV": ROLE_ARN})
    run_step(fake, oidc.run, stdin=responses(("confirm", "deploy_oidc_role")), repo=repo_dir)
    deploy = mutating(fake)[0][1]
    assert "ExistingOidcProviderArn= " in deploy + " " and PROVIDER not in deploy


def test_prod_adds_self_as_reviewer_and_keeps_existing(fake, repo_dir):
    existing = {"deployment_branch_policy": None, "protection_rules": [
        {"type": "required_reviewers", "prevent_self_review": False,
         "reviewers": [{"type": "Team", "reviewer": {"id": 7}}]},
        {"type": "wait_timer", "wait_timer": 5}]}
    aws(fake, before=None, after=stack(env="prod"))
    github(fake, environment=existing, policies=[], user_id=42)
    code, _ = run_step(fake, oidc.run, env="prod", repo=repo_dir, stdin=responses(
        ("confirm", "deploy_oidc_role"), ("confirm", "github_environment"), ("confirm", "variable_AWS_REGION"),
        ("confirm", "variable_AWS_DEPLOY_ROLE_ARN_PROD")))
    assert code == 0
    body = json.loads(fake.captures()[0][1])
    assert body["reviewers"] == [{"type": "Team", "id": 7}, {"type": "User", "id": 42}]
    assert body["wait_timer"] == 5 and body["prevent_self_review"] is False


@pytest.mark.parametrize("setup, message", [
    ({"env": "test"}, "deploy.yml에는 test 배포 작업이 없습니다"),
    ({"admin": False}, "관리자 권한이 없습니다"),
    ({"logged_out": True}, "로그인되어 있지 않습니다"),
])
def test_preconditions(fake, repo_dir, setup, message):
    if setup.get("logged_out"):
        fake.add("gh", "auth status", stderr="not logged in\n", exit=1)
    github(fake, admin=setup.get("admin", True))
    aws(fake)
    code, evts = run_step(fake, oidc.run, env=setup.get("env", "dev"), repo=repo_dir)
    assert code == 1 and mutating(fake) == []
    assert any(message in e.get("message", "") for e in evts if e["type"] == "error")


def test_failed_first_creation_must_be_deleted(fake, repo_dir):
    aws(fake, before=stack(status="ROLLBACK_COMPLETE"))
    github(fake)
    code, evts = run_step(fake, oidc.run, repo=repo_dir)
    error = next(e for e in evts if e["type"] == "error")
    assert code == 1 and "delete-stack" in error["hint"] and mutating(fake) == []


def test_extra_branch_policy_is_reported_not_removed(fake, repo_dir):
    aws(fake, before=stack())
    github(fake, environment=GOOD_ENV, policies=["main", "feature/*"],
           variables={"AWS_REGION": "ap-northeast-2", "AWS_DEPLOY_ROLE_ARN_DEV": ROLE_ARN})
    code, evts = run_step(fake, oidc.run, repo=repo_dir)
    assert code == 0 and mutating(fake) == []
    assert any("feature/*" in e.get("line", "") for e in evts if e["type"] == "log")


def test_shared_region_variable_is_kept_by_default(fake, repo_dir):
    # AWS_REGION은 prod 배포도 쓰므로 이미 다른 값이면 묻고, 기본은 유지한다
    aws(fake, before=stack())
    github(fake, environment=GOOD_ENV, policies=["main"], variables={"AWS_REGION": "us-west-2"})
    code, evts = run_step(fake, oidc.run, repo=repo_dir,
                          stdin=responses(("choice", "variable_AWS_REGION", "keep"),
                                          ("confirm", "variable_AWS_DEPLOY_ROLE_ARN_DEV")))
    assert code == 0, _dump(evts)
    assert [c[1] for c in mutating(fake)] == [f"variable set AWS_DEPLOY_ROLE_ARN_DEV --body {ROLE_ARN} --repo {REPO}"]


def test_admin_email_is_registered_as_a_variable(fake, repo_dir):
    # GitHub Actions 배포도 같은 관리자 계정을 쓰도록 저장소 변수 ADMIN_EMAIL로 등록한다 (deploy.yml이 넘긴다)
    aws(fake, before=stack())
    github(fake, environment=GOOD_ENV, policies=["main"], variables={"AWS_REGION": "ap-northeast-2"})
    code, evts = run_step(fake, oidc.run, repo=repo_dir, admin_email="admin@example.com",
                          stdin=responses(("confirm", "variable_ADMIN_EMAIL"),
                                          ("confirm", "variable_AWS_DEPLOY_ROLE_ARN_DEV")))
    assert code == 0, _dump(evts)
    assert [c[1] for c in mutating(fake)] == [
        f"variable set ADMIN_EMAIL --body admin@example.com --repo {REPO}",
        f"variable set AWS_DEPLOY_ROLE_ARN_DEV --body {ROLE_ARN} --repo {REPO}"]


def test_declining_role_variable_means_not_done(fake, repo_dir):
    aws(fake, before=stack())
    github(fake, environment=GOOD_ENV, policies=["main"], variables={"AWS_REGION": "ap-northeast-2"})
    code, evts = run_step(fake, oidc.run, repo=repo_dir,
                          stdin=responses(("confirm", "variable_AWS_DEPLOY_ROLE_ARN_DEV", False)))
    assert code == 1 and "자동 배포는 꺼져" in finished(evts)["github_variables"]["summary"]


def test_dry_run_changes_nothing(fake, repo_dir):
    aws(fake, before=None)
    github(fake)
    code, evts = run_step(fake, oidc.run, repo=repo_dir, dry_run=True, block_test=True)
    assert code == 0 and mutating(fake) == [], _dump(evts)
    dry = [e["id"] for e in evts if e["type"] == "dry_run"]
    assert dry == ["deploy_oidc_role", "github_environment", "variable_AWS_REGION",
                   "variable_AWS_DEPLOY_ROLE_ARN_DEV", "block_test"]
    role_var = next(e for e in evts if e["type"] == "dry_run" and e["id"] == "variable_AWS_DEPLOY_ROLE_ARN_DEV")
    assert "<Role 스택 배포 후 정해짐>" in role_var["command"]


# ---------------------------------------------------------------- 워크플로 확인

def configured(fake, *, alarm=None):
    aws(fake, before=stack())
    variables = {"AWS_REGION": "ap-northeast-2", "AWS_DEPLOY_ROLE_ARN_DEV": ROLE_ARN}
    github(fake, environment=GOOD_ENV, policies=["main"], variables=variables)
    fake.add("gh", "workflow run")
    fake.add("gh", "run cancel")


def runs(branch):
    return json.dumps([{"databaseId": 99, "createdAt": NOW, "url": f"https://github.com/{REPO}/actions/runs/99"}])


def job(status, conclusion=None, steps=()):
    return json.dumps({"status": status, "conclusion": conclusion, "url": "u", "jobs": [
        {"name": "dev 배포", "status": status, "conclusion": conclusion, "steps": list(steps)}]})


def test_block_test_passes_when_environment_rule_blocks(fake, repo_dir):
    configured(fake)
    fake.add("gh", "run list", runs("x"))
    fake.add("gh", "run view", job("completed", "failure", steps=()))
    code, evts = run_step(fake, oidc.run, repo=repo_dir, block_test=True,
                          stdin=responses(("confirm", "block_test")))
    calls = [c[1] for c in mutating(fake)]
    assert code == 0, evts
    assert calls[0].startswith(f"api -X POST repos/{REPO}/git/refs -f ref=refs/heads/vigie-installer-block-test-")
    assert calls[1].startswith("workflow run deploy.yml --ref vigie-installer-block-test-")
    assert calls[-1].startswith(f"api -X DELETE repos/{REPO}/git/refs/heads/vigie-installer-block-test-")
    assert not any("run cancel" in c for c in calls)


def test_block_test_cancels_run_that_was_not_blocked(fake, repo_dir):
    configured(fake)
    fake.add("gh", "run list", runs("x"))
    fake.add("gh", "run view", job("in_progress", steps=[{"name": "Set up job"}]))
    code, evts = run_step(fake, oidc.run, repo=repo_dir, block_test=True,
                          stdin=responses(("confirm", "block_test")))
    calls = [c[1] for c in mutating(fake)]
    assert code == 1
    assert f"run cancel 99 --repo {REPO}" in calls
    assert calls[-1].startswith(f"api -X DELETE repos/{REPO}/git/refs/heads/")   # 임시 브랜치는 항상 지운다
    assert any("배포 작업이 시작되었습니다" in e.get("message", "") for e in evts if e["type"] == "error")


def test_block_test_deletes_branch_even_when_run_not_found(fake, repo_dir):
    configured(fake)
    fake.add("gh", "run list", "[]")
    code, _ = run_step(fake, oidc.run, repo=repo_dir, block_test=True, stdin=responses(("confirm", "block_test")))
    assert code == 1 and mutating(fake)[-1][1].startswith(f"api -X DELETE repos/{REPO}/git/refs/heads/")


def test_test_run_waits_for_dev_job(fake, repo_dir):
    configured(fake)
    fake.add("gh", "run list", runs("main"))
    fake.add("gh", "run view", job("in_progress", steps=[{"name": "deploy"}]), times=1)
    fake.add("gh", "run view", job("completed", "success", steps=[{"name": "deploy"}]))
    code, evts = run_step(fake, oidc.run, repo=repo_dir, test_run=True, stdin=responses(("confirm", "workflow_run")))
    assert code == 0, evts
    assert [c[1] for c in mutating(fake)] == [f"workflow run deploy.yml --ref main --repo {REPO}"]
    assert "prod 배포는 GitHub에서 승인하면" in finished(evts)["workflow_test"]["summary"]


def test_test_run_reports_failed_job(fake, repo_dir):
    configured(fake)
    fake.add("gh", "run list", runs("main"))
    fake.add("gh", "run view", job("completed", "failure", steps=[{"name": "deploy"}]))
    code, evts = run_step(fake, oidc.run, repo=repo_dir, test_run=True, stdin=responses(("confirm", "workflow_run")))
    assert code == 1 and finished(evts)["workflow_test"]["status"] == "failed"


def test_job_names_match_deploy_workflow():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    from vigie_installer.github import JOB_NAMES, role_variable
    for env, name in JOB_NAMES.items():
        assert f"name: {name}" in workflow and f"vars.{role_variable(env)}" in workflow
