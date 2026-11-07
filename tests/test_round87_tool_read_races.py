"""Round 87: file tools open first and check the open file, so a FIFO swapped in never blocks.

read_file, edit_file and diff_files checked is_file() and then opened the path. An agent's own
shell can replace the file with a FIFO between the two: open()/read_bytes() then parked a worker
thread forever. Every off_loop tool shares that pool, so a few such calls stalled the node.
The race is simulated by making is_file() say True for the FIFO.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS", "AVRONE_AGENT_DOCKER_ISOLATE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "d")
    r = LocalRunner(s)
    asyncio.run(r.start())
    core = SimpleNamespace(settings=s, runner=r, codex=Codex(s.data_dir))
    ctx = SimpleNamespace(core=core, run_id="r", name="A", depth=0, agent_id=None, agent_dir=None)
    reg = builtin_registry()
    register_extra_tools(reg)
    os.mkfifo(s.workspace / "f.txt")
    (s.workspace / "ok.txt").write_text("hello\n")
    real = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: True if self.name == "f.txt" else real(self))
    return SimpleNamespace(ws=s.workspace, ctx=ctx, reg=reg)


def _call(env, tool, **args):
    out: dict = {}

    def go():
        try:
            out["v"] = asyncio.run(env.reg.call(env.ctx, tool, args))
        except BaseException as e:  # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(4)
    if t.is_alive():
        with open(env.ws / "f.txt", "wb"):  # unblock the parked open so the thread can finish
            pass
        t.join(2)
        pytest.fail(f"{tool} blocked on a FIFO")
    assert "e" not in out, out
    return out["v"]


def test_read_file_fifo(env):
    assert "not a regular file" in _call(env, "read_file", path="f.txt")


def test_edit_file_fifo(env):
    assert "not a regular file" in _call(env, "edit_file", path="f.txt", old="a", new="b")


def test_diff_files_fifo(env):
    assert "not a regular file" in _call(env, "diff_files", path_a="ok.txt", path_b="f.txt")


def test_read_file_still_reads_through_symlink(env):
    os.symlink(env.ws / "ok.txt", env.ws / "link.txt")
    assert "hello" in _call(env, "read_file", path="link.txt")


def test_edit_file_size_from_open_file(env, monkeypatch):
    from avrone import tools_extra as tx
    monkeypatch.setattr(tx, "EDIT_MAX_BYTES", 3)
    out = _call(env, "edit_file", path="ok.txt", old="hello", new="bye")
    assert "edit_file handles up to 3 bytes" in out
    assert (env.ws / "ok.txt").read_text() == "hello\n"
