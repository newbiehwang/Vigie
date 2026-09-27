"""여러 단계가 함께 쓰는 AWS 조회 도우미와 Vigie 리소스 이름

리소스 이름은 deploy.sh와 cloudformation/*.yaml이 정한 것을 그대로 옮겨 적는다.
이름이 어긋나면 설치 마법사가 엉뚱한 리소스를 확인하게 되므로,
tests/installer/test_installer_aws.py가 deploy.sh·템플릿과 비교해 확인한다.

여기 있는 함수는 모두 읽기 전용이다 (Runner.run만 쓴다).
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .runner import CommandResult, Runner

# ---- API Gateway 통합 타임아웃 할당량 ----
QUOTA_SERVICE = "apigateway"
# 할당량 코드(L-로 시작)는 문서화된 고정값이 아니어서 이름으로 찾는다
QUOTA_NAME = "Maximum integration timeout in milliseconds"
# cloudformation/llm.yaml의 TimeoutInMillis와 같아야 한다. 할당량이 이보다 작으면
# API Gateway가 이 타임아웃 값을 받아들이지 않아 스택 생성·업데이트가 실패한다.
# 120000ms까지는 요청하면 자동으로 승인되고, 그보다 큰 값은 추가 승인(검토)이 필요해 120000ms로 정했다.
REQUIRED_TIMEOUT_MS = 120000
# 아직 처리 중인 할당량 요청 상태 (이 상태면 새로 요청하지 않고 기다린다)
QUOTA_PENDING_STATUSES = ("PENDING", "CASE_OPENED")
QUOTA_REJECTED_STATUSES = ("DENIED", "NOT_APPROVED", "INVALID_REQUEST")


@dataclass(frozen=True)
class SecretParam:
    """SSM에 SecureString으로 저장하는 비밀 값 하나."""
    key: str                 # /vigie/<env>/ 뒤의 이름
    title: str               # 화면에 보일 이름
    required: bool           # False면 비워 두어도 된다 (등록하지 않고 건너뜀)
    echo: bool = False       # True면 터미널에서 입력 내용을 보여 준다 (붙여 넣었는지 확인하기 어려운 긴 키)


SECRET_PARAMS = (
    SecretParam("ANTHROPIC_API_KEY", "Anthropic API 키", required=True, echo=True),
    # Slack 값은 Slack 봇을 쓸 때만 필요하다. 없으면 Slack 요청이 서명 검증에서 모두 거부된다
    SecretParam("SlackbotToken", "Slack 봇 토큰 (xoxb-...)", required=False),
    SecretParam("SlackSigningSecret", "Slack Signing Secret", required=False),
)


def main_stacks(env: str) -> list[str]:
    """deploy.sh가 만드는 최상위 스택. deploy.sh의 배포 순서와 같다."""
    return [f"vigie-base-{env}", f"vigie-frontend-{env}", f"vigie-mcp-{env}", f"vigie-{env}"]


def oidc_stack(env: str) -> str:
    """GitHub Actions 배포용 OIDC Role 스택 (cloudformation/github-oidc.yaml, deploy.sh가 배포하지 않음)."""
    return f"vigie-github-oidc-{env}"


def env_buckets(account_id: str, env: str) -> list[str]:
    """환경마다 하나씩 있는 S3 버킷 (cloudformation/base.yaml, 모두 DeletionPolicy: Retain)."""
    kinds = ("deployment", "frontend", "outputbucket", "athenaoutputbucket", "guarddutyexportbucket",
             "dockerbuildbucket", "diagrambucket")
    return [f"vigie-{kind}-{account_id}-{env}" for kind in kinds]


def shared_bucket(account_id: str) -> str:
    """deploy.sh가 템플릿을 올리는 버킷. 모든 환경이 함께 쓴다 (deploy.sh의 CLOUDFORMATION_BUCKET)."""
    return f"vigie-cloudformation-{account_id}"


def mcp_repository(env: str) -> str:
    return f"vigie-mcp-{env}"   # cloudformation/mcp.yaml의 MCPRepo


# 로그 그룹 /aws/lambda/<이름>-<env>를 가진 Lambda 함수들 (cloudformation의 FunctionName)
LAMBDA_FUNCTIONS = ("vigie-mcp", "vigie-llm", "vigie-slackbot", "vigie-chat-history", "vigie-athena-utility")


def dashboard_name(env: str) -> str:
    return f"vigie-{env}-service"   # cloudformation/monitoring.yaml의 DashboardName


# ---- 공통 호출 ----

def aws_json(runner: Runner, *args: str, timeout: float = 60) -> tuple[Any, CommandResult]:
    """`aws <args> --output json`을 실행해 (해석한 JSON, 실행 결과)를 돌려준다.
    실패하거나 JSON이 아니면 첫 번째 값이 None이다 (원인은 두 번째 값의 stderr로 안내한다)."""
    result = runner.run(["aws", *args, "--output", "json"], timeout=timeout)
    if not result.ok:
        return None, result
    try:
        return json.loads(result.stdout or "null"), result
    except json.JSONDecodeError:
        return None, result


def error_text(result: CommandResult) -> str:
    """실패한 명령의 오류를 한 줄로 (보통 stderr의 마지막 줄에 원인이 있다)."""
    lines = [line for line in (result.stderr or result.stdout).strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"종료 코드 {result.returncode}"


def is_not_found(result: CommandResult) -> bool:
    """리소스가 없어서 실패했는지 (없는 것은 오류가 아니라 '아직 안 만들어짐'으로 다루는 경우가 많다)."""
    text = result.stderr + result.stdout
    return any(marker in text for marker in (
        "ResourceNotFound", "does not exist", "ParameterNotFound", "NotFoundException"))


def parse_time(value: Any) -> datetime | None:
    """AWS CLI가 출력하는 ISO 8601 시각 (예: 2026-09-22T12:30:04.123000+00:00)."""
    if not isinstance(value, str):
        return None
    try:
        # Python 3.10의 fromisoformat은 끝의 'Z'를 모른다
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- 스택 실패 원인 ----

# 스택 이벤트를 몇 개까지 볼지. 오래된 스택은 이벤트가 수천 개라 전부 받으면 느리다.
# 이번 작업의 이벤트는 가장 최근 것들이므로 이 정도면 충분하다.
MAX_STACK_EVENTS = 200
MAX_NESTED_DEPTH = 2   # 중첩 스택(vigie-<env> 안의 llm, logs ...)을 몇 단계까지 따라 들어갈지


@dataclass(frozen=True)
class Failure:
    stack: str
    logical_id: str
    resource_type: str
    reason: str


@dataclass
class _Collector:
    runner: Runner
    since: datetime
    seen_stacks: set[str] = field(default_factory=set)

    def collect(self, stack: str, depth: int = 0) -> list[Failure]:
        """스택 이벤트에서 since 이후 *_FAILED 이벤트를 모은다.

        - "Resource creation cancelled"처럼 다른 리소스가 실패해서 딸려 취소된 것은 원인이 아니므로 뺀다.
        - 중첩 스택 리소스가 실패했으면 그 스택 안으로 들어가 진짜 원인을 찾는다. 안에서 찾으면
          바깥의 "Embedded stack ... was not successfully created" 같은 요약 줄은 뺀다.
        """
        if stack in self.seen_stacks:
            return []
        self.seen_stacks.add(stack)
        data, _ = aws_json(self.runner, "cloudformation", "describe-stack-events", "--stack-name", stack,
                           "--max-items", str(MAX_STACK_EVENTS))
        if data is None:
            return []   # 스택이 아직 없거나 조회 실패. 요약은 가능한 만큼만 한다
        failures: list[Failure] = []
        for event in data.get("StackEvents", []):
            moment = parse_time(event.get("Timestamp"))
            if moment is not None and moment < self.since:
                continue
            if not str(event.get("ResourceStatus", "")).endswith("_FAILED"):
                continue
            reason = event.get("ResourceStatusReason", "")
            if "cancelled" in reason.lower():
                continue
            physical = event.get("PhysicalResourceId", "")
            if physical and physical == event.get("StackId"):
                continue   # 스택 자신에 대한 이벤트 (원인은 개별 리소스 이벤트에 있다)
            name = event.get("StackName", stack)
            if (event.get("ResourceType") == "AWS::CloudFormation::Stack" and physical
                    and depth < MAX_NESTED_DEPTH):
                nested = self.collect(physical, depth + 1)
                if nested:
                    failures.extend(nested)
                    continue
            failures.append(Failure(name, event.get("LogicalResourceId", "?"), event.get("ResourceType", "?"),
                                    reason or "(이유 없음)"))
        return failures


def stack_failures(runner: Runner, stacks: list[str], since: datetime) -> list[Failure]:
    """여러 스택에서 since 이후 실패한 리소스와 이유를 모은다 (deploy·teardown의 실패 요약).
    같은 리소스의 실패가 여러 번 기록될 수 있으므로 한 번씩만 돌려준다."""
    collector = _Collector(runner, since)
    failures: list[Failure] = []
    for stack in stacks:
        failures.extend(collector.collect(stack))
    return list(dict.fromkeys(failures))


# ---- 할당량 ----

@dataclass(frozen=True)
class Quota:
    code: str
    value: float
    adjustable: bool


def find_timeout_quota(runner: Runner) -> tuple[Quota | None, str | None]:
    """(할당량, 오류). 계정에 적용된 값 목록에서 먼저 찾고, 없으면 AWS 기본값 목록에서 찾는다.
    (조정한 적 없는 할당량은 적용 값 목록에 없을 수 있다)"""
    for command in ("list-service-quotas", "list-aws-default-service-quotas"):
        data, result = aws_json(runner, "service-quotas", command, "--service-code", QUOTA_SERVICE)
        if data is None:
            return None, error_text(result)
        for quota in data.get("Quotas", []):
            if quota.get("QuotaName") == QUOTA_NAME:
                return Quota(quota["QuotaCode"], float(quota.get("Value", 0)),
                             bool(quota.get("Adjustable", False))), None
    return None, f"'{QUOTA_NAME}' 할당량을 찾지 못했습니다"


def quota_requests(runner: Runner, code: str) -> tuple[list[dict] | None, str | None]:
    """이 할당량에 대한 증가 요청 기록 (최근 것부터)."""
    data, result = aws_json(runner, "service-quotas", "list-requested-service-quota-change-history-by-quota",
                            "--service-code", QUOTA_SERVICE, "--quota-code", code)
    if data is None:
        return None, error_text(result)

    def created(request: dict) -> float:
        # 시각을 해석하지 못한 요청은 가장 오래된 것으로 본다
        moment = parse_time(request.get("Created"))
        return moment.timestamp() if moment else 0.0

    return sorted(data.get("RequestedQuotas", []), key=created, reverse=True), None


# ---- SSM 파라미터 ----

def existing_parameters(runner: Runner, names: list[str]) -> tuple[dict[str, str] | None, str | None]:
    """({이름: 형식(String|SecureString...)}, 오류). 이름만 확인하고 값은 읽지 않는다.

    get-parameter 대신 describe-parameters를 쓰는 이유: get-parameter는 복호화하지 않아도
    값(암호문)을 함께 돌려준다. describe-parameters는 이름·형식 같은 메타데이터만 돌려준다.
    """
    data, result = aws_json(runner, "ssm", "describe-parameters", "--parameter-filters",
                            f"Key=Name,Option=Equals,Values={','.join(names)}")
    if data is None:
        return None, error_text(result)
    return {p["Name"]: p.get("Type", "") for p in data.get("Parameters", [])}, None
