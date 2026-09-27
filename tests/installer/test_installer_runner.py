"""명령 실행(runner.py): 읽기 전용·변경 명령 구분, dry-run, 승인 절차, 실시간 로그, 비밀 값 처리"""
import io
import json
import os
import stat

import pytest

from vigie_installer.events import JsonEmitter, Redactor, TextEmitter
from vigie_installer.runner import (DECLINED, DRY_RUN, EXECUTED, RC_NOT_FOUND, RC_TIMEOUT, Interaction,
                                  Runner, secret_file)


def make_runner(fake, *, json_mode=True, stdin="", dry_run=False, assume_yes=False):
    """가짜 CLI 환경에서 동작하는 Runner와 출력 버퍼를 만든다."""
    out = io.StringIO()
    redactor = Redactor()
    emitter = (JsonEmitter if json_mode else TextEmitter)(redactor, out)
    interaction = Interaction(emitter, json_mode=json_mode, stdin=io.StringIO(stdin), prompt_stream=out)
    runner = Runner(emitter, interaction, env=fake.env(), dry_run=dry_run, assume_yes=assume_yes)
    return runner, out


def output_events(out):
    return [json.loads(line) for line in out.getvalue().splitlines()]


def approve(id_, approved=True):
    return json.dumps({"type": "confirm_response", "id": id_, "approved": approved}) + "\n"


PUT = ["aws", "ssm", "put-parameter", "--name", "/vigie/dev/X"]


# ---- 읽기 전용 명령 ----

def test_run_returns_output(fake):
    fake.add("aws", "--version", "aws-cli/2.17.0\n")
    runner, _ = make_runner(fake)
    result = runner.run(["aws", "--version"])
    assert result.ok and result.stdout == "aws-cli/2.17.0\n" and result.outcome == EXECUTED


def test_run_reports_missing_command(fake):
    runner, _ = make_runner(fake)
    result = runner.run(["no-such-tool", "--version"])
    assert result.returncode == RC_NOT_FOUND and not result.ok


def test_run_times_out(fake):
    fake.add("aws", "sts", sleep=5)
    runner, _ = make_runner(fake)
    result = runner.run(["aws", "sts", "get-caller-identity"], timeout=0.5)
    assert result.returncode == RC_TIMEOUT


def test_read_only_commands_run_even_in_dry_run(fake):
    # dry-run에서도 현재 상태를 읽어야 "무엇을 할지"를 정확히 보여 줄 수 있다
    fake.add("aws", "get-parameter", "{}")
    runner, _ = make_runner(fake, dry_run=True)
    assert runner.run(["aws", "ssm", "get-parameter"]).ok
    assert len(fake.calls("aws")) == 1


def test_child_process_cannot_read_our_stdin(fake, tmp_path):
    # stdin은 질문에 대한 답이 들어오는 통로다. 자식 명령이 읽어 가면 다음 승인 응답이 사라진다
    script = tmp_path / "bin" / "reader"
    script.write_text("#!/bin/bash\n/bin/cat\n")
    script.chmod(0o755)
    runner, _ = make_runner(fake, stdin="보낸 응답\n")
    assert runner.run(["reader"]).stdout == ""
    assert runner.interaction.stdin.readline() == "보낸 응답\n"


# ---- 변경 명령: dry-run과 승인 ----

def test_change_in_dry_run_shows_command_without_running(fake):
    fake.add("aws", "put-parameter")
    runner, out = make_runner(fake, dry_run=True)
    result = runner.change(PUT, id_="put_ssm", reason="SSM에 값을 저장합니다")
    assert result.outcome == DRY_RUN and not result.ok
    assert fake.calls("aws") == []
    event = output_events(out)[0]
    assert event == {"type": "dry_run", "id": "put_ssm", "reason": "SSM에 값을 저장합니다",
                     "command": "aws ssm put-parameter --name /vigie/dev/X"}


