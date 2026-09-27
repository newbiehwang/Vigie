"""deploy: 저장소의 deploy.sh를 실행해 Vigie를 배포한다 (계획서 4.3절)

흐름
1. 사전 확인 (읽기 전용): 저장소, 필수 SSM 파라미터, API Gateway 통합 타임아웃 할당량.
   20~40분 걸리는 배포를 시작했다가 중간에 실패하지 않도록, 결과가 뻔한 실패는 먼저 걸러 낸다.
   특히 할당량: cloudformation/llm.yaml이 통합 타임아웃을 120000ms로 설정하므로 할당량이 그보다 작으면
   LLM 스택 생성·업데이트가 실패한다.
2. 승인 후 `AWS_REGION=<리전> [ALARM_EMAIL=<이메일>] [ADMIN_EMAIL=<이메일>] ./deploy.sh <env>`를 저장소 루트에서 실행.
   출력은 한 줄씩 log 이벤트로 내보내고, 구분 줄은 progress 이벤트로 바꾼다 (ProgressParser).
3. 실패하면 CloudFormation 스택 이벤트에서 이번 배포 중 실패한 리소스와 이유를 모아 error 이벤트로 보여 준다.
4. 취소(Ctrl+C)하면 Runner가 deploy.sh와 그 자식 프로세스 전체에 중단 신호를 보낸다.
   스택 업데이트 도중이면 스택이 중간 상태로 남을 수 있음을 안내한다.

나중에 Terraform으로 바꿀 때는 deploy_command()만 `terraform apply`로 바꾸면 되도록 실행 방식을 분리해 두었다
(진행 표시 해석과 실패 요약은 배포 도구에 맞춰 따로 바꾼다).
"""
import re
from datetime import datetime, timedelta, timezone

from .. import dotenv
from ..aws import (REQUIRED_TIMEOUT_MS, SECRET_PARAMS, error_text, existing_parameters, find_timeout_quota,
                   main_stacks, stack_failures)
from ..context import Context
from ..events import STEP_FAILED, STEP_OK, STEP_SKIPPED, Emitter
from ..runner import DECLINED, DRY_RUN, RC_INTERRUPTED, Runner

STEP = "deploy"

# deploy.sh의 번호 붙은 단계 수 (`====== 1. ... ======` ~ `====== 6. ... ======`).
# deploy.sh에 단계가 늘면 tests/installer/test_installer_deploy.py가 실패해 여기를 고치게 한다.
TOTAL_PHASES = 6

# 구분 줄: `====== 2. 기본 스택 배포 시작 ======` 또는 번호 없는 `====== Layer 및 Lambda 함수 패키징 ======`
MARKER = re.compile(r"^====== (?:(\d+)\. )?(.+?) ======$")
FINISHED_LABEL = "배포 완료!"   # deploy.sh의 마지막 구분 줄

# 배포 요약에서 뽑아 보여 줄 주소 (deploy.sh의 6. 배포 완료 요약 부분 출력 형식)
SUMMARY_PATTERNS = {
    "api_url": re.compile(r"^API Gateway URL: (\S+)"),
    "frontend_url": re.compile(r"^프론트엔드 URL: (\S+)"),
}

CLOCK_SKEW = timedelta(minutes=2)   # 이 컴퓨터와 AWS의 시계 차이를 감안해 시작 시각을 조금 앞당겨 본다


def deploy_command(ctx: Context) -> list[str]:
    return ["./deploy.sh", ctx.env]


