"""verify: 배포 검증. HTTP 요청은 가짜 함수로 바꿔 실제 네트워크 없이 확인한다"""
import io
import json

import pytest

from vigie_installer.context import build_context
from vigie_installer.events import JsonEmitter, Redactor
from vigie_installer.runner import Interaction, Runner
from vigie_installer.steps import verify

from .helpers import run_cli

API = "https://api123.execute-api.ap-northeast-2.amazonaws.com/dev"
FRONTEND = "https://dev.d1234.amplifyapp.com"
MAIN = ["vigie-base-dev", "vigie-frontend-dev", "vigie-mcp-dev", "vigie-dev"]

# 검증이 호출해도 되는 aws 명령 (모두 읽기 전용)
READ_ONLY = ("cloudformation describe-stacks", "ssm get-parameter", "logs filter-log-events", "cloudwatch get-dashboard")


def stack(name, status="UPDATE_COMPLETE", root=None, outputs=None, reason=None):
    item = {"StackName": name, "StackId": f"arn:stack/{name}", "StackStatus": status}
    if root:
        item["RootId"] = f"arn:stack/{root}"
        item["ParentId"] = f"arn:stack/{root}"
    if outputs:
        item["Outputs"] = [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]
    if reason:
        item["StackStatusReason"] = reason
    return item


def healthy_stacks(**overrides):
    stacks = {name: stack(name) for name in MAIN}
    stacks["vigie-frontend-dev"] = stack("vigie-frontend-dev",
                                       outputs={"AmplifyAppDefaultDomainWithEnv": "dev.d1234.amplifyapp.com"})
    stacks["vigie-dev-LlmStack-A"] = stack("vigie-dev-LlmStack-A", root="vigie-dev")
    stacks["vigie-dev-LogsStack-B"] = stack("vigie-dev-LogsStack-B", root="vigie-dev")
    stacks["vigie-prod"] = stack("vigie-prod", status="ROLLBACK_COMPLETE")   # 다른 환경은 영향 없어야 함
    stacks.update(overrides)
    return [s for s in stacks.values() if s is not None]


def deployed(fake, *, stacks=None, access_denied=None, dashboard=True):
    fake.add("aws", "cloudformation describe-stacks", json.dumps({"Stacks": stacks or healthy_stacks()}))
    fake.add("aws", "ssm get-parameter --name /vigie/dev/ApiGatewayId",
             json.dumps({"Parameter": {"Name": "/vigie/dev/ApiGatewayId", "Type": "String", "Value": "api123"}}))
    for group, messages in (access_denied or {}).items():
        fake.add("aws", f"--log-group-name {group} ",
                 json.dumps({"events": [{"message": m} for m in messages]}))
    fake.add("aws", "--log-group-name /aws/lambda/vigie-athena-utility-dev ",
             stderr="An error occurred (ResourceNotFoundException): The specified log group does not exist.\n",
             exit=254)
    fake.add("aws", "logs filter-log-events", json.dumps({"events": []}))
    if dashboard:
        fake.add("aws", "cloudwatch get-dashboard", json.dumps({"DashboardName": "vigie-dev-service"}))
    else:
        fake.add("aws", "cloudwatch get-dashboard",
                 stderr="An error occurred (ResourceNotFound) when calling the GetDashboard operation: "
                        "Dashboard does not exist\n", exit=254)


class FakeHttp:
    """(method, url) → 상태 코드. 요청 내역을 남긴다."""

    def __init__(self, **overrides):
        self.responses = {("POST", f"{API}/llm1"): 401, ("GET", f"{API}/health"): 200, ("GET", FRONTEND): 200}
        self.responses.update(overrides.get("responses", {}))
        self.calls = []

    def __call__(self, method, url):
        self.calls.append((method, url))
        status = self.responses.get((method, url))
        return (None, "연결할 수 없음") if status is None else (status, None)


def run_verify(fake, http):
    out = io.StringIO()
    emitter = JsonEmitter(Redactor(), out)
    ctx = build_context(env="dev", region="ap-northeast-2", profile=None, repo=None, environ=fake.env(),
                        cwd=fake.root)
    runner = Runner(emitter, Interaction(emitter, json_mode=True, stdin=io.StringIO(), prompt_stream=out),
                    env=ctx.command_env())
    code = verify.run(ctx, runner, emitter, http=http)
    evts = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, {e["id"]: e for e in evts if e["type"] == "check"}, evts


def test_healthy_deployment_passes(fake):
    deployed(fake)
    http = FakeHttp()
    code, checks, evts = run_verify(fake, http)
    assert code == 0, evts
    assert {c["status"] for c in checks.values()} == {"ok"}
    assert checks["stack_vigie-dev"]["detail"] == "UPDATE_COMPLETE (중첩 스택 2개 정상)"
    assert checks["api_auth"]["detail"] == "POST /llm1 → 401 (막힘)"
    assert checks["frontend"]["url"] == FRONTEND
    assert checks["dashboard"]["url"] == ("https://ap-northeast-2.console.aws.amazon.com/cloudwatch/home"
                                          "?region=ap-northeast-2#dashboards/dashboard/vigie-dev-service")
    assert checks["access_denied"]["detail"] == "0건 (로그 그룹 4개 확인)"   # 없는 로그 그룹 1개는 제외
    assert set(http.calls) == {("POST", f"{API}/llm1"), ("GET", f"{API}/health"), ("GET", FRONTEND)}


