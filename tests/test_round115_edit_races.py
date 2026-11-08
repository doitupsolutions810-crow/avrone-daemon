"""Round 115: parallel edit_file calls on one file all land; a writer racing the edit is noticed.

v0.107: squad agents editing different lines of one file in parallel each got "edited ... (1
replacement)", but each rewrote the file from its own stale read: 40 parallel edits left 9-13
applied (/workspace/work/editrace.py). A write_file / shell write between edit_file's read and
its write was silently overwritten too.
"""
from __future__ import annotations

import asyncio

from avrone import tools_extra
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import AgentContext, TrinityCore


class _LLM:
    async def chat(self, messages, **kw):
        return '{"final": "ok"}'


def _core(tmp_path):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"
    s.workspace.mkdir(parents=True, exist_ok=True)
    core = TrinityCore(s, _LLM(), LocalRunner(s), Codex(s.data_dir))
    tools_extra.register_extra_tools(core.tools)
    return core


def _ctx(core, i=0):
    return AgentContext(core=core, run_id="r115", name=f"A{i}", depth=0)


def test_parallel_edits_all_apply(tmp_path):
    core = _core(tmp_path)
    n = 40
    f = core.settings.workspace / "f.txt"
    f.write_text("".join(f"line{i}=old\n" for i in range(n)))

    async def go():
        return await asyncio.gather(*[core.tools.call(_ctx(core, i), "edit_file",
                                                      {"path": "f.txt", "old": f"line{i}=old", "new": f"line{i}=new"})
                                      for i in range(n)])
    outs = asyncio.run(go())
    assert all(o.startswith("edited") for o in outs), outs
    assert f.read_text().count("=new") == n, f"only {f.read_text().count('=new')} of {n} edits survived"


def test_writer_between_read_and_write_is_not_clobbered(tmp_path, monkeypatch):
    core = _core(tmp_path)
    f = core.settings.workspace / "g.txt"
    f.write_text("alpha\nbeta\n")
    real, fired = tools_extra._truthy, []

    def racing_truthy(v):  # runs after edit_file read the file, before it writes
        if not fired:
            fired.append(1)
            f.write_text("alpha\nbeta\ngamma-from-other-writer\n")
        return real(v)
    monkeypatch.setattr(tools_extra, "_truthy", racing_truthy)
    out = asyncio.run(core.tools.call(_ctx(core), "edit_file", {"path": "g.txt", "old": "alpha", "new": "ALPHA"}))
    assert out.startswith("edited"), out
    assert f.read_text() == "ALPHA\nbeta\ngamma-from-other-writer\n", f.read_text()


def test_file_that_never_settles_is_not_written(tmp_path, monkeypatch):
    core = _core(tmp_path)
    f = core.settings.workspace / "h.txt"
    f.write_text("alpha\n")
    real, n = tools_extra._truthy, [0]

    def always_racing(v):
        n[0] += 1
        f.write_text("alpha\n" + "x" * n[0] + "\n")
        return real(v)
    monkeypatch.setattr(tools_extra, "_truthy", always_racing)
    out = asyncio.run(core.tools.call(_ctx(core), "edit_file", {"path": "h.txt", "old": "alpha", "new": "ALPHA"}))
    assert out.startswith("ERROR") and "kept changing" in out, out
    assert "ALPHA" not in f.read_text()


def test_single_edit_unchanged(tmp_path):
    core = _core(tmp_path)
    f = core.settings.workspace / "k.txt"
    f.write_text("one two\n")
    out = asyncio.run(core.tools.call(_ctx(core), "edit_file", {"path": "k.txt", "old": "two", "new": "2"}))
    assert out == "edited k.txt (1 replacement)" and f.read_text() == "one 2\n", out
