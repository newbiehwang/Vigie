"""deploy: 사전 확인, 진행 표시 해석(번호 없는 구분 줄 포함), 실패 요약, 취소 시 자식 프로세스 정리"""
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from vigie_installer.events import JsonEmitter, Redactor
from vigie_installer.steps.deploy import TOTAL_PHASES, ProgressParser

from .helpers import ROOT, events

DEPLOY_SH = (ROOT / "deploy.sh").read_text()
NOW = datetime.now(timezone.utc)


def iso(moment):
    return moment.isoformat()


# ---------------------------------------------------------------- 진행 표시 해석 (실제 deploy.sh 기준)

def real_markers():
    """실제 deploy.sh가 출력하는 구분 줄 전부 (번호 유무와 관계없이)."""
    return re.findall(r'^echo "(====== .+ ======)"$', DEPLOY_SH, re.M)


def parse_lines(lines):
    out = io.StringIO()
    parser = ProgressParser(JsonEmitter(Redactor(), out))
    for line in lines:
        parser.feed(line, "stdout")
    return parser, [json.loads(line) for line in out.getvalue().splitlines()]


def test_every_deploy_sh_marker_becomes_progress():
    markers = real_markers()
    assert len(markers) >= 9, "deploy.sh에서 구분 줄을 찾지 못했습니다"
    parser, progress = parse_lines(markers)
    assert len(progress) == len(markers)   # 번호 없는 줄도 빠짐없이 진행 표시가 된다
    assert parser.finished


def test_total_phases_matches_deploy_sh():
    numbers = [int(n) for n in re.findall(r'^echo "====== (\d+)\. ', DEPLOY_SH, re.M)]
    assert numbers == list(range(1, TOTAL_PHASES + 1))


def test_unnumbered_marker_keeps_previous_phase():
    _, progress = parse_lines(["====== 3. 프론트엔드 인프라 배포 ======",
                               "====== Layer 및 Lambda 함수 패키징 ======",
                               "====== 4. 환경 변수 설정 ======",
                               "====== 배포 완료! ======"])
    assert [(p["phase"], p["label"]) for p in progress] == [
        ("3/6", "프론트엔드 인프라 배포"), ("3/6", "Layer 및 Lambda 함수 패키징"),
        ("4/6", "환경 변수 설정"), ("6/6", "배포 완료!")]


def test_header_rule_and_stderr_are_not_progress():
    parser, progress = parse_lines(["========================================"])
    parser.feed("====== 1. 가짜 ======", "stderr")
    assert progress == [] and parser.phase == 0


# ---------------------------------------------------------------- CLI 실행

def write_deploy(repo, body):
    script = repo / "deploy.sh"
    script.write_text("#!/bin/bash\n" + body)
    script.chmod(0o755)


SUCCESS_SCRIPT = """\
echo "ran" > ran.txt
echo "env: $1 region=$AWS_REGION alarm=$ALARM_EMAIL admin=$ADMIN_EMAIL"
echo "====== 1. CloudFormation 템플릿 업로드 ======"
echo "====== 2. 기본 스택 배포 시작 ======"
echo "경고 한 줄" >&2
echo "====== Layer 및 Lambda 함수 패키징 ======"
echo "====== 6. 배포 완료 요약 ======"
echo "API Gateway URL: https://abc123.execute-api.ap-northeast-2.amazonaws.com/dev"
echo "프론트엔드 URL: dev.d1234.amplifyapp.com"
echo "====== 배포 완료! ======"
"""


def ready_account(fake, *, quota=120000, anthropic=True):
    params = [{"Name": "/vigie/dev/ANTHROPIC_API_KEY", "Type": "SecureString"}] if anthropic else []
    fake.add("aws", "describe-parameters", json.dumps({"Parameters": params}))
    fake.add("aws", "list-service-quotas", json.dumps({"Quotas": [
        {"QuotaName": "Maximum integration timeout in milliseconds", "QuotaCode": "L-X", "Value": float(quota)}]}))


def approve_deploy():
    return json.dumps({"type": "confirm_response", "id": "deploy", "approved": True}) + "\n"


def deploy_cli(fake, repo, *extra, input=""):
    return subprocess.run([sys.executable, "-m", "vigie_installer", "deploy", "--json", "--repo", str(repo),
                           "--region", "ap-northeast-2", *extra],
                          input=input, capture_output=True, text=True, env=fake.env(), timeout=60)