class ProgressParser:
    """deploy.sh 출력 한 줄씩을 받아 진행 표시(progress 이벤트)로 바꾼다.

    번호 없는 구분 줄은 직전 번호 단계의 하위 작업으로 보고 phase는 그대로, label만 바꾼다.
    예) `====== 3. 프론트엔드 인프라 배포 ======` → 3/6 프론트엔드 인프라 배포
        `====== Layer 및 Lambda 함수 패키징 ======` → 3/6 Layer 및 Lambda 함수 패키징
    (가장 오래 걸리는 패키징 구간이 번호 없는 줄이라, 무시하면 진행 표시가 멈춘 것처럼 보인다)
    """

    def __init__(self, emitter: Emitter) -> None:
        self.emitter = emitter
        self.phase = 0
        self.finished = False
        self.summary: dict[str, str] = {}

    def feed(self, line: str, stream: str) -> None:
        if stream != "stdout":
            return
        text = line.strip()
        match = MARKER.match(text)
        if match:
            number, label = match.groups()
            if number:
                self.phase = int(number)
            if label == FINISHED_LABEL:
                self.finished = True
                self.phase = TOTAL_PHASES
            self.emitter.progress(STEP, f"{self.phase}/{TOTAL_PHASES}", label)
            return
        for key, pattern in SUMMARY_PATTERNS.items():
            found = pattern.match(text)
            if found:
                self.summary[key] = found.group(1)


def run(ctx: Context, runner: Runner, emitter: Emitter) -> int:
    emitter.step_started(STEP, f"Vigie 배포 ({ctx.env}, {ctx.region})")
    if not _preflight(ctx, runner, emitter):
        emitter.step_finished(STEP, STEP_FAILED, "배포 전 확인에서 멈췄습니다 (배포하지 않음)")
        return 1

    emitter.log("배포에는 보통 20~40분이 걸립니다", stream="info")
    extra_env = {"AWS_REGION": ctx.region}
    if ctx.alarm_email:
        extra_env["ALARM_EMAIL"] = ctx.alarm_email
    else:
        emitter.log("알람 이메일 없음 (--alarm-email 또는 .env의 ALARM_EMAIL로 지정하면 CloudWatch 알람을 메일로 받습니다)",
                    stream="info")
    if ctx.admin_email:
        extra_env["ADMIN_EMAIL"] = ctx.admin_email
    else:
        emitter.log("관리자 이메일 없음 (--admin-email 또는 .env의 ADMIN_EMAIL로 지정하면 그 사용자가 변경 작업을 승인하고 "
                    "모든 감사 로그를 봅니다)",
                    stream="info")

    parser = ProgressParser(emitter)
    started_at = datetime.now(timezone.utc)
    result = runner.change(deploy_command(ctx), id_="deploy",
                           reason=f"Vigie를 {ctx.env} 환경에 배포합니다 (AWS 리소스가 생성·변경되고 비용이 발생합니다)",
                           cwd=str(ctx.repo_root), extra_env=extra_env, stream=True, on_line=parser.feed)

    if result.outcome == DRY_RUN:
        emitter.step_finished(STEP, STEP_OK, "배포 확인 끝 (바꾼 것 없음)")
        return 0
    if result.outcome == DECLINED:
        emitter.step_finished(STEP, STEP_SKIPPED, "배포하지 않았습니다")
        return 0
    if result.interrupted:
        emitter.error(STEP, "배포를 취소했습니다",
                      hint="스택이 업데이트 중인 상태로 남아 있을 수 있습니다. CloudFormation 콘솔에서 vigie-로 시작하는 "
                           "스택이 *_IN_PROGRESS가 아닌지 확인한 뒤 deploy를 다시 실행하세요")
        emitter.step_finished(STEP, STEP_FAILED, "취소했습니다")
        return RC_INTERRUPTED
    if not result.ok:
        _report_failure(ctx, runner, emitter, result, since=started_at - CLOCK_SKEW)
        emitter.step_finished(STEP, STEP_FAILED, f"배포에 실패했습니다 (deploy.sh 종료 코드 {result.returncode})")
        return 1

    # 주소는 한 줄에 하나씩 (결과 요약 한 줄에 몰아 쓰면 길어서 복사하기 어렵다)
    for label, value in (("API", parser.summary.get("api_url")),
                         ("프론트엔드", _with_scheme(parser.summary.get("frontend_url")))):
        if value:
            emitter.log(f"{label}: {value}", stream="info")
    if ctx.alarm_email:
        emitter.log(f"{ctx.alarm_email}로 온 구독 확인 메일(AWS Notification - Subscription Confirmation)의 "
                    "링크를 눌러야 알람 메일을 받습니다", stream="info")
    if ctx.admin_email:
        emitter.log(f"관리자 계정({ctx.admin_email})을 새로 만들었다면 임시 비밀번호가 든 초대 메일이 갑니다. "
                    "이미 가입한 계정이면 권한(admins·approvers 그룹)만 더했습니다", stream="info")
    emitter.step_finished(STEP, STEP_OK, "배포 완료")
    return 0