def test_verify_only_reads(fake):
    deployed(fake)
    run_verify(fake, FakeHttp())
    for call in fake.calls("aws"):
        joined = " ".join(call["args"])
        assert any(allowed in joined for allowed in READ_ONLY), joined


def test_missing_stack_fails(fake):
    deployed(fake, stacks=healthy_stacks(**{"vigie-mcp-dev": None}))
    code, checks, _ = run_verify(fake, FakeHttp())
    assert code == 1 and checks["stack_vigie-mcp-dev"]["detail"] == "스택이 없습니다"


def test_nothing_deployed_stops_with_one_line(fake):
    # 배포 전에 돌리면 나머지 검사도 모두 같은 이유로 실패하므로, 한 항목으로 알리고 끝낸다
    deployed(fake, stacks=[stack("vigie-prod", root=None)])   # 다른 환경의 스택만 있다
    http = FakeHttp()
    code, checks, evts = run_verify(fake, http)
    assert code == 1
    assert list(checks) == ["stacks"]
    assert checks["stacks"]["detail"] == "dev 환경에 배포된 스택이 없습니다"
    assert checks["stacks"]["hint"] == "deploy를 먼저 실행하세요"
    assert evts[-1]["summary"] == "배포된 것이 없습니다 — deploy를 먼저 실행하세요"
    assert http.calls == []


def test_broken_nested_stack_fails_even_if_root_is_complete(fake):
    deployed(fake, stacks=healthy_stacks(**{"vigie-dev-LlmStack-A": stack(
        "vigie-dev-LlmStack-A", status="UPDATE_ROLLBACK_COMPLETE", root="vigie-dev", reason="LlmMethod 실패")}))
    code, checks, _ = run_verify(fake, FakeHttp())
    assert code == 1
    assert checks["stack_vigie-dev"]["detail"] == "vigie-dev-LlmStack-A: UPDATE_ROLLBACK_COMPLETE"
    assert checks["stack_vigie-dev"]["raw"] == "LlmMethod 실패"   # CloudFormation이 남긴 원인 원문


def test_unauthenticated_success_is_a_failure(fake):
    # 인증 없이 LLM API가 호출되면 누구나 Anthropic 요금을 쓸 수 있다
    deployed(fake)
    code, checks, _ = run_verify(fake, FakeHttp(responses={("POST", f"{API}/llm1"): 200}))
    assert code == 1 and checks["api_auth"]["status"] == "fail" and "인증 없이" in checks["api_auth"]["detail"]


@pytest.mark.parametrize("key", [("POST", f"{API}/llm1"), ("GET", f"{API}/health")])
def test_unreachable_api_fails(fake, key):
    deployed(fake)
    http = FakeHttp()
    del http.responses[key]
    code, _, _ = run_verify(fake, http)
    assert code == 1


def test_missing_api_id_fails_api_checks(fake):
    fake.add("aws", "ssm get-parameter", stderr="An error occurred (ParameterNotFound)\n", exit=254)
    deployed(fake)
    code, checks, _ = run_verify(fake, FakeHttp())
    assert code == 1 and checks["api_auth"]["status"] == checks["api_health"]["status"] == "fail"


def test_access_denied_is_listed_as_warning(fake):
    deployed(fake, access_denied={"/aws/lambda/vigie-mcp-dev": [
        "User: arn:aws:sts::123:assumed-role/vigie-mcp-role is not authorized to perform: guardduty:ListDetectors "
        "(AccessDeniedException)\n   추가 줄"]})
    code, checks, _ = run_verify(fake, FakeHttp())
    denied = checks["access_denied"]
    assert code == 0 and denied["status"] == "warn"
    assert denied["detail"].startswith("1건: /aws/lambda/vigie-mcp-dev: User: arn:aws:sts::123:assumed-role/")
    assert "\n" not in denied["detail"]


def test_frontend_not_ready_is_a_warning(fake):
    deployed(fake)
    code, checks, _ = run_verify(fake, FakeHttp(responses={("GET", FRONTEND): 404}))
    assert code == 0 and checks["frontend"]["status"] == "warn" and checks["frontend"]["url"] == FRONTEND


def test_missing_dashboard_fails(fake):
    deployed(fake, dashboard=False)
    code, checks, _ = run_verify(fake, FakeHttp())
    assert code == 1 and checks["dashboard"]["status"] == "fail"


def test_other_environment_is_ignored(fake):
    # vigie-prod가 ROLLBACK_COMPLETE여도 dev 검증에는 영향이 없다
    deployed(fake)
    code, checks, _ = run_verify(fake, FakeHttp())
    assert code == 0 and not any("vigie-prod" in c["detail"] for c in checks.values())


def test_commands_are_registered(fake):
    for command in ("setup", "deploy", "verify"):
        result = run_cli(fake, command, "--help")
        assert result.returncode == 0 and "--dry-run" in result.stdout
    assert "--alarm-email" in run_cli(fake, "deploy", "--help").stdout
    assert "--admin-email" in run_cli(fake, "deploy", "--help").stdout
