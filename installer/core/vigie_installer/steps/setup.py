"""setup: 배포 전에 한 번 해 두어야 하는 계정 설정 (계획서 4.2절)

1. quota           API Gateway 통합 타임아웃 할당량을 120000ms로 올려 달라고 요청한다 (이 값까지는 자동 승인).
2. ssm_parameters  Anthropic API 키와 Slack 값을 SSM Parameter Store에 SecureString으로 저장한다.

두 작업 모두 "현재 상태 확인 → 이미 되어 있으면 skipped → 할 일을 보여 주고 승인 → 실행" 순서를 따른다.
그래서 여러 번 실행해도 결과가 같고(멱등성), 중간에 실패해도 다시 실행하면 남은 일만 한다.

비밀 값 처리 (계획서 2.2절)
- 값은 터미널에서 보이지 않게 입력받거나(기본), JSON 모드에서는 stdin으로 받는다.
- 명령 인자로 넘기지 않고, 권한 0600 임시 파일에 담아 `--cli-input-json file://...`로 넘긴 뒤 바로 지운다.
- 이미 있는 값은 읽지 않는다 (이름·형식만 확인). 덮어쓸지는 사용자가 고르고, 기본은 "유지"다.

저장소 루트 .env에 값이 있으면 (ANTHROPIC_API_KEY, dotenv.py)
- 값을 묻지 않고 .env의 값을 쓴다. SSM에 없으면 그 값으로 등록한다 (등록 전 승인은 그대로 받는다).
- SSM에 이미 있으면 덮어쓸지 묻지 않고 그대로 둔다. 배포할 때 deploy.sh가 SSM 값과 .env 값을 비교해
  다르면 .env 값으로 맞추므로(sync_anthropic_key), setup이 기존 값을 읽을 필요가 없다.
"""
import json

from .. import dotenv
from ..aws import (QUOTA_PENDING_STATUSES, QUOTA_REJECTED_STATUSES, QUOTA_SERVICE, REQUIRED_TIMEOUT_MS,
                   SECRET_PARAMS, SecretParam, error_text, existing_parameters, find_timeout_quota,
                   quota_requests)
from ..context import Context
from ..events import STEP_FAILED, STEP_OK, STEP_SKIPPED, Emitter
from ..runner import DECLINED, DRY_RUN, Runner, secret_file

# 선택지 id (JSON 모드에서는 이 id로 응답한다)
KEEP, OVERWRITE = "keep", "overwrite"


STEP = "setup"


def run(ctx: Context, runner: Runner, emitter: Emitter) -> int:
    # 다른 명령처럼 명령 전체의 시작·끝을 알린다 (읽는 쪽이 제목과 결과 요약을 보여 줄 수 있게)
    emitter.step_started(STEP, f"사전 설정 ({ctx.env}, {ctx.region})")
    # 하위 단계의 결과(STEP_OK·STEP_SKIPPED·STEP_FAILED). 하나가 실패해도 다른 하나는 진행한다
    statuses = [_request_quota(ctx, runner, emitter), _store_parameters(ctx, runner, emitter)]
    if STEP_FAILED in statuses:
        emitter.step_finished(STEP, STEP_FAILED,
                              "사전 설정을 끝내지 못했습니다. 원인을 해결하고 다시 실행하면 이어서 진행합니다")
        return 1
    if STEP_SKIPPED in statuses:
        # 사용자가 거절한 항목이 있다 (할당량 요청이나 API 키 저장). 이대로는 배포가 실패하므로 "완료"라 하지 않는다
        emitter.step_finished(STEP, STEP_SKIPPED, "건너뛴 항목이 있어 아직 배포할 수 없습니다. setup을 다시 실행하세요")
        return 0
    emitter.step_finished(STEP, STEP_OK, "사전 설정 확인 끝 (바꾼 것 없음)" if runner.dry_run else "사전 설정 완료")
    return 0


# ---------------------------------------------------------------- 할당량

