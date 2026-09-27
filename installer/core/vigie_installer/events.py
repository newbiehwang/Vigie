"""이벤트 출력: 단계 엔진이 지금 무엇을 하는지 터미널 또는 호출한 프로그램에 알린다.

출력 형식은 두 가지이고, 단계 코드는 어느 형식인지 모른 채 같은 메서드를 호출한다.
- JSON Lines (`--json`): 한 줄에 JSON 객체 하나. 다른 프로그램이 줄 단위로 읽어 디코딩한다.
- 사람용 텍스트 (기본): 같은 이벤트를 터미널에서 읽기 좋은 모양으로 바꿔 출력한다.

이벤트 종류 (필드는 계획서 2.1절)
    step_started      단계 시작            {step, title}
    step_finished     단계 끝              {step, status: ok|skipped|failed, summary}
    check             점검 항목 하나의 결과  {id, title, status: ok|warn|fail|info, detail, raw?, hint?, url?}
                      (url: 사람이 열어 볼 주소. 예: 프론트엔드, CloudWatch 대시보드)
                      (raw: 실패한 명령이 낸 오류 원문. detail은 우리가 붙인 설명)
    log               출력 한 줄            {stream: stdout|stderr|info, line}
                      (stdout·stderr는 실행한 명령의 출력, info는 설치 마법사 자신의 안내)
    progress          긴 작업의 진행 단계    {step, phase, label}
    confirm_required  변경 작업 승인 요청    {id, command, reason}
    input_required    값 입력 요청          {id, prompt, secret}
    choice_required   선택지 중 하나 요청    {id, prompt, options: [{id, label}], default}
    dry_run           dry-run이라 실행하지 않은 변경 작업 {id, command, reason}
    error             오류와 해결 안내       {step, message, raw?, hint?}
                      (raw: 실패한 명령이 낸 오류 원문. message는 무엇이 실패했는지 우리가 붙인 설명)

비밀 값 가리기
    모든 이벤트는 출력 직전에 Redactor를 통과한다. 사용자가 입력한 비밀 값(API 키 등)을
    Redactor에 등록해 두면, 이벤트의 어떤 문자열 필드에 섞여 들어가도 `***`로 바뀐다.
    단계 코드가 실수로 비밀 값을 로그에 넣더라도 화면·로그 파일로 새지 않게 하는 마지막 방어선이다.
"""
import json
import shlex
import sys
import unicodedata
from typing import Any, TextIO

MASK = "***"

# 점검 항목(check 이벤트)의 상태
CHECK_OK = "ok"       # 통과
CHECK_WARN = "warn"   # 진행은 가능하지만 알아 둘 것 (예: 루트 계정, gh 미설치)
CHECK_FAIL = "fail"   # 이 상태로는 다음 단계를 진행할 수 없음
CHECK_INFO = "info"   # 판단 없이 알려 주는 정보 (예: 리전, 무료 플랜 제약)

# 단계(step_finished 이벤트)의 상태
STEP_OK = "ok"
STEP_SKIPPED = "skipped"   # 하지 않았음 (사용자가 거절함 등). 이미 되어 있어 할 일이 없었던 경우는 ok
STEP_FAILED = "failed"


class Redactor:
    """등록된 비밀 값을 문자열에서 찾아 `***`로 바꾼다."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, secret: str | None) -> None:
        # 빈 문자열을 등록하면 모든 문자 사이에 ***가 끼어들므로 무시한다
        if secret:
            self._secrets.add(secret)

    def redact(self, text: str) -> str:
        # 긴 값부터 바꾼다. 짧은 비밀 값이 긴 비밀 값의 일부일 때 짧은 것부터 바꾸면
        # 긴 값의 나머지 부분이 가려지지 않은 채 남기 때문이다.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, MASK)
        return text

    def redact_obj(self, value: Any) -> Any:
        """dict·list 안쪽까지 내려가 모든 문자열을 가린다 (키는 우리가 정한 이름이라 그대로 둔다)."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self.redact_obj(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_obj(item) for item in value]
        return value


