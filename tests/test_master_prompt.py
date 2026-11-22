"""MASTER_PROMPT.md is generated from the code and must stay true: every tool, skill and env it names
exists, and the committed file matches a fresh generation."""
import re
import runpy
import subprocess
import sys
from pathlib import Path

from avrone import __version__
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
MOD = runpy.run_path(str(ROOT / "tools" / "make_master_prompt.py"))


def _clean_build() -> str:
    code = "import runpy, sys; sys.stdout.write(runpy.run_path(sys.argv[1])['build']())"
    r = subprocess.run([sys.executable, "-c", code, str(ROOT / "tools" / "make_master_prompt.py")],
                       capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        raise AssertionError("clean build failed: " + (r.stderr or "").strip()[:400])
    return r.stdout


def test_committed_prompt_is_current():
    text = (ROOT / "MASTER_PROMPT.md").read_text(encoding="utf-8")
    if text == MOD["build"]():  # fast path: pure, in-process
        return
    # a long pytest run can leave import/env side effects behind (fake modules stuffed into
    # sys.modules, patched registries the generator consults); judge the committed file in a
    # clean interpreter before calling it stale.
    clean = _clean_build()
    if text == clean:
        return
    first = next((i for i, (a, b) in enumerate(zip(text, clean)) if a != b), min(len(text), len(clean)))
    raise AssertionError("run: python tools/make_master_prompt.py (clean build differs at char "
                         + str(first) + ": committed " + repr(text[first:first + 60])
                         + " vs clean " + repr(clean[first:first + 60]) + ")")


def test_prompt_names_only_real_things():
    text = MOD["build"]()
    src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z0-9_]+", text)):
        assert env in src, env
    reg = builtin_registry()
    register_extra_tools(reg)
    names = {t["name"] for t in reg.list()}
    listed = set(re.findall(r"^\| `([a-z_0-9]+)` \|", text, re.M))
    assert listed == names
    for skill in re.findall(r"^- \*\*([a-z0-9-]+)\*\*:", text, re.M):
        assert (ROOT / "skills" / skill / "SKILL.md").exists(), skill
    assert f"v{__version__}" in text and "Hard safety lines" in text
    for f in ("tools/model_bench.py", "avrone/server.py", "avrone/mcp_server.py"):
        assert (ROOT / f).exists()

def test_tree_omits_runtime_cache_directories(tmp_path, monkeypatch):
    for top in ("tools", "plugins", "scripts", "deploy", "examples"):
        directory = tmp_path / top
        directory.mkdir()
        (directory / "__pycache__").mkdir()
        (directory / "keep.txt").write_text("tracked structure", encoding="utf-8")

    monkeypatch.setitem(MOD["tree"].__globals__, "ROOT", tmp_path)
    rendered = MOD["tree"]()
    assert "__pycache__" not in rendered
    assert rendered.count("keep.txt") == 5
