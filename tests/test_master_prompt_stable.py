"""Generated handoff must be independent of test-run cache artifacts."""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_master_prompt_tree_ignores_python_caches(tmp_path, monkeypatch):
    mod = runpy.run_path(str(ROOT / "tools" / "make_master_prompt.py"))
    fn = mod["tree"]
    monkeypatch.setitem(fn.__globals__, "ROOT", tmp_path)
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "deploy.sh").write_text("echo hi", encoding="utf-8")
    baseline = fn()
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "cached.pyc").write_bytes(b"test")
    (d / ".pytest_cache").mkdir()
    (d / "compiled.pyc").write_bytes(b"test")
    assert fn() == baseline