def format_command(cmd: list[str], env: dict[str, str] | None = None) -> str:
    """사용자에게 보여 줄 명령 문자열. 셸에 그대로 붙여 넣어도 되도록 인자를 따옴표로 감싼다.
    env를 주면 `AWS_REGION=ap-northeast-2 ./deploy.sh dev`처럼 앞에 붙여, 명령이 어떤 설정으로
    실행되는지 한눈에 보이게 한다. (비밀 값은 이 문자열이 이벤트로 나갈 때 Redactor가 가린다)"""
    prefix = [f"{name}={shlex.quote(value)}" for name, value in (env or {}).items()]
    return " ".join([*prefix, shlex.join(cmd)])


class Emitter:
    """이벤트를 만드는 공통 부분. 실제로 쓰는 방식은 하위 클래스의 `_write`가 정한다."""

    def __init__(self, redactor: Redactor, stream: TextIO | None = None) -> None:
        self.redactor = redactor
        self.stream = stream or sys.stdout

    def emit(self, type_: str, **fields: Any) -> dict:
        # None인 선택 필드(hint 등)는 빼서 읽는 쪽이 "값 없음"과 "빈 문자열"을 구분하지 않아도 되게 한다
        event = {"type": type_, **{k: v for k, v in fields.items() if v is not None}}
        event = self.redactor.redact_obj(event)
        self._write(event)
        return event

    def _write(self, event: dict) -> None:  # pragma: no cover - 하위 클래스에서 구현
        raise NotImplementedError

    @property
    def indent(self) -> str:
        """지금 출력할 줄의 들여쓰기. 터미널에 질문을 직접 쓰는 쪽(runner.Interaction)이 맞춰 쓴다."""
        return "  "

    # 아래는 단계 코드가 이벤트 필드 이름을 외우지 않아도 되게 하는 편의 메서드다.

    def step_started(self, step: str, title: str) -> None:
        self.emit("step_started", step=step, title=title)

    def step_finished(self, step: str, status: str, summary: str) -> None:
        self.emit("step_finished", step=step, status=status, summary=summary)

    def check(self, id_: str, title: str, status: str, detail: str, hint: str | None = None,
              url: str | None = None, raw: str | None = None) -> None:
        self.emit("check", id=id_, title=title, status=status, detail=detail, raw=raw, hint=hint, url=url)

    def log(self, line: str, stream: str = "stdout") -> None:
        self.emit("log", stream=stream, line=line)

    def progress(self, step: str, phase: str, label: str) -> None:
        self.emit("progress", step=step, phase=phase, label=label)

    def confirm_required(self, id_: str, command: str, reason: str) -> None:
        self.emit("confirm_required", id=id_, command=command, reason=reason)

    def input_required(self, id_: str, prompt: str, secret: bool = True) -> None:
        self.emit("input_required", id=id_, prompt=prompt, secret=secret)

    def choice_required(self, id_: str, prompt: str, options: list[tuple[str, str]], default: str) -> None:
        self.emit("choice_required", id=id_, prompt=prompt, default=default,
                  options=[{"id": option_id, "label": label} for option_id, label in options])

    def dry_run(self, id_: str, command: str, reason: str) -> None:
        self.emit("dry_run", id=id_, command=command, reason=reason)

    def error(self, step: str, message: str, hint: str | None = None, raw: str | None = None) -> None:
        self.emit("error", step=step, message=message, raw=raw, hint=hint)


class JsonEmitter(Emitter):
    """다른 프로그램용: 이벤트 하나를 JSON 한 줄로 쓴다."""

    def _write(self, event: dict) -> None:
        # ensure_ascii=False: 한글을 \uXXXX로 바꾸지 않아 로그를 그대로 읽을 수 있다.
        # flush: 파이프로 연결되면 출력이 버퍼에 쌓여 읽는 쪽이 늦게 받으므로 줄마다 내보낸다.
        self.stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.stream.flush()


