"""실행 환경 정보: 어느 환경(dev/prod)을, 어느 리전·AWS 프로필로, 어느 저장소에서 배포하는가

모든 단계가 같은 Context를 받아 쓰므로, 리전·프로필을 정하는 규칙이 한곳에만 있다.
리전 결정 순서는 deploy.sh와 똑같이 맞춘다. 설치 마법사가 점검한 리전과 deploy.sh가
실제로 배포하는 리전이 다르면, 점검은 통과했는데 다른 리전에 배포되는 일이 생기기 때문이다.

    deploy.sh:  REGION=${AWS_REGION:-$(aws configure get region)}; REGION=${REGION:-ap-northeast-2}
    여기:       --region 옵션 → AWS_REGION 환경 변수 → CLI 프로필의 region → ap-northeast-2(서울)

--region 옵션을 준 경우에도 deploy.sh를 실행할 때 AWS_REGION으로 넘기므로(command_env) 두 쪽이 어긋나지 않는다.
"""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import dotenv

ENVIRONMENTS = ("dev", "test", "prod")   # deploy.sh가 허용하는 값과 같다
DEFAULT_ENV = "dev"
DEFAULT_REGION = "ap-northeast-2"

# 리전 이름 형식 (예: ap-northeast-2, us-gov-west-1). 오타로 엉뚱한 요청이 나가기 전에 걸러 낸다
REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")

# 리전을 어디서 정했는지 (사용자에게 보여 줄 설명)
REGION_SOURCES = {
    "option": "--region 옵션",
    "AWS_REGION": "AWS_REGION 환경 변수",
    "profile": "AWS CLI 프로필 설정",
    "default": "기본값",
}

# 환경 변수로 넘어온 AWS 자격 증명. AWS CLI는 이 값이 있으면 AWS_PROFILE보다 우선해서 쓴다.
# 사용자가 프로필을 골랐는데 셸에 남아 있던 다른 계정의 키로 배포되는 일을 막기 위해,
# 프로필을 지정하면 자식 명령의 환경에서 이 변수들을 뺀다.
CREDENTIAL_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


@dataclass
class Context:
    env: str                          # dev | test | prod
    region: str
    region_source: str                # REGION_SOURCES의 키
    repo_root: Path | None            # 저장소 루트. 찾지 못하면 None (check가 실패로 보고)
    repo_requested: Path | None       # --repo로 지정한 경로 (찾지 못했을 때 안내 문구에 쓴다)
    aws_profile: str | None           # None이면 AWS CLI 기본 규칙(환경 변수·default 프로필)을 따른다
    base_environ: dict[str, str]      # 설치 마법사를 실행한 환경 변수 (자식 명령 환경의 바탕)
    alarm_email: str | None = None    # deploy·oidc: CloudWatch 알람을 받을 이메일 (deploy.sh의 ALARM_EMAIL)
    admin_email: str | None = None    # deploy·oidc: 관리자 계정(admins·approvers 그룹) 이메일 (deploy.sh의 ADMIN_EMAIL)
    github_repo: str | None = None    # oidc·teardown: owner/repo. 없으면 저장소 폴더의 git remote로 알아낸다
    allow_prod: bool = False          # teardown: prod 삭제를 허용 (없으면 prod는 거부)
    test_run: bool = False            # oidc: 설정 후 main으로 배포 워크플로를 한 번 실행해 본다
    block_test: bool = False          # oidc: 다른 브랜치에서는 배포가 막히는지 확인한다
    account_id: str | None = None     # check 단계의 sts get-caller-identity로 채운다
    caller_arn: str | None = None

    @property
    def ssm_prefix(self) -> str:
        return f"/vigie/{self.env}"

    def command_env(self) -> dict[str, str]:
        """aws·gh·deploy.sh에 넘길 환경 변수."""
        env = dict(self.base_environ)
        if self.aws_profile:
            for name in CREDENTIAL_ENV_VARS:
                env.pop(name, None)
            env["AWS_PROFILE"] = self.aws_profile
        # 두 변수를 모두 설정한다: deploy.sh는 AWS_REGION을, 일부 SDK·도구는 AWS_DEFAULT_REGION을 읽는다
        env["AWS_REGION"] = env["AWS_DEFAULT_REGION"] = self.region
        # AWS CLI v2는 출력이 길면 less 같은 페이저를 띄운다. 파이프로 실행하면 사람이 q를 누를 때까지
        # 멈춰 버리므로 페이저를 끈다.
        env["AWS_PAGER"] = ""
        # gh가 로그인·선택 질문을 띄우지 않고 바로 실패하게 한다 (stdin이 막혀 있어 답할 수 없다)
        env["GH_PROMPT_DISABLED"] = "1"
        return env


