"""실행 환경(context.py): 리전 결정 순서(deploy.sh와 동일), 프로필과 자격 증명 환경 변수, 저장소 찾기"""
import pytest

from .helpers import ROOT
from vigie_installer.context import build_context, find_repo_root


def context(fake, repo, *, region=None, profile=None, **environ):
    return build_context(env="dev", region=region, profile=profile, repo=None,
                         environ=fake.env(**environ), cwd=repo)


def test_region_option_wins(fake, repo):
    fake.add("aws", "configure get region", "us-west-2\n")
    ctx = context(fake, repo, region="eu-west-1", AWS_REGION="ap-southeast-1")
    assert (ctx.region, ctx.region_source) == ("eu-west-1", "option")


def test_region_env_before_profile(fake, repo):
    fake.add("aws", "configure get region", "us-west-2\n")
    ctx = context(fake, repo, AWS_REGION="ap-southeast-1")
    assert (ctx.region, ctx.region_source) == ("ap-southeast-1", "AWS_REGION")


def test_region_from_profile(fake, repo):
    fake.add("aws", "configure get region --profile vigie-dev", "us-west-2\n")
    ctx = context(fake, repo, profile="vigie-dev")
    assert (ctx.region, ctx.region_source) == ("us-west-2", "profile")


@pytest.mark.parametrize("configure", ["unset", "missing_aws"])
def test_region_defaults_to_seoul(fake, repo, configure):
    if configure == "unset":
        fake.add("aws", "configure get region", exit=1)   # 설정 없음
    ctx = context(fake, repo)
    assert (ctx.region, ctx.region_source) == ("ap-northeast-2", "default")


def test_region_order_matches_deploy_sh():
    # deploy.sh의 리전 결정 순서가 바뀌면 설치 마법사가 점검한 리전과 실제 배포 리전이 어긋난다
    deploy = (ROOT / "deploy.sh").read_text()
    assert "REGION=${AWS_REGION:-$(aws configure get region || true)}" in deploy
    assert "REGION=${REGION:-ap-northeast-2}" in deploy


def test_command_env_sets_region_and_disables_pager(fake, repo):
    ctx = context(fake, repo, region="ap-northeast-2")
    env = ctx.command_env()
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"] == "ap-northeast-2"
    assert env["AWS_PAGER"] == "" and env["GH_PROMPT_DISABLED"] == "1"


def test_profile_removes_credentials_from_environment(fake, repo):
    # 환경 변수의 키가 남아 있으면 AWS CLI가 프로필 대신 그 키를 써서 다른 계정에 배포할 수 있다
    fake.add("aws", "configure get region", exit=1)
    ctx = context(fake, repo, profile="vigie-dev", AWS_ACCESS_KEY_ID="AKIAOTHER", AWS_SECRET_ACCESS_KEY="x",
                  AWS_SESSION_TOKEN="y")
    env = ctx.command_env()
    assert env["AWS_PROFILE"] == "vigie-dev"
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & env.keys()


def test_without_profile_environment_credentials_are_kept(fake, repo):
    ctx = context(fake, repo, AWS_ACCESS_KEY_ID="AKIAEXAMPLE")
    assert ctx.command_env()["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"


def test_find_repo_root_walks_up(repo):
    nested = repo / "cloudformation" / "nested"
    nested.mkdir()
    assert find_repo_root(nested) == repo


def test_find_repo_root_returns_none_outside_repo(tmp_path):
    assert find_repo_root(tmp_path) is None


def test_explicit_repo_is_not_searched_upward(fake, repo):
    # --repo로 하위 폴더를 잘못 지정했을 때 상위 저장소를 몰래 고르지 않는다
    ctx = build_context(env="dev", region="ap-northeast-2", profile=None, repo=str(repo / "cloudformation"),
                        environ=fake.env(), cwd=repo)
    assert ctx.repo_root is None and ctx.repo_requested == (repo / "cloudformation").resolve()


def test_real_repository_is_detected():
    assert find_repo_root(ROOT / "installer" / "core") == ROOT
    ctx = build_context(env="dev", region="ap-northeast-2", profile=None, repo=None, environ={}, cwd=ROOT)
    assert ctx.repo_root == ROOT and ctx.ssm_prefix == "/vigie/dev"


def test_emails_come_from_dotenv_when_options_are_missing(tmp_path):
    # deploy.sh와 같은 순서: 명령줄 옵션이 없으면 저장소 루트 .env의 값을 쓴다
    (tmp_path / ".env").write_text('ADMIN_EMAIL="admin@example.com"\nALARM_EMAIL=alarm@example.com\n')
    (tmp_path / "deploy.sh").write_text("#!/bin/bash\n")
    (tmp_path / "cloudformation").mkdir()
    ctx = build_context(env="dev", region="ap-northeast-2", profile=None, repo=str(tmp_path), environ={},
                        cwd=tmp_path)
    assert ctx.repo_root == tmp_path.resolve()
    assert (ctx.admin_email, ctx.alarm_email) == ("admin@example.com", "alarm@example.com")
    # 명령줄 옵션이 먼저다
    ctx = build_context(env="dev", region="ap-northeast-2", profile=None, repo=str(tmp_path), environ={},
                        cwd=tmp_path, admin_email="cli@example.com")
    assert ctx.admin_email == "cli@example.com"
