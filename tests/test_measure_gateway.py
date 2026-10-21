"""보안 게이트웨이 평가 스크립트 (scripts/measure_gateway.py): 무료 모드(패턴 탐지)가 요청 없이 돌고, 말뭉치가 겹치지 않는다."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("measure_gateway", ROOT / "scripts" / "measure_gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_corpora_have_unique_names():
    module = load()
    names = [name for name, _ in module.ATTACKS + module.DATA_ONLY + module.BENIGN]
    assert len(names) == len(set(names))


def test_pattern_baseline_runs_offline(capsys):
    """기준선: 패턴은 바꿔 쓴 지시문을 놓치고, 평범한 AWS 문서 문장은 잡지 않는다 (Jev와 비교할 값)."""
    module = load()
    module.sys.path.insert(0, str(ROOT / "services" / "llm"))
    baseline = module.patterns_report()
    assert "en_paraphrase" not in baseline["caught"] and "ko_pattern" in baseline["caught"]
    assert baseline["false"] == []
    assert "재현율" in capsys.readouterr().out