def _with_scheme(url: str | None) -> str | None:
    # deploy.sh는 프론트엔드 주소를 `dev.xxxx.amplifyapp.com`처럼 https:// 없이 출력한다
    if url and not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def _preflight(ctx: Context, runner: Runner, emitter: Emitter) -> bool:
    """배포를 시작해도 되는지 확인한다. 모두 읽기 전용이라 dry-run에서도 실행한다."""
    ok = True
    if ctx.repo_root is None:
        emitter.error(STEP, "Vigie 저장소를 찾지 못했습니다", hint="--repo <저장소 경로>로 지정하세요")
        return False

    required = [f"{ctx.ssm_prefix}/{param.key}" for param in SECRET_PARAMS if param.required]
    existing, error = existing_parameters(runner, required)
    if existing is None:
        emitter.error(STEP, "SSM 파라미터를 확인하지 못했습니다", raw=error)
        ok = False
    else:
        missing = [name for name in required if name not in existing]
        # SSM에 없어도 저장소 루트 .env에 있으면 괜찮다: deploy.sh가 스택을 배포하기 전에 SSM으로 올린다
        for param in SECRET_PARAMS:
            name = f"{ctx.ssm_prefix}/{param.key}"
            if name in missing and dotenv.read_value(ctx.repo_root, param.key):
                missing.remove(name)
                emitter.log(f"{name}이(가) SSM에 없지만 저장소 루트 .env에 있어 deploy.sh가 배포 전에 등록합니다",
                            stream="info")
        if missing:
            emitter.error(STEP, f"필수 SSM 파라미터가 없습니다: {', '.join(missing)}",
                          hint="setup 명령으로 먼저 등록하거나, 저장소 루트 .env에 적으세요 (.env.example 참고)")
            ok = False

    quota, error = find_timeout_quota(runner)
    if quota is None:
        # 할당량 조회 권한이 없는 계정일 수도 있다. 막지는 않고 알려만 준다
        emitter.log("통합 타임아웃 할당량을 확인하지 못했습니다 — 부족하면 스택 생성이 실패합니다", stream="info")
        emitter.log(error or "(오류 원문 없음)", stream="stderr")
    elif quota.value < REQUIRED_TIMEOUT_MS:
        # cloudformation/llm.yaml이 통합 타임아웃을 120000ms로 고정하므로 지금 배포하면 스택 생성이 실패한다
        emitter.error(STEP, f"통합 타임아웃 할당량이 {int(quota.value)}ms입니다 (필요 {REQUIRED_TIMEOUT_MS}ms)",
                      hint="setup으로 증가를 요청하고, 승인된 뒤 다시 배포하세요")
        ok = False
    return ok


def _report_failure(ctx: Context, runner: Runner, emitter: Emitter, result, *, since: datetime) -> None:
    unique = stack_failures(runner, main_stacks(ctx.env), since)
    for failure in unique:
        # 원인(reason)은 CloudFormation이 남긴 원문이다
        emitter.error(STEP, f"{failure.stack}: {failure.logical_id} ({failure.resource_type})", raw=failure.reason)
    if not unique:
        emitter.error(STEP, "deploy.sh가 실패했습니다", raw=error_text(result),
                      hint="CloudFormation 스택에서는 실패 기록을 찾지 못했습니다. 위 로그의 마지막 부분을 확인하세요")