def _request_quota(ctx: Context, runner: Runner, emitter: Emitter) -> str:
    """할당량이 부족하면 증가를 요청한다. 결과 상태(STEP_OK·STEP_SKIPPED·STEP_FAILED)를 돌려준다."""
    step = "quota"
    emitter.step_started(step, "API Gateway 통합 타임아웃 할당량")

    quota, error = find_timeout_quota(runner)
    if quota is None:
        emitter.error(step, "할당량을 조회하지 못했습니다", raw=error,
                      hint="Service Quotas 콘솔에서 API Gateway의 'Maximum integration timeout in milliseconds'를 "
                           "직접 확인하세요")
        emitter.step_finished(step, STEP_FAILED, "")
        return STEP_FAILED

    current = int(quota.value)
    if current >= REQUIRED_TIMEOUT_MS:
        emitter.step_finished(step, STEP_OK, "")   # 이미 충분하다 = 할 일을 마친 상태
        return STEP_OK
    if not quota.adjustable:
        emitter.error(step, f"이 리전에서는 할당량을 조정할 수 없습니다 (현재 {current}ms)",
                      hint="다른 리전을 쓰거나 AWS Support에 문의하세요")
        emitter.step_finished(step, STEP_FAILED, "")
        return STEP_FAILED

    # 이미 요청해 둔 것이 처리 중이면 또 요청하지 않는다 (중복 요청은 거절될 수 있다)
    history, error = quota_requests(runner, quota.code)
    if history is None:
        emitter.error(step, "이전 요청 기록을 조회하지 못했습니다", raw=error)
        emitter.step_finished(step, STEP_FAILED, "")
        return STEP_FAILED
    pending = next((r for r in history if r.get("Status") in QUOTA_PENDING_STATUSES), None)
    if pending:
        emitter.step_finished(step, STEP_OK, f"승인 대기 중 ({pending['Status']}) — 승인되면 deploy를 실행하세요")
        return STEP_OK
    if history and history[0].get("Status") in QUOTA_REJECTED_STATUSES:
        emitter.log(f"지난 요청이 거절되었습니다 ({history[0]['Status']}). 다시 요청합니다", stream="info")

    # llm.yaml이 통합 타임아웃을 120000ms로 고정하므로 할당량이 오르기 전에는 스택 생성이 실패한다
    emitter.log(f"현재 {current}ms → {REQUIRED_TIMEOUT_MS}ms 필요", stream="info")
    result = runner.change(
        ["aws", "service-quotas", "request-service-quota-increase", "--service-code", QUOTA_SERVICE,
         "--quota-code", quota.code, "--desired-value", str(REQUIRED_TIMEOUT_MS), "--output", "json"],
        id_="request_quota", reason=f"통합 타임아웃 할당량을 {REQUIRED_TIMEOUT_MS}ms로 올려 달라고 요청합니다",
        timeout=60)
    if result.outcome == DRY_RUN:
        emitter.step_finished(step, STEP_OK, "")
        return STEP_OK
    if result.outcome == DECLINED:
        emitter.step_finished(step, STEP_SKIPPED, "할당량이 오르기 전에는 배포가 실패합니다")
        return STEP_SKIPPED
    if not result.ok:
        emitter.error(step, "할당량 증가를 요청하지 못했습니다", raw=error_text(result))
        emitter.step_finished(step, STEP_FAILED, "")
        return STEP_FAILED
    try:
        status = json.loads(result.stdout)["RequestedQuota"]["Status"]
    except (json.JSONDecodeError, KeyError, TypeError):
        status = "알 수 없음"
    emitter.step_finished(step, STEP_OK, f"요청했습니다 ({status}) — 승인되면 deploy를 실행하세요")
    return STEP_OK


# ---------------------------------------------------------------- SSM 파라미터

def _store_parameters(ctx: Context, runner: Runner, emitter: Emitter) -> str:
    step = "ssm_parameters"
    emitter.step_started(step, "SSM 파라미터")
    names = [f"{ctx.ssm_prefix}/{param.key}" for param in SECRET_PARAMS]
    existing, error = existing_parameters(runner, names)
    if existing is None:
        emitter.error(step, "기존 파라미터를 조회하지 못했습니다", raw=error)
        emitter.step_finished(step, STEP_FAILED, "")
        return STEP_FAILED

    stored, kept, declined, failed, planned = [], [], [], [], []
    for param, name in zip(SECRET_PARAMS, names, strict=True):
        param_type = existing.get(name)
        from_env = dotenv.read_value(ctx.repo_root, param.key)
        if from_env:
            # 저장소 루트 .env에 적어 둔 값: 묻지 않고 쓴다 (모듈 설명 참고)
            runner.emitter.redactor.add(from_env)
            if param_type is not None:
                emitter.log(f"{name}: 저장소 루트 .env에 {param.key}가 있어 그대로 둡니다. "
                            "배포할 때 deploy.sh가 .env 값과 다르면 SSM을 .env 값으로 맞춥니다", stream="info")
                kept.append(name)
                continue
            emitter.log(f"저장소 루트 .env의 {param.key}를 씁니다: {mask_secret(from_env)}", stream="info")
            outcome = _put_parameter(ctx, runner, emitter, param, name, overwrite=False, value=from_env)
            {"stored": stored, "failed": failed, "planned": planned, "declined": declined}[outcome].append(name)
            continue
        if param_type is not None:
            if param_type != "SecureString":
                emitter.log(f"{name}이(가) {param_type} 형식으로 저장되어 있습니다. 평문으로 보관되므로 "
                            "덮어써서 SecureString으로 바꾸기를 권장합니다", stream="info")
            choice = runner.interaction.choose(
                f"existing_{param.key}", f"{name}이(가) 이미 있습니다",
                [(KEEP, "기존 값 유지"), (OVERWRITE, "새 값으로 덮어쓰기")], default=KEEP)
            if choice == KEEP:
                kept.append(name)
                continue
        outcome = _put_parameter(ctx, runner, emitter, param, name, overwrite=param_type is not None)
        {"stored": stored, "failed": failed, "planned": planned, "declined": declined}[outcome].append(name)

    # 0개는 말하지 않는다. 모두 이미 등록되어 있으면(=할 일이 없으면) 요약 없이 [완료]
    required = {f"{ctx.ssm_prefix}/{param.key}" for param in SECRET_PARAMS if param.required}
    parts = [f"{len(names_)}개 {label}" for names_, label in
             ((failed, "실패"), (stored, "등록"), (planned, "등록 예정"), (declined, "건너뜀")) if names_]
    summary = " · ".join(parts) if (stored or failed or planned or declined) else ""
    if failed:
        emitter.step_finished(step, STEP_FAILED, summary)
        return STEP_FAILED
    if any(name in required for name in declined):
        # 필수 값(API 키)을 사용자가 저장하지 않기로 했다 → 배포할 수 없는 상태
        emitter.step_finished(step, STEP_SKIPPED, summary + " — API 키가 없으면 배포할 수 없습니다")
        return STEP_SKIPPED
    emitter.step_finished(step, STEP_OK, summary)
    return STEP_OK