def test_change_runs_after_approval(fake):
    fake.add("aws", "put-parameter", '{"Version": 1}')
    runner, out = make_runner(fake, stdin=approve("put_ssm"))
    result = runner.change(PUT, id_="put_ssm", reason="SSM에 값을 저장합니다")
    assert result.ok
    assert output_events(out)[0]["type"] == "confirm_required"
    assert len(fake.calls("aws")) == 1


@pytest.mark.parametrize("stdin", [
    approve("put_ssm", approved=False),                                     # 거절
    "",                                                                     # 응답 없이 stdin이 닫힘
    "이건 JSON이 아님\n",                                                     # 형식 오류
    approve("other_id"),                                                    # 다른 질문에 대한 응답
    json.dumps({"type": "confirm_response", "id": "put_ssm", "approved": "yes"}) + "\n",  # true가 아님
])
def test_change_is_not_run_unless_clearly_approved(fake, stdin):
    fake.add("aws", "put-parameter")
    runner, _ = make_runner(fake, stdin=stdin)
    result = runner.change(PUT, id_="put_ssm", reason="SSM에 값을 저장합니다")
    assert result.outcome == DECLINED
    assert fake.calls("aws") == []


def test_invalid_response_is_not_echoed(fake):
    # 잘못 보낸 줄에 비밀 값이 들어 있을 수 있으므로 오류 메시지에 원문을 넣지 않는다
    fake.add("aws", "put-parameter")
    runner, out = make_runner(fake, stdin='{"value": "sk-ant-LEAK"\n')
    runner.change(PUT, id_="put_ssm", reason="저장")
    assert "sk-ant-LEAK" not in out.getvalue()


def test_assume_yes_skips_question_but_logs_command(fake):
    fake.add("aws", "put-parameter")
    runner, out = make_runner(fake, assume_yes=True)
    assert runner.change(PUT, id_="put_ssm", reason="저장").ok
    kinds = [e["type"] for e in output_events(out)]
    assert "confirm_required" not in kinds
    assert any(e["type"] == "log" and "자동 승인" in e["line"] for e in output_events(out))


def test_assume_yes_does_not_apply_to_irreversible_changes(fake):
    # teardown처럼 되돌릴 수 없는 작업은 --yes가 있어도 반드시 묻는다
    fake.add("aws", "delete-stack")
    runner, _ = make_runner(fake, assume_yes=True, stdin="")
    result = runner.change(["aws", "cloudformation", "delete-stack"], id_="delete", reason="삭제",
                           allow_assume_yes=False)
    assert result.outcome == DECLINED and fake.calls("aws") == []


@pytest.mark.parametrize("answer, expected", [("y\n", EXECUTED), ("yes\n", EXECUTED), ("\n", DECLINED),
                                              ("n\n", DECLINED)])
def test_text_mode_asks_y_n(fake, answer, expected):
    fake.add("aws", "put-parameter")
    runner, out = make_runner(fake, json_mode=False, stdin=answer)
    assert runner.change(PUT, id_="put_ssm", reason="저장").outcome == expected
    assert "[y/N]" in out.getvalue()


# ---- 실시간 로그 ----

def test_streaming_emits_each_line_and_calls_on_line(fake):
    fake.add("deploy", "dev", stdout="====== 1. 업로드 ======\n완료\n", stderr="경고\n")
    runner, out = make_runner(fake)
    seen = []
    result = runner.run(["deploy", "dev"], stream=True, on_line=lambda line, name: seen.append((name, line)))
    logs = [(e["stream"], e["line"]) for e in output_events(out) if e["type"] == "log"]
    assert result.ok and result.stdout == "====== 1. 업로드 ======\n완료\n"
    assert sorted(logs) == sorted(seen) == sorted(
        [("stdout", "====== 1. 업로드 ======"), ("stdout", "완료"), ("stderr", "경고")])


# ---- 비밀 값 ----

