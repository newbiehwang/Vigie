"""저장소 루트 .env 읽기: deploy.sh의 sync_anthropic_key와 같은 규칙으로 읽는다"""
import pytest

from vigie_installer import dotenv


@pytest.mark.parametrize("text,expected", [
    ("ANTHROPIC_API_KEY=sk-ant-1\n", "sk-ant-1"),
    ('ANTHROPIC_API_KEY="sk-ant-2"\n', "sk-ant-2"),            # 따옴표를 벗긴다
    ("ANTHROPIC_API_KEY='sk-ant-3'\n", "sk-ant-3"),
    ("  ANTHROPIC_API_KEY = sk-ant-4  \n", "sk-ant-4"),        # 앞뒤 공백을 지운다
    ("ANTHROPIC_API_KEY=a\nANTHROPIC_API_KEY=b\n", "b"),       # 마지막 줄을 쓴다
    ("# ANTHROPIC_API_KEY=commented\n", None),                 # 주석은 읽지 않는다
    ("ANTHROPIC_API_KEY=\n", None),                            # 비어 있으면 없는 것
    ("OTHER=1\n", None),
])
def test_read_value(tmp_path, text, expected):
    (tmp_path / ".env").write_text(text)
    assert dotenv.read_value(tmp_path, "ANTHROPIC_API_KEY") == expected


def test_missing_file_or_repo(tmp_path):
    assert dotenv.read_value(tmp_path, "ANTHROPIC_API_KEY") is None
    assert dotenv.read_value(None, "ANTHROPIC_API_KEY") is None
