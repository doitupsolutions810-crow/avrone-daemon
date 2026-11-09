"""Round 125: edit_file serializes with editors in other processes.

v0.117 (probe /workspace/work/editxproc.py): the per-path edit lock lived in one process. The
daemon and the MCP server (or two workers) sharing a workspace each read the file, changed their
own line and replaced it: 4 processes x 25 edits of distinct lines all answered "edited", but only
92-96 of the 100 changes survived. The edit now holds an flock on the file's directory.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from avrone import tools_extra
from avrone.trinity import AgentContext
from tests.test_round121_readonly_storage import _core

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import asyncio, sys
from pathlib import Path
from avrone.config import Settings
from avrone.codex import Codex
from avrone.llm import LLM
from avrone.sovereign import LocalRunner
from avrone.trinity import AgentContext, TrinityCore
from avrone.tools_extra import register_extra_tools
ws, data, me, n = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
s = Settings(); s.workspace = Path(ws); s.data_dir = Path(data); s.llm_mode = "mock"
core = TrinityCore(s, LLM(s), LocalRunner(s), Codex(s.data_dir)); register_extra_tools(core.tools)
async def main():
    while not Path(ws, "go").exists():
        await asyncio.sleep(0.001)
    cs = [AgentContext(core=core, run_id="r", name=f"P{me}A{i}", depth=0) for i in range(n)]
    outs = await asyncio.gather(*[core.tools.call(cs[i], "edit_file",
        {"path": "f.txt", "old": f"p{me}l{i}=old", "new": f"p{me}l{i}=new"}) for i in range(n)])
    print(sum(o.startswith("edited") for o in outs))
asyncio.run(main())
'''


def test_parallel_processes_keep_every_edit(tmp_path):
    P, N = 4, 20
    ws, data = tmp_path / "ws", tmp_path / "d"
    ws.mkdir()
    f = ws / "f.txt"
    f.write_text("".join(f"p{p}l{i}=old\n" for p in range(P) for i in range(N)))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, "-c", PROBE, str(ws), str(data), str(p), str(N)],
                              stdout=subprocess.PIPE, text=True, env=env, cwd=str(ROOT)) for p in range(P)]
    time.sleep(2.5)  # all imported and waiting
    (ws / "go").touch()
    reported = sum(int((p.communicate(timeout=60)[0].strip() or "0").splitlines()[-1]) for p in procs)
    assert reported == P * N
    assert f.read_text().count("=new") == P * N  # every reported edit survived


def test_edit_waits_for_another_holder(tmp_path):
    core = _core(tmp_path)
    ws = core.settings.workspace
    (ws / "f.txt").write_text("a=1\n")
    fd = os.open(ws, os.O_RDONLY)  # its own open file description, like another process
    fcntl.flock(fd, fcntl.LOCK_EX)
    out: list[str] = []
    ctx = AgentContext(core=core, run_id="r", name="E-1", depth=0)
    t = threading.Thread(target=lambda: out.append(asyncio.run(
        core.tools.call(ctx, "edit_file", {"path": "f.txt", "old": "a=1", "new": "a=2"}))))
    t.start()
    time.sleep(0.4)
    try:
        assert not out and (ws / "f.txt").read_text() == "a=1\n"  # held off while locked
    finally:
        os.close(fd)
    t.join(10)
    assert out and out[0].startswith("edited"), out
    assert (ws / "f.txt").read_text() == "a=2\n"


def test_lock_released_after_edit(tmp_path):
    core = _core(tmp_path)
    ws = core.settings.workspace
    (ws / "f.txt").write_text("a=1\n")
    ctx = AgentContext(core=core, run_id="r", name="E-1", depth=0)
    for old, new in (("a=1", "a=2"), ("a=2", "a=3"), ("zzz", "y")):
        asyncio.run(core.tools.call(ctx, "edit_file", {"path": "f.txt", "old": old, "new": new}))
    fd = os.open(ws, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # nobody still holds it, error path included
    finally:
        os.close(fd)


def test_unlockable_dir_still_edits(tmp_path, monkeypatch):
    core = _core(tmp_path)
    (core.settings.workspace / "f.txt").write_text("a=1\n")
    from avrone import safeio

    def never(fd, timeout_s=None):
        return False
    monkeypatch.setattr(safeio, "flock_wait", never)
    ctx = AgentContext(core=core, run_id="r", name="E-1", depth=0)
    out = asyncio.run(core.tools.call(ctx, "edit_file", {"path": "f.txt", "old": "a=1", "new": "a=2"}))
    assert out.startswith("edited") and (core.settings.workspace / "f.txt").read_text() == "a=2\n"
    assert getattr(tools_extra, "_dir_flock", None) or pytest.fail("no _dir_flock")