def mask_secret(value: str) -> str:
    """비밀 값을 확인용으로 줄여 보여 준다: 앞 7글자(sk-ant- 같은 종류 표시)와 끝 4글자, 길이.
    짧은 값은 일부만 보여도 추측하기 쉬우므로 길이만 보여 준다."""
    if len(value) < 20:
        return f"({len(value)}자)"
    return f"{value[:7]}…{value[-4:]} ({len(value)}자)"


def _put_parameter(ctx: Context, runner: Runner, emitter: Emitter, param: SecretParam, name: str,
                   *, overwrite: bool, value: str | None = None) -> str:
    """파라미터 하나를 저장한다. 결과: stored | failed | planned(dry-run) | declined.
    value: 이미 정해진 값(저장소 루트 .env). 없으면 사용자에게 묻는다."""
    step = "ssm_parameters"
    reason = f"{name}에 SecureString으로 저장합니다" + (" (덮어쓰기)" if overwrite else "")

    if runner.dry_run:
        # dry-run에서는 비밀 값을 묻지 않는다. 실제 명령과 같은 모양으로 보여 주기만 한다
        runner.change(["aws", "ssm", "put-parameter", "--cli-input-json", "file://<임시 파일>"],
                      id_=f"put_{param.key}", reason=reason)
        return "planned"

    from_env = value is not None
    if not from_env:
        value = runner.interaction.secret(f"secret_{param.key}", param.title, echo=param.echo)
    # 복사해 붙여 넣을 때 딸려 온 공백·줄바꿈을 지운다 (그대로 저장하면 API 호출이 인증 오류로 실패한다)
    value = (value or "").strip()
    runner.emitter.redactor.add(value)
    if value and not param.echo and not from_env:
        # 보이지 않게 입력한 값은 들어갔는지 알 수 없으므로 길이 등으로 확인해 준다 (보이게 입력한 값은 필요 없다)
        emitter.log(f"입력됨: {mask_secret(value)}", stream="info")
    if not value:
        if param.required:
            emitter.error(step, f"{param.title}이(가) 비어 있어 저장하지 않았습니다",
                          hint="Anthropic 콘솔(console.anthropic.com)에서 API 키를 발급받아 입력하세요")
            return "failed"
        # Slack을 쓰지 않는 경우: 등록하지 않는다. Lambda 설정(layers/common/config.py)은 있는 파라미터만
        # 읽고, Signing Secret이 없으면 Slack 요청을 모두 거부하므로(services/slackbot/slack_security.py) 안전하다.
        emitter.log("비워 두어 등록하지 않습니다 (Slack 기능 꺼짐, 나중에 setup으로 등록 가능)", stream="info")
        return "declined"

    request = {"Name": name, "Value": value, "Type": "SecureString", "Overwrite": overwrite}
    with secret_file(request) as path:
        result = runner.change(["aws", "ssm", "put-parameter", "--cli-input-json", f"file://{path}",
                                "--output", "json"],
                               id_=f"put_{param.key}", reason=reason, timeout=60)
    if result.outcome == DECLINED:
        return "declined"
    if not result.ok:
        emitter.error(step, f"{name}을(를) 저장하지 못했습니다", raw=error_text(result))
        return "failed"
    return "stored"
