"""명령줄 진입점: `python -m vigie_installer <명령> [옵션]`

    python -m vigie_installer check              # 사전 점검 (터미널용 텍스트 출력)
    python -m vigie_installer check --json       # JSON Lines 출력 (다른 프로그램이 읽을 때)
    python -m vigie_installer check --profile vigie-dev --region ap-northeast-2
    python -m vigie_installer setup              # 할당량 요청, SSM 파라미터 등록
    python -m vigie_installer deploy --alarm-email me@example.com
    python -m vigie_installer verify             # 배포 검증 (읽기 전용)
    python -m vigie_installer oidc --env dev --block-test   # GitHub Actions 자동 배포 설정
    python -m vigie_installer teardown --env dev # 정리 (되돌릴 수 없음, prod는 --allow-prod 필요)

종료 코드
    0  성공
    1  단계 실패 (점검 실패, 명령 오류 등 — 자세한 내용은 error·check 이벤트)
    2  명령줄 사용법 오류 (argparse가 stderr에 설명을 쓴다)
    3  Python 버전이 낮음 (vigie_installer/__init__.py)
    130 사용자가 Ctrl+C로 중단
"""
import argparse
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import TextIO

from .context import DEFAULT_ENV, ENVIRONMENTS, build_context
from .events import Emitter, JsonEmitter, Redactor, TextEmitter
from .runner import Interaction, Runner
from .steps import check, deploy, oidc, setup, teardown, verify


def _alarm_email(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--alarm-email", help="CloudWatch 알람을 받을 이메일 (deploy.sh의 ALARM_EMAIL). 없으면 .env의 ALARM_EMAIL")


def _admin_email(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--admin-email",
                        help="관리자 계정 이메일 (deploy.sh의 ADMIN_EMAIL). admins·approvers 그룹에 넣고, 없으면 만들어 초대 메일을 "
                             "보낸다. 없으면 .env의 ADMIN_EMAIL")


def _github_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--github-repo", help="GitHub 저장소 owner/repo (기본: 저장소 폴더의 git remote)")


def _deploy_options(parser: argparse.ArgumentParser) -> None:
    _alarm_email(parser)
    _admin_email(parser)


def _oidc_options(parser: argparse.ArgumentParser) -> None:
    _alarm_email(parser)
    _admin_email(parser)
    _github_repo(parser)
    parser.add_argument("--test-run", action="store_true",
                        help="설정 후 main으로 배포 워크플로를 실행해 dev 배포 작업이 성공하는지 확인 (실제 배포)")
    parser.add_argument("--block-test", action="store_true",
                        help="임시 브랜치에서 실행해 main 외 브랜치의 배포가 막히는지 확인")


def _teardown_options(parser: argparse.ArgumentParser) -> None:
    _github_repo(parser)
    parser.add_argument("--allow-prod", action="store_true", help="prod 환경 삭제를 허용")


# 명령 이름 → (단계 모듈의 run 함수, 설명, 명령 전용 옵션을 추가하는 함수)
COMMANDS = {
    "check": (check.run, "사전 점검 (아무것도 바꾸지 않음)", None),
    "setup": (setup.run, "API Gateway 할당량 요청과 SSM 파라미터 등록", None),
    "deploy": (deploy.run, "deploy.sh로 Vigie 배포 (20~40분)", _deploy_options),
    "verify": (verify.run, "배포 검증 (아무것도 바꾸지 않음)", None),
    "oidc": (oidc.run, "GitHub Actions 자동 배포 설정 (OIDC Role, Environment, 저장소 변수)", _oidc_options),
    "teardown": (teardown.run, "한 환경의 Vigie 리소스를 모두 삭제 (되돌릴 수 없음)", _teardown_options),
}

EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    # 모든 명령에 공통인 옵션. 명령 뒤에 쓰는 형식(`check --json`)을 쓰기 위해 각 하위 명령에 붙인다
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="이벤트를 JSON Lines로 출력 (다른 프로그램이 읽을 때)")
    common.add_argument("--dry-run", action="store_true",
                        help="상태 확인만 하고 변경 명령은 보여 주기만 함")
    common.add_argument("--yes", action="store_true",
                        help="변경 작업을 묻지 않고 승인 (되돌릴 수 없는 삭제 작업에는 적용되지 않음)")
    common.add_argument("--env", choices=ENVIRONMENTS, default=DEFAULT_ENV,
                        help=f"배포 환경 (기본 {DEFAULT_ENV})")
    common.add_argument("--region", help="AWS 리전 (기본: AWS_REGION → 프로필 설정 → ap-northeast-2)")
    common.add_argument("--profile", help="사용할 AWS CLI 프로필 (기본: AWS CLI 기본 규칙)")
    common.add_argument("--repo", help="Vigie 저장소 경로 (기본: 현재 폴더에서 상위로 찾음)")

    parser = argparse.ArgumentParser(prog="vigie_installer", description="Vigie 설치 마법사 단계 엔진")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<명령>")
    for name, (_, help_text, add_options) in COMMANDS.items():
        command = commands.add_parser(name, parents=[common], help=help_text, description=help_text)
        if add_options:
            add_options(command)
    return parser