# 사람용 출력에서 상태마다 앞에 붙이는 표시
_CHECK_MARKS = {CHECK_OK: "[ OK ]", CHECK_WARN: "[주의]", CHECK_FAIL: "[오류]", CHECK_INFO: "[정보]"}
_STEP_TAGS = {STEP_OK: "[완료]", STEP_SKIPPED: "[건너뜀]", STEP_FAILED: "[오류]"}
_COMMAND_MARKS = {STEP_OK: "✓", STEP_SKIPPED: "·", STEP_FAILED: "✗"}


def display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글·한자 같은 넓은 글자는 두 칸이라 글자 수로 맞추면 줄이 어긋난다
    (결과 표시 아래 줄을 제목 글자에 맞춰 들여 쓸 때 쓴다)."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


class _Step:
    """출력 중인 단계 하나 (하위 단계의 제목을 언제 쓸지 정하는 데 쓴다)."""

    def __init__(self, id_: str, title: str, depth: int) -> None:
        self.id = id_
        self.title = title
        self.depth = depth
        self.opened = depth == 0          # 명령 전체의 제목은 시작할 때 바로 쓴다
        self.errors: list[dict] = []      # 제목을 쓰기 전에 나온 오류 (결과 줄 아래에 모아 쓴다)
        self.planned = False              # dry-run이라 실행하지 않은 변경 작업이 있었음 → [완료] 대신 [예정]


