"""저장소 루트의 .env에서 값 읽기 (.env.example 참고)

ANTHROPIC_API_KEY를 .env에 적어 두면 setup이 키를 묻지 않고 그 값을 쓰고, deploy는 SSM에 키가 없어도
멈추지 않는다 (deploy.sh가 스택을 배포하기 전에 .env의 키를 SSM으로 올린다: deploy.sh의 sync_anthropic_key).

읽는 규칙은 deploy.sh의 sync_anthropic_key와 같게 맞춘다. 두 쪽이 다르게 읽으면 setup은 "있다"고 했는데
deploy.sh는 "없다"며 건너뛰는 일이 생긴다.
- KEY=VALUE 줄에서 앞뒤 공백을 지우고, 값을 감싼 따옴표(" 또는 ')를 벗긴다
- 같은 키가 여러 번 나오면 마지막 줄을 쓴다
- 값이 비어 있으면 없는 것으로 본다
.env는 셸로 실행(source)하지 않고 글자로만 읽는다.
"""
from pathlib import Path

ENV_FILE = ".env"


def read_value(repo_root: Path | None, key: str) -> str | None:
    """저장소 루트 .env의 key 값. 저장소를 모르거나 파일·값이 없으면 None."""
    if repo_root is None:
        return None
    try:
        lines = (repo_root / ENV_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    value = ""
    for line in lines:
        name, sep, rest = line.strip().partition("=")
        if sep and name.strip() == key:
            value = rest.strip().strip('"').strip("'")
    return value or None