def _raise_interrupt(signum: int, frame) -> None:
    # 부모 프로세스가 SIGTERM을 보내도 Ctrl+C(SIGINT)와 똑같이 처리한다. 기본 동작(즉시 종료)대로 두면
    # deploy.sh처럼 따로 실행 중인 자식 프로세스 그룹이 남아 배포를 계속한다 (runner.py 모듈 설명 참고)
    raise KeyboardInterrupt


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
         environ: dict[str, str] | None = None, cwd: Path | None = None) -> int:
    """테스트에서 입출력·환경을 바꿔 끼울 수 있도록 인자로 받는다 (기본은 실제 프로세스의 값)."""
    args = build_parser().parse_args(argv)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    environ = dict(os.environ if environ is None else environ)

    redactor = Redactor()
    emitter: Emitter = (JsonEmitter(redactor, stdout) if args.json
                        else TextEmitter(redactor, stdout, dry_run=args.dry_run))
    try:
        ctx = build_context(env=args.env, region=args.region, profile=args.profile, repo=args.repo,
                            environ=environ, cwd=cwd or Path.cwd(),
                            alarm_email=getattr(args, "alarm_email", None),
                            admin_email=getattr(args, "admin_email", None),
                            github_repo=getattr(args, "github_repo", None),
                            allow_prod=getattr(args, "allow_prod", False),
                            test_run=getattr(args, "test_run", False),
                            block_test=getattr(args, "block_test", False))
        interaction = Interaction(emitter, json_mode=args.json, stdin=stdin, prompt_stream=stdout)
        runner = Runner(emitter, interaction, env=ctx.command_env(),
                        cwd=str(ctx.repo_root) if ctx.repo_root else None,
                        dry_run=args.dry_run, assume_yes=args.yes)
        step_run, _, _ = COMMANDS[args.command]
        return step_run(ctx, runner, emitter)
    except KeyboardInterrupt:
        emitter.error(args.command, "사용자가 중단했습니다")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - 어떤 예외든 읽는 쪽이 이해할 수 있는 error 이벤트로 바꾼다
        # 예외 메시지·스택에도 비밀 값이 섞일 수 있으므로 Redactor를 거친다.
        # 스택은 stdout(JSON Lines 통로)을 깨뜨리지 않도록 stderr로 보낸다.
        emitter.error(args.command, f"예상하지 못한 오류: {type(exc).__name__}: {exc}",
                      hint="설치 마법사의 버그일 수 있습니다. stderr의 상세 내용과 함께 알려 주세요")
        sys.stderr.write(redactor.redact(traceback.format_exc()))
        return 1


if __name__ == "__main__":
    # 신호 처리기는 실제 실행할 때만 설치한다 (테스트에서 main()을 직접 부를 때 pytest의 처리기를 바꾸지 않도록)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    sys.exit(main())
