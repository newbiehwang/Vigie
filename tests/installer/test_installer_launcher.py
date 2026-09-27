"""Python 선택 실행기(installer/core/vigie-installer)와 오래된 Python에 대한 보호"""
import ast
import json
import subprocess
import sys

from .helpers import CORE

LAUNCHER = CORE / "vigie-installer"


def launch(*args, env):
    # PYTHONPATH를 빼고 실행한다: 실행기가 스스로 vigie_installer 위치를 PYTHONPATH에 넣는지 확인하기 위해
    env = {k: v for k, v in env.items() if k != "PYTHONPATH"}
    return subprocess.run(["/bin/bash", str(LAUNCHER), *args], capture_output=True, text=True,
                          env=env, timeout=60)


def old_python(tmp_path):
    """버전 확인에 실패하는(3.10 미만처럼 행동하는) 가짜 인터프리터."""
    path = tmp_path / "python3.9"
    path.write_text("#!/bin/bash\nexit 1\n")
    path.chmod(0o755)
    return path


def test_launcher_syntax():
    assert subprocess.run(["bash", "-n", str(LAUNCHER)]).returncode == 0
    assert LAUNCHER.stat().st_mode & 0o111, "실행 권한이 있어야 한다"


def test_launcher_runs_cli_with_chosen_python(fake):
    result = launch("check", "--help", env=fake.env(VIGIE_PYTHON=sys.executable))
    assert result.returncode == 0 and "--dry-run" in result.stdout


def test_launcher_rejects_old_explicit_python_as_json(fake, tmp_path):
    result = launch("check", "--json", env=fake.env(VIGIE_PYTHON=str(old_python(tmp_path))))
    assert result.returncode == 3
    event = json.loads(result.stdout)
    assert event["type"] == "error" and event["step"] == "python" and "VIGIE_PYTHON" in event["message"]


def test_launcher_rejects_old_explicit_python_as_text(fake, tmp_path):
    result = launch("check", env=fake.env(VIGIE_PYTHON=str(old_python(tmp_path))))
    assert result.returncode == 3 and result.stdout == "" and "오류:" in result.stderr


def test_launcher_prefers_homebrew_path_order():
    # 찾는 순서가 문서(스크립트 주석)와 일치하는지: Homebrew(Apple Silicon) → Homebrew(Intel) → PATH → /usr/bin
    text = LAUNCHER.read_text()
    assert "candidates=(/opt/homebrew/bin/python3 /usr/local/bin/python3)" in text
    assert text.index('candidates+=("$path_python")') < text.index("candidates+=(/usr/bin/python3)")
    # /usr/bin/python3는 macOS에서 Command Line Tools가 있을 때만 시도한다 (설치 창 방지)
    assert "xcode-select -p" in text


def test_package_init_parses_on_old_python():
    # __init__.py는 3.9 이하에서도 해석되어야 버전 오류 안내를 보여 줄 수 있다
    ast.parse((CORE / "vigie_installer" / "__init__.py").read_text(), feature_version=(3, 6))


def test_package_init_guard_emits_json_error():
    # 실제 오래된 Python 없이 보호 코드의 동작을 확인하기 위해 최소 버전을 현재보다 높게 바꿔 실행한다
    source = (CORE / "vigie_installer" / "__init__.py").read_text().replace(
        "MIN_PYTHON = (3, 10)", "MIN_PYTHON = (99, 0)")
    result = subprocess.run([sys.executable, "-c", source, "--json"], capture_output=True, text=True)
    assert result.returncode == 3
    event = json.loads(result.stdout)
    assert event["type"] == "error" and event["message"].startswith("Python 99.0 이상이 필요합니다")