def test_secret_from_app_is_redacted_everywhere_afterwards(fake):
    fake.add("echo-tool", "", stdout="값은 sk-ant-SECRET 입니다\n")
    stdin = json.dumps({"type": "secret_response", "id": "anthropic", "value": "sk-ant-SECRET"}) + "\n"
    runner, out = make_runner(fake, stdin=stdin)
    assert runner.interaction.secret("anthropic", "Anthropic API 키") == "sk-ant-SECRET"
    runner.run(["echo-tool"], stream=True)   # 명령 출력에 비밀 값이 섞여 나와도
    runner.emitter.error("x", "실패: sk-ant-SECRET")
    assert "sk-ant-SECRET" not in out.getvalue()
    assert output_events(out)[0] == {"type": "input_required", "id": "anthropic",
                                     "prompt": "Anthropic API 키", "secret": True}


def test_secret_file_is_private_and_removed(tmp_path):
    with secret_file({"Value": "sk-ant-SECRET"}, directory=str(tmp_path)) as path:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert json.loads(open(path).read()) == {"Value": "sk-ant-SECRET"}
    assert not os.path.exists(path)


def test_secret_file_is_removed_even_on_error(tmp_path):
    with pytest.raises(RuntimeError):
        with secret_file({"Value": "x"}, directory=str(tmp_path)) as path:
            raise RuntimeError("명령 실패")
    assert not os.path.exists(path)


# ---- 선택 (유지/덮어쓰기 등) ----

OPTIONS = [("keep", "기존 값 유지"), ("overwrite", "덮어쓰기")]


def choose_response(choice):
    return json.dumps({"type": "choice_response", "id": "existing_x", "choice": choice}) + "\n"


@pytest.mark.parametrize("stdin, expected", [
    (choose_response("overwrite"), "overwrite"),
    (choose_response("keep"), "keep"),
    (choose_response("delete-everything"), "keep"),   # 선택지에 없는 값 → 기본값
    ("", "keep"),                                      # 응답 없음 → 기본값 (오류로 알리지 않음)
])
def test_choose_in_json_mode(fake, stdin, expected):
    runner, out = make_runner(fake, stdin=stdin)
    assert runner.interaction.choose("existing_x", "이미 있습니다", OPTIONS, default="keep") == expected
    evts = output_events(out)
    assert evts[0] == {"type": "choice_required", "id": "existing_x", "prompt": "이미 있습니다", "default": "keep",
                       "options": [{"id": "keep", "label": "기존 값 유지"}, {"id": "overwrite", "label": "덮어쓰기"}]}
    assert not any(e["type"] == "error" for e in evts)


@pytest.mark.parametrize("answer, expected", [("2\n", "overwrite"), ("\n", "keep"), ("9\n", "keep"), ("x\n", "keep")])
def test_choose_in_text_mode(fake, answer, expected):
    runner, out = make_runner(fake, json_mode=False, stdin=answer)
    assert runner.interaction.choose("existing_x", "이미 있습니다", OPTIONS, default="keep") == expected
    assert "2) 덮어쓰기" in out.getvalue() and "[기본 1]" in out.getvalue()


def test_command_shows_extra_env(fake):
    runner, out = make_runner(fake, dry_run=True)
    runner.change(["./deploy.sh", "dev"], id_="deploy", reason="배포",
                  extra_env={"AWS_REGION": "ap-northeast-2", "ALARM_EMAIL": "a b@example.com"})
    assert output_events(out)[0]["command"] == "AWS_REGION=ap-northeast-2 ALARM_EMAIL='a b@example.com' ./deploy.sh dev"


def test_streaming_timeout_kills_child_processes(fake, tmp_path):
    # deploy.sh처럼 자식을 띄우는 명령이 제한 시간을 넘기면 자식까지 모두 종료한다
    script = tmp_path / "bin" / "slow"
    pid_file = tmp_path / "child.pid"
    script.write_text(f"#!/bin/bash\n/bin/bash -c 'echo $$ > {pid_file}; exec /bin/sleep 30'\n")
    script.chmod(0o755)
    runner, out = make_runner(fake)
    result = runner.run(["slow"], stream=True, timeout=1)
    child = int(pid_file.read_text())
    assert result.returncode == RC_TIMEOUT and not result.interrupted
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert any("중단합니다" in e["line"] for e in output_events(out) if e["type"] == "log")


