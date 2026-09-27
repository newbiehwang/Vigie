"""이벤트 출력(events.py): JSON Lines 형식, 사람용 텍스트, 비밀 값 가리기"""
import io
import json

from vigie_installer.events import (STEP_FAILED, STEP_OK, STEP_SKIPPED, JsonEmitter, Redactor, TextEmitter,
                                  display_width)


def test_json_emitter_writes_one_object_per_line():
    out = io.StringIO()
    emitter = JsonEmitter(Redactor(), out)
    emitter.step_started("check", "사전 점검")
    emitter.check("aws_cli", "AWS CLI", "ok", "2.17.0")
    emitter.step_finished("check", "ok", "통과")

    lines = out.getvalue().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["step_started", "check", "step_finished"]
    assert "사전 점검" in lines[0]   # 한글이 \uXXXX로 바뀌지 않음


def test_optional_fields_are_omitted_when_none():
    out = io.StringIO()
    JsonEmitter(Redactor(), out).check("region", "배포 리전", "info", "ap-northeast-2")
    assert "hint" not in json.loads(out.getvalue())


def test_redactor_masks_secrets_in_every_string_field():
    redactor = Redactor()
    redactor.add("sk-ant-SECRET")
    out = io.StringIO()
    JsonEmitter(redactor, out).emit("custom", message="키: sk-ant-SECRET", nested={"list": ["x sk-ant-SECRET"]})
    event = json.loads(out.getvalue())
    assert "sk-ant-SECRET" not in out.getvalue()
    assert event["message"] == "키: ***" and event["nested"]["list"] == ["x ***"]


def test_redactor_replaces_longer_secret_first():
    # 짧은 값("abc")을 먼저 바꾸면 긴 값("abcdef")의 나머지("def")가 드러난다
    redactor = Redactor()
    redactor.add("abc")
    redactor.add("abcdef")
    assert redactor.redact("값=abcdef") == "값=***"


def test_redactor_ignores_empty_secret():
    redactor = Redactor()
    redactor.add("")
    redactor.add(None)
    assert redactor.redact("그대로") == "그대로"


def test_text_emitter_formats_check_with_hint():
    out = io.StringIO()
    TextEmitter(Redactor(), out).check("aws_cli", "AWS CLI", "fail", "설치되어 있지 않습니다", "brew install awscli")
    assert out.getvalue() == "  [오류] AWS CLI: 설치되어 있지 않습니다\n         → brew install awscli\n"


def test_text_emitter_also_redacts():
    redactor = Redactor()
    redactor.add("xoxb-123")
    out = io.StringIO()
    TextEmitter(redactor, out).log("token=xoxb-123")
    assert "xoxb-123" not in out.getvalue()


# --- 터미널 출력 모양 --------------------------------------------------------------------------------

def text_emitter(**kwargs):
    out = io.StringIO()
    return TextEmitter(Redactor(), out, **kwargs), out


def test_display_width_counts_korean_as_two_columns():
    assert display_width("SSM 파라미터") == 4 + 4 * 2   # "SSM " + 한글 4자
    assert display_width("abc") == 3


def test_substep_is_one_line_with_status_on_the_left():
    emitter, out = text_emitter()
    emitter.step_started("setup", "사전 설정 (dev, ap-southeast-2)")
    emitter.step_started("quota", "API Gateway 통합 타임아웃 할당량")
    emitter.step_finished("quota", STEP_OK, "")
    emitter.step_started("ssm", "SSM 파라미터")
    emitter.step_finished("ssm", STEP_OK, "")
    emitter.step_finished("setup", STEP_OK, "사전 설정 완료")
    # 점검 항목([ OK ] …)처럼 결과 표시가 왼쪽에 온다
    assert out.getvalue().splitlines() == ["", "사전 설정 (dev, ap-southeast-2)",
                                           "  [완료] API Gateway 통합 타임아웃 할당량",
                                           "  [완료] SSM 파라미터",
                                           "✓ 사전 설정 완료"]


