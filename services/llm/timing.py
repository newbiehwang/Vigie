"""질문 하나를 처리하며 단계마다 걸린 시간 (대화 속도를 잴 때 어디가 느린지 보려고 남긴다).

    {"timing": {"totalMs": 8123, "steps": {"models": {"ms": 412, "n": 1}, "mcp_init": {"ms": 11020, "n": 1},
                "history": {"ms": 38, "n": 1}, "model": {"ms": 5210, "n": 2}, "tool:describe_log_groups": {...}}}}

- 같은 이름의 단계는 더한다 (모델 호출이 세 번이면 n=3, ms는 합).
- 끝에 JSON 한 줄로 CloudWatch Logs에 남긴다. Logs Insights에서 `filter ispresent(timing.totalMs)`로 모아 볼 수 있다.
- 값은 로그에만 남고 화면·감사 로그에는 넣지 않는다.
"""
import json
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator


class Stopwatch:
    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._start = time.perf_counter()
        self.steps: Dict[str, Dict[str, int]] = {}

    @classmethod
    def off(cls) -> "Stopwatch":
        """재지 않는 시간 기록 (시간 기록을 받지 못한 클라이언트가 쓴다)."""
        return cls(enabled=False)

    def add(self, name: str, ms: float) -> None:
        if not self._enabled:
            return
        step = self.steps.setdefault(name, {"ms": 0, "n": 0})
        step["ms"] += int(ms)
        step["n"] += 1

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """with timer.step("model"): … 블록이 끝나면(예외가 나도) 걸린 시간을 더한다."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, (time.perf_counter() - started) * 1000)

    def total_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)

    def summary(self, **extra: Any) -> Dict[str, Any]:
        return {"timing": {"totalMs": self.total_ms(), "steps": self.steps, **extra}}

    def log(self, **extra: Any) -> None:
        if self._enabled:
            print(json.dumps(self.summary(**extra), ensure_ascii=False))
