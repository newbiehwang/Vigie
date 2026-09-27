"""Vigie 설치 마법사의 단계 엔진 (표준 라이브러리만 사용)

다른 프로그램은 이 패키지를 `python -m vigie_installer <명령> --json`으로 실행하고,
stdout으로 나오는 JSON Lines 이벤트를 읽어 화면에 반영한다. 터미널에서 직접 실행하면
같은 이벤트가 사람이 읽기 좋은 텍스트로 출력된다.

Python 버전 확인
----------------
macOS 기본 `/usr/bin/python3`는 Command Line Tools에 포함된 3.9라서, 3.10 이상 문법을 쓰는
다른 모듈을 import하는 순간 SyntaxError가 나고 사용자는 원인을 알기 어렵다.
그래서 패키지를 불러올 때 가장 먼저 실행되는 이 파일에서 버전을 확인하고,
오래된 버전이면 설치 방법을 담은 오류를 출력한 뒤 종료 코드 3으로 끝낸다.

이 파일만은 오래된 Python에서도 해석될 수 있도록 3.10 이상 문법
(match 문, `X | None` 타입 표기, 괄호로 묶은 with 등)을 쓰지 않는다.
(tests/installer/test_launcher.py가 3.6 문법 기준으로 파싱해 확인한다)
"""
import sys

MIN_PYTHON = (3, 10)
EXIT_PYTHON_TOO_OLD = 3

if sys.version_info < MIN_PYTHON:
    import json

    _message = "Python {}.{} 이상이 필요합니다. 현재 {}.{} ({})".format(
        MIN_PYTHON[0], MIN_PYTHON[1], sys.version_info[0], sys.version_info[1], sys.executable)
    _hint = "brew install python 으로 설치한 뒤 installer/core/vigie-installer 로 실행하세요"
    if "--json" in sys.argv:
        # --json으로 실행한 쪽은 stdout의 JSON Lines만 읽으므로 같은 형식의 error 이벤트로 알린다
        print(json.dumps({"type": "error", "step": "python", "message": _message, "hint": _hint},
                         ensure_ascii=False))
    else:
        print("오류: " + _message + "\n  → " + _hint, file=sys.stderr)
    sys.exit(EXIT_PYTHON_TOO_OLD)
