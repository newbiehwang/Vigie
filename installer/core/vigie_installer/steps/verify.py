"""verify: 배포가 제대로 됐는지 확인한다 (계획서 4.4절). 아무것도 바꾸지 않는다.

| 항목            | 방법                                                   | 통과 기준                      |
|-----------------|--------------------------------------------------------|--------------------------------|
| 스택 상태        | describe-stacks (최상위 4개 + 그 아래 중첩 스택)          | CREATE_COMPLETE/UPDATE_COMPLETE |
| 인증 없는 호출   | POST /llm1 (인증 헤더 없이)                             | 401 또는 403                    |
| 공개 경로        | GET /health                                            | 200                            |
| 최소 권한 오류   | 최근 24시간 Lambda 로그에서 AccessDenied 검색              | 0건 (있으면 주의로 목록 표시)     |
| 프론트엔드       | frontend 스택 출력의 주소로 GET                          | 200 (아니면 주의: 배포 반영 중일 수 있음) |
| 대시보드         | cloudwatch get-dashboard                               | 존재                           |

API 주소는 deploy.sh와 같은 방법으로 만든다: SSM /vigie/<env>/ApiGatewayId → https://<id>.execute-api.<리전>.amazonaws.com/<env>
HTTP 요청은 표준 라이브러리 urllib으로 보낸다. 테스트에서 가짜로 바꿀 수 있도록 run()의 http 인자로 받는다.
"""
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from ..aws import LAMBDA_FUNCTIONS, aws_json, dashboard_name, error_text, is_not_found, main_stacks
from ..context import Context
from ..events import CHECK_FAIL, CHECK_OK, CHECK_WARN, STEP_FAILED, STEP_OK, Emitter
from ..runner import Runner

STEP = "verify"
HEALTHY_STACK_STATUSES = ("CREATE_COMPLETE", "UPDATE_COMPLETE")
LOG_LOOKBACK_HOURS = 24
MAX_LOG_EVENTS = 20       # 로그 그룹마다 가져올 AccessDenied 이벤트 수 (목록 표시용이라 많을 필요 없다)
SHOWN_LOG_EVENTS = 3      # 화면에 보여 줄 개수
HTTP_TIMEOUT = 15

# (상태 코드 또는 None, 오류 설명 또는 None)
HttpResult = tuple[int | None, str | None]
HttpFunc = Callable[[str, str], HttpResult]


