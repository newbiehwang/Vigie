"""aws.py의 리소스 이름·설정값이 deploy.sh·CloudFormation 템플릿·README와 일치하는지 확인한다.
(한쪽만 바뀌면 설치 마법사가 엉뚱한 리소스를 확인하거나 배포 전 확인이 틀린 판단을 한다)"""
import re

from vigie_installer.aws import (LAMBDA_FUNCTIONS, REQUIRED_TIMEOUT_MS, SECRET_PARAMS, dashboard_name, main_stacks,
                               parse_time)

from .helpers import ROOT

DEPLOY_SH = (ROOT / "deploy.sh").read_text()
TEMPLATES = {path.name: path.read_text() for path in (ROOT / "cloudformation").glob("*.yaml")}


def test_main_stacks_match_deploy_sh():
    names = dict(re.findall(r'^(\w+_STACK_NAME)="([^"]+)"', DEPLOY_SH, re.M))
    assert sorted(name.replace("$ENV", "dev") for name in names.values()) == sorted(main_stacks("dev"))


def test_lambda_functions_exist_in_templates():
    declared = set()
    for text in TEMPLATES.values():
        declared |= set(re.findall(r"FunctionName: !Sub ['\"]([\w-]+)-\$\{Environment\}['\"]", text))
    assert set(LAMBDA_FUNCTIONS) <= declared


def test_dashboard_name_matches_monitoring_template():
    assert "DashboardName: !Sub 'vigie-${Environment}-service'" in TEMPLATES["monitoring.yaml"]
    assert dashboard_name("dev") == "vigie-dev-service"


def test_required_timeout_matches_llm_template():
    # 할당량이 이 값보다 작으면 스택이 실패한다. 템플릿의 최댓값과 같아야 사전 확인이 맞다
    timeouts = [int(v) for v in re.findall(r"TimeoutInMillis: (\d+)", TEMPLATES["llm.yaml"])]
    assert timeouts and max(timeouts) == REQUIRED_TIMEOUT_MS


def test_latency_alarm_fires_before_integration_timeout():
    # 응답은 통합 타임아웃에서 끊기므로, 알람 임계값이 그 이상이면 알람이 울릴 수 없다
    threshold = int(re.search(r"ApiLatencyP95ThresholdMs:\n(?:\s+.*\n)*?\s+Default: (\d+)",
                              TEMPLATES["monitoring.yaml"]).group(1))
    assert threshold < REQUIRED_TIMEOUT_MS


def test_readme_quota_matches_required_value():
    readme = (ROOT / "README.md").read_text()
    assert f"Maximum integration timeout in milliseconds -> {REQUIRED_TIMEOUT_MS}ms" in readme


def test_secret_params_match_readme():
    readme = (ROOT / "README.md").read_text()
    secure = set(re.findall(r'put-parameter --name "/vigie/\$\{Environment\}/([^"]+)"[^\n]*--type "SecureString"', readme))
    assert {param.key for param in SECRET_PARAMS} == secure


def test_optional_secrets_are_safe_to_omit():
    # Slack 값을 등록하지 않아도 되는 근거: 설정 로더는 있는 파라미터만 읽고,
    # Signing Secret이 없으면 Slack 요청을 거부한다
    config = (ROOT / "layers" / "common" / "config.py").read_text()
    security = (ROOT / "services" / "slackbot" / "slack_security.py").read_text()
    assert "get_parameters_by_path" in config
    assert "if not signing_secret:" in security
    assert [p.key for p in SECRET_PARAMS if p.required] == ["ANTHROPIC_API_KEY"]


def test_api_url_is_built_like_deploy_sh():
    # verify는 deploy.sh와 같은 방법으로 API 주소를 만든다
    assert 'aws ssm get-parameter --name "$SSM_PATH_PREFIX/ApiGatewayId"' in DEPLOY_SH
    assert 'API_URL="https://${API_GATEWAY_ID}.execute-api.${REGION}.amazonaws.com/${ENV}"' in DEPLOY_SH


def test_frontend_output_exists():
    assert "AmplifyAppDefaultDomainWithEnv:" in TEMPLATES["frontend.yaml"]


def test_parse_time_handles_cli_formats():
    assert parse_time("2026-09-22T12:30:04.123000+00:00").year == 2026
    assert parse_time("2026-09-22T12:30:04Z").tzinfo is not None
    assert parse_time("이상한 값") is None and parse_time(None) is None
