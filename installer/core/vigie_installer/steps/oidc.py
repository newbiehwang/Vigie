"""oidc: GitHub Actions 자동 배포 설정 (계획서 4.5절)

main에 push하면 GitHub Actions(.github/workflows/deploy.yml)가 장기 Access Key 없이 배포하도록 만든다.

1. oidc_role           cloudformation/github-oidc.yaml로 배포 Role 스택(vigie-github-oidc-<env>)을 배포한다.
2. github_environment  GitHub Environment(<env>)를 만들고 배포 브랜치를 main으로 제한한다. prod는 본인을 필수 검토자로.
3. github_variables    저장소 변수 AWS_REGION, (ALARM_EMAIL), (ADMIN_EMAIL), AWS_DEPLOY_ROLE_ARN_<ENV>를 등록한다.
                       Role 변수가 등록되는 순간부터 main push가 실제 배포를 일으킨다 (그래서 마지막에 한다).
4. workflow_test       (--test-run) main으로 워크플로를 실행해 dev 배포 작업이 성공하는지 본다.
5. block_test          (--block-test) 임시 브랜치에서 실행해 Environment 보호 규칙이 배포를 막는지 본다.

OIDC 공급자는 계정에 URL당 하나만 만들 수 있어 여러 환경이 함께 쓴다.
- 처음 배포하는 환경의 스택이 공급자를 만들고, 다른 환경은 ExistingOidcProviderArn으로 그것을 가리킨다.
- 공급자를 만든 스택을 다시 배포할 때 ExistingOidcProviderArn을 넘기면 CloudFormation이 그 스택의 공급자
  리소스를 지워 버린다 (조건이 바뀌어 리소스가 빠지므로). 그래서 "공급자가 이 스택 소속인지"를 먼저 확인한다.
- teardown은 다른 환경이 이 공급자를 쓰고 있으면 공급자를 가진 스택을 지우지 않는다.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import github
from ..aws import aws_json, error_text, is_not_found, oidc_stack, parse_time, stack_failures
from ..context import Context
from ..events import STEP_FAILED, STEP_OK, STEP_SKIPPED, Emitter, format_command
from ..runner import DECLINED, DRY_RUN, EXECUTED, Runner, secret_file

STEP = "oidc"
SUPPORTED_ENVS = ("dev", "prod")   # deploy.yml에 배포 작업이 있는 환경
TEMPLATE = Path("cloudformation") / "github-oidc.yaml"
PROVIDER_HOST = "token.actions.githubusercontent.com"
PROVIDER_LOGICAL_ID = "GitHubOidcProvider"   # github-oidc.yaml의 공급자 리소스 이름
HEALTHY = ("CREATE_COMPLETE", "UPDATE_COMPLETE")
UNKNOWN_ROLE = "<Role 스택 배포 후 정해짐>"

POLL_INTERVAL = 10              # 워크플로 실행 상태를 확인하는 간격(초)
RUN_APPEAR_TIMEOUT = 120        # `gh workflow run` 뒤 실행 기록이 나타나기를 기다리는 시간(초)
BLOCK_TEST_TIMEOUT = 5 * 60     # 차단 테스트: 작업이 막히는지 판단할 때까지 기다리는 시간(초)
TEST_RUN_TIMEOUT = 100 * 60     # 시험 실행: dev 배포 작업이 끝나기를 기다리는 시간(초). deploy.yml의 90분 + 여유
CLOCK_SKEW = timedelta(minutes=2)


def run(ctx: Context, runner: Runner, emitter: Emitter) -> int:
    emitter.step_started(STEP, f"GitHub Actions 자동 배포 설정 ({ctx.env})")
    repo = _preconditions(ctx, runner, emitter)
    if repo is None:
        emitter.step_finished(STEP, STEP_FAILED, "사전 확인에서 멈췄습니다")
        return 1

    ok, role_arn = _deploy_role(ctx, runner, emitter, repo)
    if ok:
        ok = _environment(ctx, runner, emitter, repo)
    if ok:
        ok = _variables(ctx, runner, emitter, repo, role_arn)
    if ok and ctx.test_run:
        ok = _test_run(ctx, runner, emitter, repo)
    if ok and ctx.block_test:
        ok = _block_test(ctx, runner, emitter, repo)

    if not ok:
        emitter.step_finished(STEP, STEP_FAILED, "설정을 끝내지 못했습니다. 원인을 해결하고 다시 실행하면 이어서 진행합니다")
        return 1
    emitter.step_finished(STEP, STEP_OK, f"{repo}의 main에 push하면 {ctx.env}에 배포됩니다"
                          + (" (prod는 승인 후)" if ctx.env == "prod" else ""))
    return 0


def _preconditions(ctx: Context, runner: Runner, emitter: Emitter) -> str | None:
    """이상이 없으면 owner/repo를 돌려준다."""
    if ctx.env not in SUPPORTED_ENVS:
        emitter.error(STEP, f"deploy.yml에는 {ctx.env} 배포 작업이 없습니다",
                      hint=f"--env {' 또는 --env '.join(SUPPORTED_ENVS)}로 실행하세요")
        return None
    if ctx.repo_root is None:
        emitter.error(STEP, "Vigie 저장소를 찾지 못했습니다", hint="--repo <저장소 경로>로 지정하세요")
        return None
    error = github.check_login(runner)
    if error:
        emitter.error(STEP, error, hint="brew install gh 후 gh auth login을 실행하세요")
        return None
    repo, error = github.resolve_repo(ctx, runner)
    if repo is None:
        emitter.error(STEP, error, hint="--github-repo owner/repo로 지정하세요")
        return None
    admin, error = github.is_admin(runner, repo)
    if admin is None:
        emitter.error(STEP, f"{repo} 저장소 정보를 가져오지 못했습니다", raw=error)
        return None
    if not admin:
        emitter.error(STEP, f"{repo}의 관리자 권한이 없습니다",
                      hint="Environment와 저장소 변수를 바꾸려면 관리자 권한이 필요합니다 (본인 fork에서 실행하세요)")
        return None
    return repo


# ---------------------------------------------------------------- 1. 배포 Role 스택

def _deploy_role(ctx: Context, runner: Runner, emitter: Emitter, repo: str) -> tuple[bool, str]:
    """(성공 여부, 배포 Role ARN)."""
    step = "oidc_role"
    stack_name = oidc_stack(ctx.env)
    emitter.step_started(step, f"배포 Role 스택 {stack_name}")

    stack, error = _describe_stack(runner, stack_name)
    if error:
        emitter.error(step, "스택을 조회하지 못했습니다", raw=error)
        emitter.step_finished(step, STEP_FAILED, "")
        return False, UNKNOWN_ROLE
    status = stack.get("StackStatus", "") if stack else ""
    if status.endswith("_IN_PROGRESS"):
        emitter.error(step, f"{stack_name}이(가) 작업 중입니다 ({status})", hint="끝난 뒤 다시 실행하세요")
        emitter.step_finished(step, STEP_FAILED, "")
        return False, UNKNOWN_ROLE
    if status in ("ROLLBACK_COMPLETE", "ROLLBACK_FAILED"):
        # 처음 만들다 실패한 스택은 업데이트할 수 없고, 지운 뒤 다시 만들어야 한다
        emitter.error(step, f"{stack_name}이(가) 처음 생성에 실패한 상태입니다 ({status})",
                      hint=f"aws cloudformation delete-stack --stack-name {stack_name} 로 지운 뒤 다시 실행하세요")
        emitter.step_finished(step, STEP_FAILED, "")
        return False, UNKNOWN_ROLE

    provider_arn, error = _existing_provider(ctx, runner, stack_name, stack is not None)
    if error:
        emitter.error(step, "GitHub OIDC 공급자를 확인하지 못했습니다", raw=error)
        emitter.step_finished(step, STEP_FAILED, "")
        return False, UNKNOWN_ROLE
    params = {"Environment": ctx.env, "GitHubRepository": repo, "ExistingOidcProviderArn": provider_arn}

    template_path = ctx.repo_root / TEMPLATE
    if stack and status in HEALTHY and _same_params(stack, params) and _same_template(runner, stack_name,
                                                                                         template_path):
        role_arn = _role_arn(stack)
        if role_arn:
            emitter.step_finished(step, STEP_OK, "")   # 이미 최신 = 할 일을 마친 상태
            return True, role_arn

    if provider_arn:
        emitter.log(f"계정에 있는 GitHub OIDC 공급자를 함께 씁니다: {provider_arn}", stream="info")
    cmd = ["aws", "cloudformation", "deploy", "--stack-name", stack_name, "--template-file", str(TEMPLATE),
           "--parameter-overrides", *(f"{key}={value}" for key, value in params.items()),
           "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
           "--tags", "Project=Vigie", f"Environment={ctx.env}"]
    started = datetime.now(timezone.utc)
    result = runner.change(cmd, id_="deploy_oidc_role", cwd=str(ctx.repo_root), stream=True,
                           reason=f"{repo}의 {ctx.env} Environment만 쓸 수 있는 배포 Role을 만듭니다 "
                                  "(IAM Role 생성, PowerUserAccess + vigie-* Role 관리 권한)")
    if result.outcome == DRY_RUN:
        emitter.step_finished(step, STEP_OK, "")
        return True, (_role_arn(stack) if stack else None) or UNKNOWN_ROLE
    if result.outcome == DECLINED:
        emitter.step_finished(step, STEP_SKIPPED, "")
        return False, UNKNOWN_ROLE
    if not result.ok:
        for failure in stack_failures(runner, [stack_name], started - CLOCK_SKEW):
            emitter.error(step, f"{failure.logical_id} ({failure.resource_type})", raw=failure.reason)
        emitter.error(step, "스택 배포에 실패했습니다", raw=error_text(result))
        emitter.step_finished(step, STEP_FAILED, "")
        return False, UNKNOWN_ROLE

    stack, error = _describe_stack(runner, stack_name)
    role_arn = _role_arn(stack) if stack else None
    if not role_arn:
        emitter.error(step, "스택 출력 DeployRoleArn을 읽지 못했습니다", raw=error or "출력에 DeployRoleArn이 없습니다")
        emitter.step_finished(step, STEP_FAILED, "")
        return False, UNKNOWN_ROLE
    emitter.step_finished(step, STEP_OK, f"배포 Role: {role_arn}")
    return True, role_arn


def _describe_stack(runner: Runner, name: str) -> tuple[dict | None, str | None]:
    """(스택 정보 또는 None(없음), 오류)."""
    data, result = aws_json(runner, "cloudformation", "describe-stacks", "--stack-name", name)
    if data is None:
        if "does not exist" in result.stderr:
            return None, None
        return None, error_text(result)
    stacks = data.get("Stacks", [])
    return (stacks[0] if stacks else None), None


def _existing_provider(ctx: Context, runner: Runner, stack_name: str, stack_exists: bool) -> tuple[str, str | None]:
    """(ExistingOidcProviderArn에 넘길 값, 오류). 빈 문자열이면 이 스택이 공급자를 만든다(또는 이미 가지고 있다)."""
    if stack_exists:
        _, result = aws_json(runner, "cloudformation", "describe-stack-resource", "--stack-name", stack_name,
                             "--logical-resource-id", PROVIDER_LOGICAL_ID)
        if result.ok:
            return "", None   # 이 스택이 공급자를 가지고 있다 → 계속 가지고 있어야 한다 (모듈 설명 참고)
        if not ("does not exist" in result.stderr or is_not_found(result)):
            return "", error_text(result)
    data, result = aws_json(runner, "iam", "list-open-id-connect-providers")
    if data is None:
        return "", error_text(result)
    for provider in data.get("OpenIDConnectProviderList", []):
        arn = provider.get("Arn", "")
        if arn.endswith(f"oidc-provider/{PROVIDER_HOST}"):
            return arn, None
    return "", None


def _same_params(stack: dict, params: dict[str, str]) -> bool:
    current = {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters", [])}
    return all(current.get(key, "") == value for key, value in params.items())


def _same_template(runner: Runner, stack_name: str, template_path: Path) -> bool:
    """배포된 템플릿이 저장소의 템플릿과 같은지 (다르면 템플릿 변경을 반영하기 위해 다시 배포한다)."""
    data, _ = aws_json(runner, "cloudformation", "get-template", "--stack-name", stack_name)
    body = (data or {}).get("TemplateBody")
    return isinstance(body, str) and body.strip() == template_path.read_text().strip()


def _role_arn(stack: dict) -> str | None:
    for output in stack.get("Outputs", []):
        if output.get("OutputKey") == "DeployRoleArn":
            return output.get("OutputValue")
    return None


# ---------------------------------------------------------------- 2. GitHub Environment

def _environment(ctx: Context, runner: Runner, emitter: Emitter, repo: str) -> bool:
    """Environment를 만들고 배포 브랜치를 main 하나로 제한한다. prod는 본인을 필수 검토자로 둔다.

    브랜치 제한이 필요한 이유: 배포 Role의 신뢰 정책은 "이 저장소의 <env> Environment 작업"만 확인한다.
    workflow_dispatch는 실행할 브랜치를 고를 수 있으므로, 제한이 없으면 리뷰받지 않은 브랜치의 코드로도
    <env> Environment 작업을 실행해 Role을 얻을 수 있다.
    """
    step = "github_environment"
    emitter.step_started(step, f"GitHub Environment {ctx.env}")
    current, exists, error = github.environment(runner, repo, ctx.env)
    if error:
        emitter.error(step, "Environment를 조회하지 못했습니다", raw=error)
        emitter.step_finished(step, STEP_FAILED, "")
        return False
    policies: list[str] = []
    if exists:
        policies, error = github.branch_policies(runner, repo, ctx.env)
        if policies is None:
            emitter.error(step, "배포 브랜치 규칙을 조회하지 못했습니다", raw=error)
            emitter.step_finished(step, STEP_FAILED, "")
            return False

    reviewers = _reviewers(current)
    me = None
    if ctx.env == "prod":
        user, result = github.gh_json(runner, "api", "user")
        if not user or "id" not in user:
            emitter.error(step, "로그인한 GitHub 사용자를 확인하지 못했습니다", raw=github.error_text(result))
            emitter.step_finished(step, STEP_FAILED, "")
            return False
        me = {"type": "User", "id": user["id"]}

    branch_policy = (current or {}).get("deployment_branch_policy") or {}
    custom = branch_policy.get("custom_branch_policies") is True and branch_policy.get("protected_branches") is False
    need_put = not exists or not custom or (me is not None and me not in reviewers)
    need_main = github.DEPLOY_BRANCH not in policies
    extra = [name for name in policies if name != github.DEPLOY_BRANCH]
    if extra:
        # 사용자가 직접 넣은 규칙일 수 있어 지우지 않고 알린다
        emitter.log(f"main 외의 배포 브랜치 규칙이 있습니다: {', '.join(extra)}. 그 브랜치에서도 {ctx.env}에 "
                    "배포할 수 있으니 필요 없으면 GitHub의 Environment 설정에서 지우세요", stream="info")
    if not need_put and not need_main:
        emitter.step_finished(step, STEP_OK, "")   # 이미 main 브랜치로 제한됨
        return True

    # PUT은 보호 규칙 전체를 새로 정하므로, 기존 검토자·대기 시간은 그대로 다시 넣는다
    body: dict = {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
    new_reviewers = reviewers + ([me] if me and me not in reviewers else [])
    if new_reviewers:
        body["reviewers"] = new_reviewers
    for rule in (current or {}).get("protection_rules", []):
        if rule.get("type") == "wait_timer":
            body["wait_timer"] = rule.get("wait_timer", 0)
    if current and "prevent_self_review" in _reviewer_rule(current):
        body["prevent_self_review"] = _reviewer_rule(current)["prevent_self_review"]

    put = ["gh", "api", "-X", "PUT", f"repos/{repo}/environments/{ctx.env}", "--input", "<본문 파일>"]
    post = ["gh", "api", "-X", "POST", f"repos/{repo}/environments/{ctx.env}/deployment-branch-policies",
            "-f", f"name={github.DEPLOY_BRANCH}", "-f", "type=branch"]
    shown = ([format_command(put) + f"   # 본문: {json.dumps(body, ensure_ascii=False)}"] if need_put else []) + \
            ([format_command(post)] if need_main else [])
    outcome = runner.approve("github_environment", f"{ctx.env} Environment의 배포를 main 브랜치로 제한합니다"
                             + (" (prod는 본인 승인 필요)" if me else ""), shown)
    if outcome == DRY_RUN:
        emitter.step_finished(step, STEP_OK, "")
        return True
    if outcome == DECLINED:
        emitter.step_finished(step, STEP_SKIPPED, "")
        return False

    if need_put:
        with secret_file(body) as path:
            result = runner.run_approved(put[:-1] + [path], timeout=60)
        if not result.ok:
            emitter.error(step, "Environment 설정에 실패했습니다", raw=github.error_text(result),
                          hint="비공개 저장소에서 필수 검토자를 쓰려면 유료 플랜이 필요할 수 있습니다")
            emitter.step_finished(step, STEP_FAILED, "")
            return False
    if need_main:
        result = runner.run_approved(post, timeout=60)
        if not result.ok:
            emitter.error(step, "배포 브랜치 규칙 추가에 실패했습니다", raw=github.error_text(result))
            emitter.step_finished(step, STEP_FAILED, "")
            return False
    emitter.step_finished(step, STEP_OK, "main 브랜치만 배포 · 본인 승인 필요" if me else "main 브랜치만 배포")
    return True


def _reviewer_rule(environment: dict | None) -> dict:
    for rule in (environment or {}).get("protection_rules", []):
        if rule.get("type") == "required_reviewers":
            return rule
    return {}


def _reviewers(environment: dict | None) -> list[dict]:
    """기존 필수 검토자를 PUT 본문 형식({type, id})으로."""
    return [{"type": item.get("type"), "id": item.get("reviewer", {}).get("id")}
            for item in _reviewer_rule(environment).get("reviewers", [])]


# ---------------------------------------------------------------- 3. 저장소 변수

def _variables(ctx: Context, runner: Runner, emitter: Emitter, repo: str, role_arn: str) -> bool:
    step = "github_variables"
    emitter.step_started(step, "저장소 변수")
    current, error = github.variables(runner, repo)
    if current is None:
        emitter.error(step, "저장소 변수를 조회하지 못했습니다", raw=error)
        emitter.step_finished(step, STEP_FAILED, "")
        return False

    # (이름, 원하는 값, 승인 화면의 이유). Role 변수를 마지막에 둔다: 등록되는 순간부터 main push가 배포를 일으킨다
    wanted = [("AWS_REGION", ctx.region, "배포 리전 (dev·prod 배포 작업이 함께 씁니다)")]
    if ctx.alarm_email:
        wanted.append(("ALARM_EMAIL", ctx.alarm_email, "CloudWatch 알람 수신 이메일 (dev·prod 공통)"))
    if ctx.admin_email:
        wanted.append(("ADMIN_EMAIL", ctx.admin_email, "관리자 계정 이메일: 배포할 때 admins·approvers 그룹에 넣는다 (dev·prod 공통)"))
    role_var = github.role_variable(ctx.env)
    wanted.append((role_var, role_arn, f"주의: 이 변수가 등록되면 이후 main에 push할 때마다 {ctx.env} 배포가 "
                                       "자동으로 시작됩니다 (AWS 비용 발생)"))

    changed, kept = [], []
    for name, value, reason in wanted:
        if current.get(name) == value:
            continue
        if name in current and name != role_var:
            # 다른 환경의 배포도 이 값을 쓰므로, 이미 다른 값이 있으면 바꿀지 묻는다 (기본은 유지)
            choice = runner.interaction.choose(
                f"variable_{name}", f"저장소 변수 {name}이(가) 이미 '{current[name]}'입니다. '{value}'(으)로 바꿀까요?",
                [("keep", "기존 값 유지"), ("overwrite", "바꾸기")], default="keep")
            if choice == "keep":
                kept.append(name)
                continue
        result = runner.change(["gh", "variable", "set", name, "--body", value, "--repo", repo],
                               id_=f"variable_{name}", reason=f"{name} = {value}: {reason}", timeout=60)
        if result.outcome == DECLINED:
            kept.append(name)
            continue
        if result.outcome == EXECUTED and not result.ok:
            emitter.error(step, f"{name} 등록에 실패했습니다", raw=github.error_text(result))
            emitter.step_finished(step, STEP_FAILED, "")
            return False
        changed.append(name)

    if role_var in kept:
        emitter.step_finished(step, STEP_SKIPPED, f"{role_var}을(를) 등록하지 않아 자동 배포는 꺼져 있습니다")
        return False
    if not changed:
        emitter.step_finished(step, STEP_OK, "")   # 이미 모두 등록됨
        return True
    emitter.step_finished(step, STEP_OK, ("등록 예정: " if runner.dry_run else "등록: ") + ", ".join(changed))
    return True


# ---------------------------------------------------------------- 4·5. 워크플로 실행 확인

def _find_run(runner: Runner, repo: str, branch: str, since: datetime) -> dict | None:
    """방금 수동 실행한 워크플로 실행 기록을 찾는다 (`gh workflow run`은 실행 ID를 돌려주지 않는다)."""
    deadline = time.monotonic() + RUN_APPEAR_TIMEOUT
    while time.monotonic() < deadline:
        runs, _ = github.gh_json(runner, "run", "list", "--repo", repo, "--workflow", github.WORKFLOW,
                                 "--branch", branch, "--event", "workflow_dispatch", "--limit", "5",
                                 "--json", "databaseId,createdAt,url")
        for item in runs or []:
            created = parse_time(item.get("createdAt"))
            if created and created >= since - CLOCK_SKEW:
                return item
        time.sleep(POLL_INTERVAL)
    return None


def _dev_job(runner: Runner, repo: str, run_id: int, env: str) -> tuple[dict | None, dict | None]:
    """(실행 정보, 해당 환경 배포 작업)."""
    data, _ = github.gh_json(runner, "run", "view", str(run_id), "--repo", repo, "--json",
                             "status,conclusion,jobs,url")
    if not data:
        return None, None
    job = next((j for j in data.get("jobs", []) if j.get("name") == github.JOB_NAMES[env]), None)
    return data, job


def _test_run(ctx: Context, runner: Runner, emitter: Emitter, repo: str) -> bool:
    step = "workflow_test"
    emitter.step_started(step, "배포 워크플로 시험 실행 (main)")
    started = datetime.now(timezone.utc)
    result = runner.change(["gh", "workflow", "run", github.WORKFLOW, "--ref", github.DEPLOY_BRANCH, "--repo", repo],
                           id_="workflow_run", timeout=60,
                           reason=f"main 브랜치로 배포 워크플로를 실행합니다 ({ctx.env}에 실제로 배포되고 비용이 발생합니다)")
    if result.outcome != EXECUTED:
        emitter.step_finished(step, STEP_SKIPPED if result.outcome == DECLINED else STEP_OK, "")
        return result.outcome != DECLINED
    if not result.ok:
        emitter.error(step, "워크플로를 실행하지 못했습니다", raw=github.error_text(result))
        emitter.step_finished(step, STEP_FAILED, "")
        return False

    found = _find_run(runner, repo, github.DEPLOY_BRANCH, started)
    if not found:
        emitter.error(step, "실행 기록을 찾지 못했습니다", hint="GitHub의 Actions 탭에서 직접 확인하세요")
        emitter.step_finished(step, STEP_FAILED, "")
        return False
    emitter.log(f"실행: {found.get('url')} (배포가 끝날 때까지 기다립니다. 보통 20~40분)", stream="info")

    deadline = time.monotonic() + TEST_RUN_TIMEOUT
    last_status = None
    while time.monotonic() < deadline:
        _, job = _dev_job(runner, repo, found["databaseId"], ctx.env)
        if job and job.get("status") != last_status:
            last_status = job.get("status")
            emitter.progress(step, "", f"{job.get('name')}: {last_status}")
        if job and job.get("status") == "completed":
            if job.get("conclusion") == "success":
                hint = " prod 배포는 GitHub에서 승인하면 시작됩니다" if ctx.env == "dev" else ""
                emitter.step_finished(step, STEP_OK, f"{job.get('name')} 성공.{hint}")
                return True
            emitter.error(step, f"{job.get('name')}이(가) {job.get('conclusion')}(으)로 끝났습니다",
                          hint=f"로그: {found.get('url')}")
            emitter.step_finished(step, STEP_FAILED, "")
            return False
        time.sleep(POLL_INTERVAL)
    emitter.error(step, "제한 시간 안에 배포 작업이 끝나지 않았습니다", hint=f"진행 상황: {found.get('url')}")
    emitter.step_finished(step, STEP_FAILED, "")
    return False


def _block_test(ctx: Context, runner: Runner, emitter: Emitter, repo: str) -> bool:
    """main이 아닌 브랜치에서 수동 실행했을 때 Environment 보호 규칙이 배포 작업을 막는지 확인한다.

    임시 브랜치는 main과 같은 커밋에서 만든다. 만에 하나 막히지 않아도 main과 같은 코드가 배포되고,
    그 경우 작업이 단계를 시작하는 즉시 실행을 취소한다.
    """
    step = "block_test"
    emitter.step_started(step, "main 외 브랜치 배포 차단 확인")
    ref, result = github.gh_json(runner, "api", f"repos/{repo}/git/ref/heads/{github.DEPLOY_BRANCH}")
    sha = (ref or {}).get("object", {}).get("sha")
    if not sha:
        emitter.error(step, "main 브랜치의 커밋을 찾지 못했습니다", raw=github.error_text(result))
        emitter.step_finished(step, STEP_FAILED, "")
        return False

    branch = f"vigie-installer-block-test-{int(time.time())}"
    create = ["gh", "api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}"]
    dispatch = ["gh", "workflow", "run", github.WORKFLOW, "--ref", branch, "--repo", repo]
    delete = ["gh", "api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"]
    outcome = runner.approve("block_test", f"임시 브랜치 {branch}를 만들어 배포 워크플로를 실행하고, 막히는지 확인한 뒤 "
                             "브랜치를 지웁니다 (막히지 않으면 실행을 즉시 취소합니다)",
                             [format_command(create), format_command(dispatch),
                              f"gh run cancel <실행 ID> --repo {repo}   # 막히지 않았을 때만", format_command(delete)])
    if outcome != EXECUTED:
        emitter.step_finished(step, STEP_SKIPPED if outcome == DECLINED else STEP_OK, "")
        return outcome != DECLINED

    created = runner.run_approved(create, timeout=60)
    if not created.ok:
        emitter.error(step, "임시 브랜치를 만들지 못했습니다", raw=github.error_text(created))
        emitter.step_finished(step, STEP_FAILED, "")
        return False
    try:
        return _judge_block(ctx, runner, emitter, repo, branch, dispatch)
    finally:
        # 어떤 결과든(예외 포함) 임시 브랜치는 지운다
        removed = runner.run_approved(delete, timeout=60)
        if not removed.ok:
            emitter.error(step, f"임시 브랜치 {branch}를 지우지 못했습니다", raw=github.error_text(removed),
                          hint="GitHub에서 직접 지우세요")


def _judge_block(ctx: Context, runner: Runner, emitter: Emitter, repo: str, branch: str,
                 dispatch: list[str]) -> bool:
    step = "block_test"
    started = datetime.now(timezone.utc)
    result = runner.run_approved(dispatch, timeout=60)
    if not result.ok:
        emitter.error(step, "워크플로를 실행하지 못했습니다", raw=github.error_text(result))
        emitter.step_finished(step, STEP_FAILED, "")
        return False
    found = _find_run(runner, repo, branch, started)
    if not found:
        emitter.error(step, "실행 기록을 찾지 못했습니다", hint="GitHub의 Actions 탭에서 직접 확인하세요")
        emitter.step_finished(step, STEP_FAILED, "")
        return False

    deadline = time.monotonic() + BLOCK_TEST_TIMEOUT
    while time.monotonic() < deadline:
        _, job = _dev_job(runner, repo, found["databaseId"], ctx.env)
        if job:
            # 보호 규칙에 막힌 작업은 단계를 하나도 실행하지 않고 실패로 끝난다
            if job.get("status") == "completed" and job.get("conclusion") == "failure" and not job.get("steps"):
                emitter.step_finished(step, STEP_OK, f"{branch} 브랜치의 {ctx.env} 배포가 보호 규칙에 막혔습니다")
                return True
            if job.get("conclusion") == "skipped":
                emitter.error(step, "배포 작업이 건너뛰어져 판단할 수 없습니다",
                              hint=f"저장소 변수 {github.role_variable(ctx.env)}이(가) 등록되어 있는지 확인하세요")
                emitter.step_finished(step, STEP_FAILED, "")
                return False
            if job.get("steps"):
                runner.run_approved(["gh", "run", "cancel", str(found["databaseId"]), "--repo", repo], timeout=60)
                emitter.error(step, f"{branch} 브랜치에서 {ctx.env} 배포 작업이 시작되었습니다. 실행을 취소했습니다",
                              hint="GitHub의 Environment 설정에서 배포 브랜치가 main으로만 제한되어 있는지 확인하세요")
                emitter.step_finished(step, STEP_FAILED, "")
                return False
        time.sleep(POLL_INTERVAL)
    emitter.error(step, "제한 시간 안에 판단하지 못했습니다", hint=f"진행 상황: {found.get('url')}")
    emitter.step_finished(step, STEP_FAILED, "")
    return False
