"""GitHub 조회 도우미 (gh CLI). 모두 읽기 전용이다 (Runner.run만 쓴다).

oidc·teardown 단계가 저장소 이름, 로그인 상태, 권한, 저장소 변수, Environment 설정을 확인할 때 쓴다.
"""
import json
import re
from typing import Any

from .context import Context
from .runner import CommandResult, Runner

# cloudformation/github-oidc.yaml의 GitHubRepository AllowedPattern과 같다
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

WORKFLOW = "deploy.yml"      # .github/workflows/deploy.yml
DEPLOY_BRANCH = "main"       # 배포를 허용할 유일한 브랜치
# deploy.yml의 작업 이름 (환경 → 작업 표시 이름). 워크플로 실행 결과에서 작업을 찾을 때 쓴다
JOB_NAMES = {"dev": "dev 배포", "prod": "prod 배포 (승인 필요)"}


def role_variable(env: str) -> str:
    """deploy.yml이 읽는 배포 Role 변수 이름. 이 변수가 있어야 해당 환경 배포 작업이 실행된다."""
    return f"AWS_DEPLOY_ROLE_ARN_{env.upper()}"


def gh_json(runner: Runner, *args: str, timeout: float = 60, cwd: str | None = None) -> tuple[Any, CommandResult]:
    """`gh <args>`를 실행해 (해석한 JSON, 실행 결과)를 돌려준다. 실패하거나 JSON이 아니면 첫 값이 None."""
    result = runner.run(["gh", *args], timeout=timeout, cwd=cwd)
    if not result.ok:
        return None, result
    try:
        return json.loads(result.stdout or "null"), result
    except json.JSONDecodeError:
        return None, result


def is_not_found(result: CommandResult) -> bool:
    """gh api가 404로 실패했는지 (Environment가 아직 없는 경우 등)."""
    return "HTTP 404" in (result.stderr + result.stdout)


def error_text(result: CommandResult) -> str:
    lines = [line for line in (result.stderr or result.stdout).strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"종료 코드 {result.returncode}"


def check_login(runner: Runner) -> str | None:
    """로그인되어 있으면 None, 아니면 오류 설명."""
    result = runner.run(["gh", "auth", "status", "--hostname", "github.com"], timeout=30)
    if result.returncode == 127:
        return "GitHub CLI(gh)가 설치되어 있지 않습니다"
    if not result.ok:
        return "GitHub에 로그인되어 있지 않습니다"
    return None


def resolve_repo(ctx: Context, runner: Runner) -> tuple[str | None, str | None]:
    """(owner/repo, 오류). --github-repo를 주지 않으면 저장소 폴더의 git remote로 gh가 알아낸다."""
    if ctx.github_repo:
        if not REPO_PATTERN.match(ctx.github_repo):
            return None, f"'{ctx.github_repo}'은(는) owner/repo 형식이 아닙니다"
        return ctx.github_repo, None
    cwd = str(ctx.repo_root) if ctx.repo_root else None
    data, result = gh_json(runner, "repo", "view", "--json", "nameWithOwner", cwd=cwd)
    if not data or not data.get("nameWithOwner"):
        return None, f"GitHub 저장소를 알아내지 못했습니다: {error_text(result)}"
    return data["nameWithOwner"], None


def is_admin(runner: Runner, repo: str) -> tuple[bool | None, str | None]:
    """(관리자 여부, 오류). Environment와 저장소 변수를 바꾸려면 저장소 관리자 권한이 필요하다."""
    data, result = gh_json(runner, "api", f"repos/{repo}")
    if data is None:
        return None, error_text(result)
    return bool(data.get("permissions", {}).get("admin")), None


def variables(runner: Runner, repo: str) -> tuple[dict[str, str] | None, str | None]:
    """저장소 변수 {이름: 값} (Actions variables, 비밀이 아니라 값이 보인다)."""
    data, result = gh_json(runner, "variable", "list", "--repo", repo, "--json", "name,value")
    if data is None:
        return None, error_text(result)
    return {item["name"]: item.get("value", "") for item in data}, None


def environment(runner: Runner, repo: str, env: str) -> tuple[dict | None, bool, str | None]:
    """(Environment 설정, 존재 여부, 오류)."""
    data, result = gh_json(runner, "api", f"repos/{repo}/environments/{env}")
    if data is None:
        if is_not_found(result):
            return None, False, None
        return None, False, error_text(result)
    return data, True, None


def branch_policies(runner: Runner, repo: str, env: str) -> tuple[list[str] | None, str | None]:
    """Environment의 배포 브랜치 규칙 이름 목록 (예: ["main"])."""
    data, result = gh_json(runner, "api", f"repos/{repo}/environments/{env}/deployment-branch-policies")
    if data is None:
        if is_not_found(result):
            return [], None   # 사용자 지정 규칙을 켜지 않은 Environment
        return None, error_text(result)
    return [policy.get("name", "") for policy in data.get("branch_policies", [])], None
