"""Round 109: atomic_write_bytes never copies a symlink target's mode or keeps setuid/setgid;
python tool refuses a symlinked .avrone/tmp.

v0.101: atomic_write_bytes took the mode from p.stat(), which follows a symlink, so a link swapped
in after resolve copied a host file's 04755 onto agent content; a plain rewrite of a setuid file
kept S_ISUID (the kernel drops it on write()). t_python wrote and ran its snippet through a
symlinked .avrone/tmp, outside the workspace.
"""
from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace

import pytest

from avrone import tools, tools_extra


def test_rewrite_drops_setuid_setgid(tmp_path):
    p = tmp_path / "s.sh"
    p.write_text("old")
    os.chmod(p, 0o6755)
    tools.atomic_write_bytes(p, b"new")
    m = stat.S_IMODE(os.lstat(p).st_mode)
    assert not m & (stat.S_ISUID | stat.S_ISGID), oct(m)
    assert m & 0o755 == 0o755  # exec bits still kept
    assert p.read_bytes() == b"new"


def test_symlink_target_mode_not_copied(tmp_path):
    host = tmp_path / "host-bin"
    host.write_text("x")
    os.chmod(host, 0o4711)
    p = tmp_path / "out.txt"
    p.symlink_to(host)
    tools.atomic_write_bytes(p, b"agent")
    st = os.lstat(p)
    assert stat.S_ISREG(st.st_mode)  # the link itself was replaced
    assert not stat.S_IMODE(st.st_mode) & stat.S_ISUID
    assert stat.S_IMODE(st.st_mode) != 0o4711 & 0o7777
    assert host.read_text() == "x"


def test_plain_mode_kept(tmp_path):
    p = tmp_path / "run.sh"
    p.write_text("a")
    os.chmod(p, 0o750)
    tools.atomic_write_bytes(p, b"b")
    assert stat.S_IMODE(os.lstat(p).st_mode) == 0o750


class _Runner:
    def __init__(self, ws):
        self.workspace = ws
        self.cmds = []

    async def run(self, cmd, **kw):
        self.cmds.append(cmd)
        return SimpleNamespace(text=lambda: "ran")


def _ctx(ws):
    core = SimpleNamespace(runner=_Runner(ws), settings=SimpleNamespace(agents_can_shell=True))
    return SimpleNamespace(core=core, name="T", run_id="r", depth=0, agent_id=None)


def test_python_refuses_symlinked_tmp(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / ".avrone").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / ".avrone" / "tmp").symlink_to(outside)
    monkeypatch.setattr(tools_extra.fsunify, "agent_root", lambda ctx: None)
    ctx = _ctx(ws)
    out = asyncio.run(tools_extra.t_python(ctx, {"code": "print(1)"}))
    assert out.startswith("ERROR") and "symlink" in out
    assert list(outside.iterdir()) == []
    assert ctx.core.runner.cmds == []


def test_python_refuses_symlinked_avrone(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    (outside / "tmp").mkdir(parents=True)
    (ws / ".avrone").symlink_to(outside)
    monkeypatch.setattr(tools_extra.fsunify, "agent_root", lambda ctx: None)
    ctx = _ctx(ws)
    out = asyncio.run(tools_extra.t_python(ctx, {"code": "print(1)"}))
    assert out.startswith("ERROR")
    assert list((outside / "tmp").iterdir()) == []


def test_python_normal_runs_and_cleans(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(tools_extra.fsunify, "agent_root", lambda ctx: None)
    ctx = _ctx(ws)
    out = asyncio.run(tools_extra.t_python(ctx, {"code": "print(1)"}))
    assert out == "ran"
    assert ctx.core.runner.cmds and ".avrone/tmp/snippet_" in ctx.core.runner.cmds[0]
    assert list((ws / ".avrone" / "tmp").iterdir()) == []