def is_repo_root(path: Path) -> bool:
    """Vigie 저장소 루트인지: deploy.sh와 cloudformation/ 폴더가 함께 있어야 한다."""
    return (path / "deploy.sh").is_file() and (path / "cloudformation").is_dir()


def find_repo_root(start: Path) -> Path | None:
    """start 폴더부터 상위로 올라가며 저장소 루트를 찾는다 (저장소 안의 하위 폴더에서 실행해도 되도록)."""
    for candidate in (start, *start.parents):
        if is_repo_root(candidate):
            return candidate
    return None


def resolve_region(option: str | None, environ: dict[str, str], profile: str | None) -> tuple[str, str]:
    """(리전, 출처)를 돌려준다. 순서는 모듈 설명 참고."""
    if option:
        return option, "option"
    if environ.get("AWS_REGION"):
        return environ["AWS_REGION"], "AWS_REGION"
    configured = _profile_region(profile, environ)
    if configured:
        return configured, "profile"
    return DEFAULT_REGION, "default"


def _profile_region(profile: str | None, environ: dict[str, str]) -> str | None:
    """`aws configure get region`으로 프로필에 설정된 리전을 읽는다.

    Runner를 쓰지 않고 subprocess를 직접 부르는 이유: Runner는 Context가 만든 환경 변수로
    만들어지는데, 그 Context를 만드는 도중이라 아직 Runner가 없다. 읽기 전용이라 dry-run과도 무관하다.
    """
    cmd = ["aws", "configure", "get", "region"]
    if profile:
        cmd += ["--profile", profile]
    try:
        proc = subprocess.run(cmd, env={**environ, "AWS_PAGER": ""}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return None   # aws가 없으면 check 단계가 따로 알려 준다. 여기서는 기본값으로 넘어간다
    # 설정되지 않은 값이면 종료 코드 1과 빈 출력이 나온다
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def build_context(*, env: str, region: str | None, profile: str | None, repo: str | None,
                  environ: dict[str, str], cwd: Path, alarm_email: str | None = None,
                  admin_email: str | None = None, github_repo: str | None = None, allow_prod: bool = False, test_run: bool = False,
                  block_test: bool = False) -> Context:
    """명령줄 옵션과 환경 변수로 Context를 만든다."""
    requested = Path(repo).expanduser().resolve() if repo else None
    if requested is not None:
        # 경로를 명시했으면 그 경로만 본다. 상위 폴더를 뒤져 다른 저장소를 고르면 사용자가 혼란스럽다
        repo_root = requested if is_repo_root(requested) else None
    else:
        repo_root = find_repo_root(cwd.resolve())
    region_value, region_source = resolve_region(region, environ, profile)
    # 이메일은 명령줄 옵션이 먼저이고, 없으면 저장소 루트 .env 값을 쓴다 (deploy.sh와 같은 순서)
    alarm_email = alarm_email or dotenv.read_value(repo_root, "ALARM_EMAIL")
    admin_email = admin_email or dotenv.read_value(repo_root, "ADMIN_EMAIL")
    return Context(env=env, region=region_value, region_source=region_source, repo_root=repo_root,
                   repo_requested=requested, aws_profile=profile, base_environ=dict(environ),
                   alarm_email=alarm_email, admin_email=admin_email, github_repo=github_repo, allow_prod=allow_prod,
                   test_run=test_run, block_test=block_test)