def test_successful_deploy(fake, repo):
    write_deploy(repo, SUCCESS_SCRIPT)
    ready_account(fake)
    result = deploy_cli(fake, repo, "--alarm-email", "me@example.com", "--admin-email", "admin@example.com",
                        input=approve_deploy())
    evts = events(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr

    confirm = next(e for e in evts if e["type"] == "confirm_required")
    assert confirm["command"] == ("AWS_REGION=ap-northeast-2 ALARM_EMAIL=me@example.com ADMIN_EMAIL=admin@example.com "
                                  "./deploy.sh dev")

    logs = [(e["stream"], e["line"]) for e in evts if e["type"] == "log"]
    assert ("stdout", "env: dev region=ap-northeast-2 alarm=me@example.com admin=admin@example.com") in logs
    # 관리자 계정은 새로 만들면 초대 메일이 가고, 이미 있으면 권한만 더한다
    assert any(stream == "info" and "admin@example.com" in line and "초대 메일" in line for stream, line in logs)
    assert ("stderr", "경고 한 줄") in logs
    assert [(e["phase"], e["label"]) for e in evts if e["type"] == "progress"] == [
        ("1/6", "CloudFormation 템플릿 업로드"), ("2/6", "기본 스택 배포 시작"),
        ("2/6", "Layer 및 Lambda 함수 패키징"), ("6/6", "배포 완료 요약"), ("6/6", "배포 완료!")]

    done = evts[-1]
    assert done["status"] == "ok" and done["summary"] == "배포 완료"
    # 주소는 한 줄에 하나씩 (복사하기 쉽게)
    assert ("info", "API: https://abc123.execute-api.ap-northeast-2.amazonaws.com/dev") in logs
    assert ("info", "프론트엔드: https://dev.d1234.amplifyapp.com") in logs
    assert any("구독 확인 메일" in e.get("line", "") for e in evts)


def test_missing_anthropic_key_stops_before_deploy(fake, repo):
    write_deploy(repo, SUCCESS_SCRIPT)
    ready_account(fake, anthropic=False)
    result = deploy_cli(fake, repo, input=approve_deploy())
    assert result.returncode == 1 and not (repo / "ran.txt").exists()
    assert any(e["type"] == "error" and "ANTHROPIC_API_KEY" in e["message"] for e in events(result.stdout))


def test_low_quota_stops_before_deploy(fake, repo):
    # llm.yaml이 120000ms를 쓰므로 할당량이 낮으면 스택이 실패한다 → 20~40분 기다리기 전에 멈춘다
    write_deploy(repo, SUCCESS_SCRIPT)
    ready_account(fake, quota=29000)
    result = deploy_cli(fake, repo, input=approve_deploy())
    error = next(e for e in events(result.stdout) if e["type"] == "error")
    assert result.returncode == 1 and not (repo / "ran.txt").exists()
    assert "29000ms" in error["message"] and "setup" in error["hint"]


def test_quota_lookup_failure_does_not_block(fake, repo):
    write_deploy(repo, SUCCESS_SCRIPT)
    fake.add("aws", "list-service-quotas", stderr="AccessDeniedException\n", exit=254)
    ready_account(fake)
    result = deploy_cli(fake, repo, input=approve_deploy())
    assert result.returncode == 0 and (repo / "ran.txt").exists()


def test_dry_run_does_not_deploy(fake, repo):
    write_deploy(repo, SUCCESS_SCRIPT)
    ready_account(fake)
    result = deploy_cli(fake, repo, "--dry-run")
    evts = events(result.stdout)
    assert result.returncode == 0 and not (repo / "ran.txt").exists()
    assert [e["command"] for e in evts if e["type"] == "dry_run"] == ["AWS_REGION=ap-northeast-2 ./deploy.sh dev"]


def test_declined_deploy_is_skipped(fake, repo):
    write_deploy(repo, SUCCESS_SCRIPT)
    ready_account(fake)
    result = deploy_cli(fake, repo, input="")   # 응답 없음 = 거절
    assert result.returncode == 0 and not (repo / "ran.txt").exists()
    assert events(result.stdout)[-1]["status"] == "skipped"


def stack_events(*items):
    return json.dumps({"StackEvents": list(items)})


def event(stack, logical, status, reason="", *, rtype="AWS::ApiGateway::Method", physical="phys", when=None):
    stack_id = f"arn:aws:cloudformation:ap-northeast-2:123:stack/{stack}/id"
    return {"StackName": stack, "StackId": stack_id, "LogicalResourceId": logical, "ResourceType": rtype,
            "PhysicalResourceId": stack_id if physical == "self" else physical, "ResourceStatus": status,
            "ResourceStatusReason": reason, "Timestamp": iso(when or NOW + timedelta(seconds=30))}


NESTED_ARN = "arn:aws:cloudformation:ap-northeast-2:123:stack/vigie-dev-LlmStack-ABC/xyz"


def test_failure_summary_finds_root_cause_in_nested_stack(fake, repo):
    write_deploy(repo, 'echo "====== 2. 기본 스택 배포 시작 ======"\necho "An error occurred" >&2\nexit 255\n')
    ready_account(fake)
    fake.add("aws", f"describe-stack-events --stack-name {NESTED_ARN}", stack_events(
        event("vigie-dev-LlmStack-ABC", "LlmMethod", "CREATE_FAILED",
              "Timeout should be between 50 ms and 29000 ms"),
        event("vigie-dev-LlmStack-ABC", "LlmOptionsMethod", "CREATE_FAILED", "Resource creation cancelled")))
    fake.add("aws", "describe-stack-events --stack-name vigie-dev --max-items", stack_events(
        event("vigie-dev", "vigie-dev", "UPDATE_ROLLBACK_IN_PROGRESS", physical="self"),
        event("vigie-dev", "LlmStack", "UPDATE_FAILED", "Embedded stack was not successfully updated",
              rtype="AWS::CloudFormation::Stack", physical=NESTED_ARN),
        # 이번 배포 이전의 오래된 실패는 보여 주지 않는다
        event("vigie-dev", "OldThing", "CREATE_FAILED", "지난달 실패", when=NOW - timedelta(days=30))))
    fake.add("aws", "describe-stack-events", stderr="Stack with id x does not exist\n", exit=254)

    result = deploy_cli(fake, repo, input=approve_deploy())
    errors = [(e["message"], e.get("raw")) for e in events(result.stdout) if e["type"] == "error"]
    assert result.returncode == 1
    # 어느 리소스가 실패했는지와, CloudFormation이 남긴 원인 원문을 나눠 보낸다
    assert errors == [("vigie-dev-LlmStack-ABC: LlmMethod (AWS::ApiGateway::Method)",
                       "Timeout should be between 50 ms and 29000 ms")]
    assert events(result.stdout)[-1]["summary"] == "배포에 실패했습니다 (deploy.sh 종료 코드 255)"


def test_failure_without_stack_events_shows_last_error_line(fake, repo):
    write_deploy(repo, 'echo "npm ERR! build failed" >&2\nexit 1\n')
    ready_account(fake)
    fake.add("aws", "describe-stack-events", stack_events())
    result = deploy_cli(fake, repo, input=approve_deploy())
    error = next(e for e in events(result.stdout) if e["type"] == "error")
    assert error["message"] == "deploy.sh가 실패했습니다" and error["raw"] == "npm ERR! build failed"


# ---------------------------------------------------------------- 취소: 자식 프로세스까지 정리되는가

CANCEL_SCRIPT = """\
echo "====== 1. 오래 걸리는 작업 ======"
# 전경(foreground) 자식 프로세스: 자신의 PID를 남기고 오래 잠든다 (aws cloudformation wait를 흉내)
/bin/bash -c 'echo $$ > child.pid; exec /bin/sleep 30'
echo "여기까지 오면 안 됨"
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_cancel_stops_deploy_and_its_children(fake, repo, sig):
    write_deploy(repo, CANCEL_SCRIPT)
    ready_account(fake)
    proc = subprocess.Popen([sys.executable, "-m", "vigie_installer", "deploy", "--json", "--repo", str(repo),
                             "--region", "ap-northeast-2", "--yes"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=fake.env())
    lines = []
    for line in proc.stdout:   # 진행 표시가 나오고 자식이 뜰 때까지 기다린다
        lines.append(line)
        if '"progress"' in line:
            break
    pid_file = repo / "child.pid"
    deadline = time.time() + 10
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    child = int(pid_file.read_text())
    assert alive(child)

    started = time.time()
    proc.send_signal(sig)   # Ctrl+C(SIGINT) 또는 종료 요청(SIGTERM)
    rest, _ = proc.communicate(timeout=30)
    assert time.time() - started < 20   # 강제 종료(60초)까지 가지 않고 정리된다
    evts = events("".join(lines) + rest)

    assert proc.returncode == 130
    assert not alive(child), "deploy.sh의 자식 프로세스가 남아 있습니다"
    assert not any("여기까지 오면 안 됨" in e.get("line", "") for e in evts)
    error = next(e for e in evts if e["type"] == "error")
    assert error["message"] == "배포를 취소했습니다" and "IN_PROGRESS" in error["hint"]


def test_key_in_dotenv_lets_deploy_start_without_ssm_parameter(fake, repo):
    # SSM에 키가 없어도 저장소 루트 .env에 있으면 멈추지 않는다 (deploy.sh가 배포 전에 SSM으로 올린다)
    write_deploy(repo, SUCCESS_SCRIPT)
    ready_account(fake, anthropic=False)
    (repo / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-FROM-DOTENV\n")
    result = deploy_cli(fake, repo, input=approve_deploy())
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "ran.txt").exists()
    assert any("저장소 루트 .env" in e.get("line", "") for e in events(result.stdout))
    assert "sk-ant-api03-FROM-DOTENV" not in result.stdout


def test_empty_key_in_dotenv_still_stops_deploy(fake, repo):
    write_deploy(repo, SUCCESS_SCRIPT)
    ready_account(fake, anthropic=False)
    (repo / ".env").write_text("ANTHROPIC_API_KEY=\n")
    result = deploy_cli(fake, repo, input=approve_deploy())
    assert result.returncode == 1 and not (repo / "ran.txt").exists()