def test_failed_substep_shows_error_then_source_under_it():
    emitter, out = text_emitter()
    emitter.step_started("setup", "사전 설정")
    emitter.step_started("quota", "할당량")
    emitter.error("quota", "할당량을 조회하지 못했습니다", raw="An error occurred (AccessDenied) ...",
                  hint="콘솔에서 확인하세요")
    emitter.step_finished("quota", STEP_FAILED, "")
    # [오류] 아랫줄에 무엇이 실패했는지, 그 아랫줄에 오류 원문 (제목 글자에 맞춰 들여 쓴다)
    assert out.getvalue().splitlines()[2:] == ["  [오류] 할당량",
                                               "         할당량을 조회하지 못했습니다",
                                               "         An error occurred (AccessDenied) ...",
                                               "         → 콘솔에서 확인하세요"]


def test_error_outside_a_substep_is_tagged():
    emitter, out = text_emitter()
    emitter.step_started("deploy", "Vigie 배포")
    emitter.error("deploy", "deploy.sh가 실패했습니다", raw="npm ERR! build failed")
    emitter.step_finished("deploy", STEP_FAILED, "배포에 실패했습니다")
    assert out.getvalue().splitlines()[2:] == ["  [오류] deploy.sh가 실패했습니다",
                                               "         npm ERR! build failed",
                                               "✗ 배포에 실패했습니다"]


def test_substep_with_output_prints_title_first():
    # 도중에 질문·로그가 나오면 제목을 먼저 쓰고, 끝나면 결과 줄을 다시 쓴다
    emitter, out = text_emitter()
    emitter.step_started("setup", "사전 설정")
    emitter.step_started("quota", "할당량")
    emitter.log("현재 29000ms → 120000ms 필요", stream="info")
    emitter.step_finished("quota", STEP_OK, "요청했습니다 (PENDING)")
    assert out.getvalue().splitlines()[2:] == ["  할당량",
                                               "    현재 29000ms → 120000ms 필요",
                                               "  [완료] 할당량",
                                               "         요청했습니다 (PENDING)"]


def test_dry_run_is_announced_once_and_planned_steps_say_planned():
    emitter, out = text_emitter(dry_run=True)
    emitter.step_started("setup", "사전 설정")
    emitter.step_started("quota", "할당량")
    emitter.dry_run("request_quota", "aws service-quotas request-service-quota-increase", "할당량을 올려 달라고 요청합니다")
    emitter.step_finished("quota", STEP_OK, "")
    emitter.step_started("ssm", "SSM 파라미터")
    emitter.step_finished("ssm", STEP_OK, "")
    lines = out.getvalue().splitlines()
    assert lines[1] == "사전 설정 · dry-run (아무것도 바꾸지 않음)"
    assert lines[3:5] == ["    할 일: 할당량을 올려 달라고 요청합니다",
                          "      $ aws service-quotas request-service-quota-increase"]
    assert lines[5] == "  [예정] 할당량"          # 바꾸지 않았으므로 "완료"가 아니다
    assert lines[6] == "  [완료] SSM 파라미터"    # 이미 되어 있어 할 일이 없었던 단계
    assert "dry-run" not in "\n".join(lines[2:])   # 줄마다 되풀이하지 않는다



def test_multiple_commands_each_get_their_own_line():
    # teardown처럼 명령 여러 개를 한 번에 보여 줄 때 둘째 줄부터 들여쓰기와 $가 빠지지 않는다
    emitter, out = text_emitter(dry_run=True)
    emitter.step_started("teardown", "Vigie 정리")
    emitter.dry_run("delete_log_groups", "aws logs delete-log-group a\naws logs delete-log-group b", "로그 그룹 2개를 지웁니다")
    assert out.getvalue().splitlines()[2:] == ["  할 일: 로그 그룹 2개를 지웁니다",
                                               "    $ aws logs delete-log-group a",
                                               "    $ aws logs delete-log-group b"]

def test_skipped_substep_gives_the_reason():
    emitter, out = text_emitter()
    emitter.step_started("setup", "사전 설정")
    emitter.step_started("quota", "할당량")
    emitter.step_finished("quota", STEP_SKIPPED, "할당량이 오르기 전에는 배포가 실패합니다")
    assert out.getvalue().splitlines()[2:] == ["  [건너뜀] 할당량",
                                               "           할당량이 오르기 전에는 배포가 실패합니다"]