# ---- 여러 명령 한 번에 승인 (teardown 단계 등) ----

def test_approve_shows_all_commands_at_once(fake):
    runner, out = make_runner(fake, stdin=approve("delete_logs"))
    outcome = runner.approve("delete_logs", "로그 그룹을 지웁니다", ["aws logs delete-log-group a", "aws logs delete-log-group b"])
    assert outcome == EXECUTED
    assert output_events(out)[0]["command"] == "aws logs delete-log-group a\naws logs delete-log-group b"


def test_run_approved_never_runs_in_dry_run(fake):
    # 단계 코드가 실수로 dry-run 결과를 무시하고 run_approved를 불러도 실행되지 않는다 (이중 안전장치)
    fake.add("aws", "delete-bucket")
    runner, _ = make_runner(fake, dry_run=True)
    assert runner.approve("x", "삭제", ["aws s3api delete-bucket"]) == DRY_RUN
    assert runner.run_approved(["aws", "s3api", "delete-bucket"]).outcome == DRY_RUN
    assert fake.calls("aws") == []


def test_approve_can_refuse_assume_yes(fake):
    runner, _ = make_runner(fake, assume_yes=True, stdin="")
    assert runner.approve("x", "삭제", ["rm"], allow_assume_yes=False) == DECLINED
    assert runner.approve("y", "변경", ["change"]) == EXECUTED


@pytest.mark.parametrize("stdin, expected", [
    (json.dumps({"type": "text_response", "id": "confirm_env", "value": " dev "}) + "\n", "dev"),
    ("", ""),
    (json.dumps({"type": "text_response", "id": "other", "value": "dev"}) + "\n", ""),
])
def test_text_input(fake, stdin, expected):
    runner, out = make_runner(fake, stdin=stdin)
    assert runner.interaction.text("confirm_env", "환경 이름을 입력하세요") == expected
    assert output_events(out)[0] == {"type": "input_required", "id": "confirm_env", "prompt": "환경 이름을 입력하세요",
                                     "secret": False}


class TtyInput(io.StringIO):
    """터미널처럼 보이는 입력 (isatty가 True)"""
    def isatty(self):
        return True


def test_secret_on_terminal_is_hidden_by_default(monkeypatch):
    asked = []
    monkeypatch.setattr("vigie_installer.runner.getpass.getpass", lambda prompt, stream: asked.append(prompt) or "v")
    out = io.StringIO()
    interaction = Interaction(TextEmitter(Redactor(), out), json_mode=False, stdin=TtyInput("typed\n"),
                              prompt_stream=out)
    assert interaction.secret("s", "비밀") == "v"
    assert asked == ["  비밀: "]   # 보이지 않게 입력받는 getpass를 쓴다


def test_secret_with_echo_reads_a_visible_line(monkeypatch):
    # 붙여 넣은 긴 키가 들어갔는지 보이게 입력받는다 (getpass를 쓰지 않는다)
    monkeypatch.setattr("vigie_installer.runner.getpass.getpass",
                        lambda *a, **k: pytest.fail("echo=True인데 getpass를 불렀습니다"))
    out = io.StringIO()
    redactor = Redactor()
    interaction = Interaction(TextEmitter(redactor, out), json_mode=False, stdin=TtyInput("sk-ant-VISIBLE\n"),
                              prompt_stream=out)
    assert interaction.secret("s", "Anthropic API 키", echo=True) == "sk-ant-VISIBLE"
    assert out.getvalue() == "  Anthropic API 키: "
    assert redactor.redact("key=sk-ant-VISIBLE") == "key=***"   # 이후 출력에서는 여전히 가린다