class TextEmitter(Emitter):
    """터미널용: 설치 도구에서 흔한 모양으로 쓴다.

        사전 설정 (dev, ap-southeast-2)
          [완료] API Gateway 통합 타임아웃 할당량
          [오류] SSM 파라미터
                 /vigie/dev/ANTHROPIC_API_KEY를 저장하지 못했습니다
                 An error occurred (AccessDeniedException) when calling the PutParameter operation: ...
        ✗ 사전 설정을 끝내지 못했습니다

    - 하위 단계는 한 줄로 끝낸다: 결과 표시([완료] 등)를 점검 항목처럼 왼쪽에 붙여 끝날 때 한꺼번에 쓴다.
      도중에 질문·로그가 나오면 제목을 먼저 쓰고, 끝날 때 결과 줄을 다시 쓴다 (질문과 답이 제목 아래에 오도록).
    - 성공하면 [완료]만 쓴다. 할 말(요약)이 있을 때만 다음 줄에 쓴다.
      dry-run에서 바꿀 일이 있었던 단계는 [예정]이다 (바꾸지 않았는데 "완료"라고 하면 헷갈린다).
    - 오류는 [오류] 다음 줄에 무엇이 실패했는지, 그 다음 줄에 명령이 낸 오류 원문을 쓴다.
    - dry-run은 명령 제목에 한 번만 알린다.
    """

    def __init__(self, redactor: Redactor, stream: TextIO | None = None, *, dry_run: bool = False) -> None:
        super().__init__(redactor, stream)
        self.dry_run_mode = dry_run   # (dry_run이라는 이름은 이벤트 메서드 Emitter.dry_run이 쓴다)
        self._steps: list[_Step] = []

    @property
    def indent(self) -> str:
        return "  " * (len(self._steps) if self._steps else 1)

    def _out(self, *lines: str) -> None:
        for line in lines:
            self.stream.write(line + "\n")
        self.stream.flush()

    def _open(self) -> None:
        """아직 쓰지 않은 하위 단계 제목을 쓴다 (그 안에서 무언가 출력하기 직전에)."""
        for step in self._steps:
            if not step.opened:
                self._out("  " * step.depth + step.title)
                step.opened = True
                self._flush_errors(step)

    def _flush_errors(self, step: _Step) -> None:
        for error in step.errors:
            self._error_lines(error, "  " * (step.depth + 1), tag=True)
        step.errors.clear()

    def _error_lines(self, event: dict, pad: str, *, tag: bool) -> None:
        """오류 하나: [오류] 설명 → 원문 → 해결 안내. tag=False면 윗줄에 이미 [오류]가 있어 설명부터 쓴다."""
        lead = f"{pad}[오류] " if tag else pad
        under = " " * display_width(lead)
        lines = [lead + event["message"]]
        if "raw" in event:
            lines.append(under + event["raw"])
        if "hint" in event:
            lines.append(f"{under}→ {event['hint']}")
        self._out(*lines)

    @staticmethod
    def _tag(step: _Step, status: str) -> str:
        return "[예정]" if status == STEP_OK and step.planned else _STEP_TAGS.get(status, status)

    def _write(self, event: dict) -> None:
        kind = event["type"]
        if kind == "step_started":
            self._open()   # 하위 단계 안의 하위 단계: 바깥 제목을 먼저 쓴다
            step = _Step(event["step"], event["title"], len(self._steps))
            self._steps.append(step)
            if step.depth == 0:
                self._out("", event["title"] + (" · dry-run (아무것도 바꾸지 않음)" if self.dry_run_mode else ""))
            return
        if kind == "step_finished":
            self._finish(event)
            return
        if kind == "error" and self._steps and not self._steps[-1].opened:
            self._steps[-1].errors.append(event)   # 한 줄짜리 결과 아래에 모아 쓴다
            return
        if kind in ("input_required", "choice_required"):
            self._open()   # 질문 문구는 입력을 받는 쪽(runner.Interaction)이 직접 쓴다
            return

        self._open()
        pad = self.indent
        if kind == "check":
            mark = _CHECK_MARKS.get(event["status"], event["status"])
            under = pad + " " * (display_width(mark) + 1)
            lines = [f"{pad}{mark} {event['title']}: {event['detail']}"]
            if "raw" in event:
                lines.append(under + event["raw"])
            if "hint" in event:
                lines.append(f"{under}→ {event['hint']}")
            if "url" in event:
                lines.append(under + event["url"])
            self._out(*lines)
        elif kind == "log":
            marker = {"stderr": "! ", "stdout": "| "}.get(event["stream"], "")
            self._out(pad + marker + event["line"])
        elif kind == "progress":
            self._out(f"{pad}[{event['phase']}] {event['label']}")
        elif kind == "confirm_required":
            self._out(f"{pad}변경 작업: {event['reason']}", *self._command_lines(event["command"], pad))
        elif kind == "dry_run":
            if self._steps:
                self._steps[-1].planned = True
            self._out(f"{pad}할 일: {event['reason']}", *self._command_lines(event["command"], pad))
        elif kind == "error":
            self._error_lines(event, pad, tag=True)
        else:
            self._out(json.dumps(event, ensure_ascii=False))   # 모르는 종류는 원본 그대로

    @staticmethod
    def _command_lines(command: str, pad: str) -> list[str]:
        """명령 여러 개가 줄바꿈으로 이어져 오면(teardown의 버킷 삭제 등) 줄마다 `$`를 붙여 같은 깊이로 쓴다."""
        return [f"{pad}  $ {line}" for line in command.splitlines()]

    def _finish(self, event: dict) -> None:
        # 끝난 단계를 찾는다. 짝이 맞지 않으면(중간 단계가 끝을 알리지 않음) 그 안쪽 것도 함께 닫는다
        index = next((i for i in range(len(self._steps) - 1, -1, -1) if self._steps[i].id == event["step"]), None)
        if index is None:
            self._out(f"{_COMMAND_MARKS.get(event['status'], '·')} {event['summary']}")
            return
        step = self._steps[index]
        del self._steps[index:]
        status, summary = event["status"], event.get("summary", "")
        if step.depth == 0:
            self._out(f"{_COMMAND_MARKS.get(status, '·')} {summary}")
            return
        tag = self._tag(step, status)
        self._out(f"{'  ' * step.depth}{tag} {step.title}")
        # 아랫줄(오류·이유)은 제목 글자에 맞춰 들여 쓴다
        pad = "  " * step.depth + " " * (display_width(tag) + 1)
        if step.errors:
            for error in step.errors:
                self._error_lines(error, pad, tag=False)
            step.errors.clear()
        elif summary:
            self._out(pad + summary)
