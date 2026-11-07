"""Round 89: list_files and read_file's directory listing read a bounded number of entries.

Before: iterdir() + sorted() with an is_dir() stat per entry listed (and stat'ed) every entry of a
huge directory, at every level, before keeping 400; read_file on a directory did the same for its
30-name hint. list_files also printed the size of a symlink's target, from anywhere on the host.
"""
from __future__ import annotations

import asyncio
import os
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
    return SimpleNamespace(ws=s.workspace, ctx=ctx, reg=reg, tmp=tmp_path)


def call(env, tool, **a):
    return asyncio.run(env.reg.call(env.ctx, tool, a))


def _big(env, n=3000):
    d = env.ws / "big"
    d.mkdir()
    for i in range(n):
        (d / f"f{i:05d}").touch()
    return d


def test_list_files_scan_is_bounded(env, monkeypatch):
    from avrone import tools_extra as tx
    if not hasattr(tx, "LIST_DIR_SCAN_MAX"):
        pytest.fail("no scan cap")
    monkeypatch.setattr(tx, "LIST_DIR_SCAN_MAX", 500)
    _big(env)
    calls = []
    real = os.stat
    monkeypatch.setattr(os, "stat", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    out = call(env, "list_files", path="big", depth=1)
    assert "truncated" in out
    assert len(calls) < 1000  # was one stat per entry for the sort key, then one per listed file


def test_list_files_total_budget(env, monkeypatch):
    from avrone import tools_extra as tx
    if not hasattr(tx, "LIST_TOTAL_SCAN_MAX"):
        pytest.fail("no total cap")
    monkeypatch.setattr(tx, "LIST_TOTAL_SCAN_MAX", 50)
    for k in range(5):
        d = env.ws / "t" / f"d{k}"
        d.mkdir(parents=True)
        for i in range(30):
            (d / f"x{i}").touch()
    out = call(env, "list_files", path="t", depth=3)
    assert "truncated" in out


def test_list_files_never_sizes_link_target(env):
    outside = env.tmp / "secret.bin"
    outside.write_bytes(b"x" * 12345)
    os.symlink(outside, env.ws / "l")
    out = call(env, "list_files", path=".", depth=1)
    assert "12345" not in out and "l (symlink)" in out


def test_read_file_dir_hint_bounded(env, monkeypatch):
    from avrone import tools
    if not hasattr(tools, "DIR_LISTING_SCAN_MAX"):
        pytest.fail("no dir listing cap")
    monkeypatch.setattr(tools, "DIR_LISTING_SCAN_MAX", 100)
    _big(env, 500)
    out = call(env, "read_file", path="big")
    assert "is a directory" in out and "over 100 entries" in out


def test_list_files_still_lists_dirs_first(env):
    (env.ws / "b").mkdir()
    (env.ws / "a.txt").write_text("hi")
    out = call(env, "list_files", path=".", depth=1)
    assert out.index("b/") < out.index("a.txt (2b)")