def http_request(method: str, url: str) -> HttpResult:
    """HTTP 요청을 보내고 상태 코드를 돌려준다. 4xx·5xx도 예외가 아니라 상태 코드로 돌려준다."""
    data = b"{}" if method == "POST" else None
    headers = {"User-Agent": "vigie-installer"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, str(getattr(exc, "reason", exc))


def run(ctx: Context, runner: Runner, emitter: Emitter, http: HttpFunc = http_request) -> int:
    emitter.step_started(STEP, f"배포 검증 ({ctx.env}, {ctx.region})")
    statuses: list[str] = []

    def report(id_: str, title: str, status: str, detail: str, hint: str | None = None,
               url: str | None = None, raw: str | None = None) -> None:
        statuses.append(status)
        emitter.check(id_, title, status, detail, hint, url, raw=raw)

    stacks = _check_stacks(ctx, runner, report)
    if stacks is None:
        # 배포된 것이 하나도 없다: 나머지 검사는 모두 같은 이유로 실패하므로 하지 않는다
        emitter.step_finished(STEP, STEP_FAILED, "배포된 것이 없습니다 — deploy를 먼저 실행하세요")
        return 1
    _check_api(ctx, runner, http, report)
    _check_access_denied(ctx, runner, report)
    _check_frontend(ctx, stacks, http, report)
    _check_dashboard(ctx, runner, report)

    counts = {s: statuses.count(s) for s in (CHECK_OK, CHECK_WARN, CHECK_FAIL)}
    warn = f" · {counts[CHECK_WARN]}개 주의" if counts[CHECK_WARN] else ""   # 0개는 말하지 않는다
    if counts[CHECK_FAIL]:
        emitter.step_finished(STEP, STEP_FAILED, f"{counts[CHECK_FAIL]}개 오류{warn}")
        return 1
    emitter.step_finished(STEP, STEP_OK, f"{counts[CHECK_OK]}개 통과{warn}")
    return 0


def _check_stacks(ctx: Context, runner: Runner, report) -> dict[str, dict] | None:
    """최상위 스택과 그 아래 중첩 스택의 상태. 다른 검사가 쓰도록 {스택 이름: 스택 정보}를 돌려준다.
    이 환경의 스택이 하나도 없으면 한 항목으로 알리고 None을 돌려준다 (배포 전에 실행한 경우)."""
    data, result = aws_json(runner, "cloudformation", "describe-stacks")
    if data is None:
        report("stacks", "CloudFormation 스택", CHECK_FAIL, "스택 목록을 조회하지 못했습니다", raw=error_text(result))
        return {}
    all_stacks = data.get("Stacks", [])
    by_name = {stack["StackName"]: stack for stack in all_stacks}
    if not any(name in by_name for name in main_stacks(ctx.env)):
        report("stacks", "CloudFormation 스택", CHECK_FAIL, f"{ctx.env} 환경에 배포된 스택이 없습니다",
               "deploy를 먼저 실행하세요")
        return None

    for name in main_stacks(ctx.env):
        title = f"스택 {name}"
        stack = by_name.get(name)
        if stack is None:
            report(f"stack_{name}", title, CHECK_FAIL, "스택이 없습니다", "deploy를 먼저 실행하세요")
            continue
        # 이 스택 아래의 중첩 스택 (RootId가 이 스택의 ID). 중첩 스택이 실패해도 최상위는 COMPLETE일 수 있다
        nested = [s for s in all_stacks if s.get("RootId") == stack["StackId"]]
        broken = [s for s in [stack, *nested] if s.get("StackStatus") not in HEALTHY_STACK_STATUSES]
        if broken:
            detail = ", ".join(f"{s['StackName']}: {s.get('StackStatus')}" for s in broken)
            reason = next((s.get("StackStatusReason") for s in broken if s.get("StackStatusReason")), None)
            report(f"stack_{name}", title, CHECK_FAIL, detail,
                   "*_ROLLBACK_COMPLETE는 마지막 배포가 실패해 되돌아간 상태입니다", raw=reason)
            continue
        detail = stack["StackStatus"] + (f" (중첩 스택 {len(nested)}개 정상)" if nested else "")
        report(f"stack_{name}", title, CHECK_OK, detail)
    return by_name


def _api_base(ctx: Context, runner: Runner) -> tuple[str | None, str | None]:
    """(API 주소, 오류). deploy.sh와 같은 방식으로 만든다."""
    data, result = aws_json(runner, "ssm", "get-parameter", "--name", f"{ctx.ssm_prefix}/ApiGatewayId")
    if data is None:
        return None, error_text(result)
    api_id = data.get("Parameter", {}).get("Value")
    if not api_id:
        return None, "ApiGatewayId 값이 비어 있습니다"
    return f"https://{api_id}.execute-api.{ctx.region}.amazonaws.com/{ctx.env}", None


def _check_api(ctx: Context, runner: Runner, http: HttpFunc, report) -> None:
    base, error = _api_base(ctx, runner)
    if base is None:
        for id_, title in (("api_auth", "인증 없는 API 호출 차단"), ("api_health", "공개 경로 /health")):
            report(id_, title, CHECK_FAIL, "API 주소를 알 수 없습니다", "deploy가 끝났는지 확인하세요", raw=error)
        return

    # 인증 헤더 없이 LLM API를 부른다. Cognito 인증이 걸려 있으면 API Gateway가 401로 막는다.
    # 막히지 않으면 누구나 이 API로 Anthropic 요금을 쓸 수 있으므로 실패로 본다.
    status, error = http("POST", f"{base}/llm1")
    if status in (401, 403):
        report("api_auth", "인증 없는 API 호출 차단", CHECK_OK, f"POST /llm1 → {status} (막힘)")
    elif status is None:
        report("api_auth", "인증 없는 API 호출 차단", CHECK_FAIL, "요청하지 못했습니다", raw=error)
    elif 200 <= status < 300:
        report("api_auth", "인증 없는 API 호출 차단", CHECK_FAIL, f"POST /llm1 → {status}: 인증 없이 호출되었습니다",
               "cloudformation/llm.yaml의 LlmMethod에 AuthorizationType: COGNITO_USER_POOLS가 적용됐는지 확인하세요")
    else:
        report("api_auth", "인증 없는 API 호출 차단", CHECK_FAIL, f"POST /llm1 → 예상하지 못한 응답 {status}")

    status, error = http("GET", f"{base}/health")
    if status == 200:
        report("api_health", "공개 경로 /health", CHECK_OK, "GET /health → 200", url=f"{base}/health")
    else:
        if status is None:
            report("api_health", "공개 경로 /health", CHECK_FAIL, "요청하지 못했습니다", raw=error)
        else:
            report("api_health", "공개 경로 /health", CHECK_FAIL, f"GET /health → {status}")


def _check_access_denied(ctx: Context, runner: Runner, report) -> None:
    """최소 권한으로 줄인 IAM 정책에 빠진 권한이 있으면 Lambda 로그에 AccessDenied가 남는다."""
    start_ms = int((time.time() - LOG_LOOKBACK_HOURS * 3600) * 1000)
    found: list[str] = []
    checked = 0
    errors: list[str] = []
    for function in LAMBDA_FUNCTIONS:
        group = f"/aws/lambda/{function}-{ctx.env}"
        data, result = aws_json(runner, "logs", "filter-log-events", "--log-group-name", group,
                                "--filter-pattern", "AccessDenied", "--start-time", str(start_ms),
                                "--max-items", str(MAX_LOG_EVENTS))
        if data is None:
            if not is_not_found(result):   # 로그 그룹이 없는 것은 아직 호출되지 않았다는 뜻이라 괜찮다
                errors.append(f"{group}: {error_text(result)}")
            continue
        checked += 1
        for event in data.get("events", []):
            message = " ".join(str(event.get("message", "")).split())   # 여러 줄 메시지를 한 줄로
            found.append(f"{group}: {message[:200]}")

    title = f"최근 {LOG_LOOKBACK_HOURS}시간 AccessDenied 로그"
    if found:
        shown = "; ".join(found[:SHOWN_LOG_EVENTS]) + (f" 외 {len(found) - SHOWN_LOG_EVENTS}건"
                                                       if len(found) > SHOWN_LOG_EVENTS else "")
        report("access_denied", title, CHECK_WARN, f"{len(found)}건: {shown}",
               "IAM 정책에 필요한 권한이 빠졌을 수 있습니다. 해당 Lambda의 Role 정책을 확인하세요")
    elif errors:
        report("access_denied", title, CHECK_WARN, f"로그 그룹 {len(errors)}개를 확인하지 못했습니다",
               raw="; ".join(errors))
    else:
        report("access_denied", title, CHECK_OK, f"0건 (로그 그룹 {checked}개 확인)")


def _check_frontend(ctx: Context, stacks: dict[str, dict], http: HttpFunc, report) -> None:
    title = "프론트엔드"
    stack = stacks.get(f"vigie-frontend-{ctx.env}")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in (stack or {}).get("Outputs", [])}
    domain = outputs.get("AmplifyAppDefaultDomainWithEnv")
    if not domain:
        report("frontend", title, CHECK_FAIL, "frontend 스택 출력에서 주소를 찾지 못했습니다")
        return
    url = f"https://{domain}"
    status, error = http("GET", url)
    if status == 200:
        report("frontend", title, CHECK_OK, f"{url} → 200", url=url)
    else:
        # Amplify 배포는 deploy.sh가 끝난 뒤에도 몇 분 더 걸릴 수 있어 실패 대신 주의로 둔다
        report("frontend", title, CHECK_WARN, f"{url} → {status if status is not None else error}",
               "Amplify 배포가 아직 반영 중일 수 있습니다. 몇 분 뒤 verify를 다시 실행하세요", url=url)


def _check_dashboard(ctx: Context, runner: Runner, report) -> None:
    name = dashboard_name(ctx.env)
    url = (f"https://{ctx.region}.console.aws.amazon.com/cloudwatch/home?region={ctx.region}"
           f"#dashboards/dashboard/{name}")
    data, result = aws_json(runner, "cloudwatch", "get-dashboard", "--dashboard-name", name)
    if data is None:
        if is_not_found(result):
            report("dashboard", "CloudWatch 대시보드", CHECK_FAIL, f"{name}이(가) 없습니다")
        else:
            report("dashboard", "CloudWatch 대시보드", CHECK_WARN, f"{name}을(를) 확인하지 못했습니다",
                   raw=error_text(result))
        return
    report("dashboard", "CloudWatch 대시보드", CHECK_OK, name, url=url)

