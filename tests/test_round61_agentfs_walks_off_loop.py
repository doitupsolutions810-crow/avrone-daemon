"""Round 61: agent-dir file tools ran their directory walks on the event loop.

A wider AST sweep found raw blocking I/O in async tools outside to_thread. Measured on a
100k-file agent dir (a pip or npm install into an agent's own dir gets there quickly):

- quota_write (fs_write, plus write_file and edit_file inside an agent dir) walks the WHOLE agent
  dir with lstat on every write to check the quota: ~0.26 s per write on the loop, while holding a
  threading.Lock (a second holder in a thread would have blocked the loop outright).
- fs_list sorts rglob of the whole tree, then walks it again for usage: ~0.6 s+.
- fs_delete rmtree + dir_usage of the subtree; fs_read opens and stats.
- list_files sorts every directory level before capping at 400 entries.
- evolve_list reads every proposal file (3000: ~0.15 s).

All now run in a worker thread (off_loop, or to_thread around quota_write). Behavior and
messages are unchanged.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from avrone import agentfs, agentfs_tools
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def _probe():
    try:
        asyncio.get_running_loop()
        return "on-loop"
    except RuntimeError:
        return "thread"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_FS_MAX_FILE_MB", "AVRONE_AGENT_FS",
              "AVRONE_AGENT_DOCKER_ISOLATE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    runner = LocalRunner(s)
    asyncio.run(runner.start())
    codex = Codex(s.data_dir)
    core = SimpleNamespace(settings=s, runner=runner, codex=codex)
    d = agentfs.agent_dir(s.workspace, "alpha")
    ctx = SimpleNamespace(core=core, run_id="run1", name="Alpha-1", depth=0, agent_id="alpha", agent_dir=d)
    reg = builtin_registry()
    register_extra_tools(reg)
    where: list[str] = []
    orig = agentfs_tools.dir_usage
    monkeypatch.setattr(agentfs_tools, "dir_usage", lambda p: (where.append(_probe()), orig(p))[1])
    return SimpleNamespace(s=s, ctx=ctx, reg=reg, dir=d, where=where, tmp=tmp_path)


def call(env, tool, **args):
    return asyncio.run(env.reg.call(env.ctx, tool, args))


@pytest.mark.parametrize("tool", ["write_file", "fs_write"])
def test_quota_walk_off_loop_on_write(env, tool):
    out = call(env, tool, path="notes/a.txt", content="hello")
    assert out.startswith("wrote notes/a.txt (5 bytes;")
    assert env.where == ["thread"]


def test_quota_walk_off_loop_on_edit(env):
    call(env, "write_file", path="e.txt", content="alpha beta")
    env.where.clear()
    out = call(env, "edit_file", path="e.txt", old="beta", new="gamma")
    assert out.startswith("edited") and (env.dir / "e.txt").read_text() == "alpha gamma"
    assert env.where == ["thread"]


def test_fs_list_and_delete_off_loop(env):
    for i in range(5):
        call(env, "fs_write", path=f"sub/f{i}.txt", content="x" * i)
    env.where.clear()
    listing = call(env, "fs_list", recursive=True)
    assert "sub/f4.txt  4 bytes" in listing
    out = call(env, "fs_delete", path="sub", recursive=True)
    assert out.startswith("deleted sub (10 bytes freed)")
    assert env.where == ["thread", "thread"]


def test_fs_read_off_loop(env, monkeypatch):
    call(env, "fs_write", path="r.txt", content="read me")
    seen = []
    orig = agentfs_tools._resolve
    monkeypatch.setattr(agentfs_tools, "_resolve", lambda *a: (seen.append(_probe()), orig(*a))[1])
    assert call(env, "fs_read", path="r.txt") == "read me"
    assert seen == ["thread"]


def test_quota_still_enforced_from_thread(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    call(env, "fs_write", path="big.bin", content="x" * 900_000)
    out = call(env, "write_file", path="big2.bin", content="y" * 300_000)
    assert "quota" in out and not (env.dir / "big2.bin").exists()


def test_concurrent_writes_respect_quota(env, monkeypatch):
    """Writes now race in threads; the per-dir lock still serialises check + write."""
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")

    async def go():
        tasks = [env.reg.call(env.ctx, "fs_write", {"path": f"c{i}.bin", "content": "z" * 300_000})
                 for i in range(6)]
        return await asyncio.gather(*tasks, return_exceptions=True)
    res = asyncio.run(go())
    ok = [r for r in res if isinstance(r, str) and r.startswith("wrote")]
    assert len(ok) == 3, res
    total = sum(p.stat().st_size for p in env.dir.rglob("*") if p.is_file())
    assert total <= 1024 * 1024


def test_list_files_off_loop(env, monkeypatch):
    from avrone import tools_extra
    (env.s.workspace / "pkg").mkdir(parents=True, exist_ok=True)
    (env.s.workspace / "pkg" / "m.py").write_text("x")
    seen = []
    orig = tools_extra.fsunify.display
    monkeypatch.setattr(tools_extra.fsunify, "display", lambda *a: (seen.append(_probe()), orig(*a))[1])
    legacy = SimpleNamespace(core=env.ctx.core, run_id="run1", name="L", depth=0, agent_id=None, agent_dir=None)
    out = asyncio.run(env.reg.call(legacy, "list_files", {"path": "."}))
    assert "pkg/" in out
    assert seen and set(seen) == {"thread"}


def test_evolve_list_off_loop(tmp_path, monkeypatch):
    from avrone import evolve
    s = SimpleNamespace(data_dir=tmp_path)
    ctx = SimpleNamespace(core=SimpleNamespace(settings=s))
    d = evolve._data(ctx)
    d.mkdir(parents=True, exist_ok=True)
    (d / "p1.json").write_text(json.dumps({"pid": "p1", "passed": True, "goal": "g", "files": ["a.py"], "by": "x"}))
    seen = []
    monkeypatch.setattr(evolve, "list_loops", lambda st: (seen.append(_probe()), [])[1])
    out = asyncio.run(evolve.t_evolve_list(ctx, {}))
    assert out.startswith("p1 [PASS] g (a.py) by x")
    assert seen == ["thread"]


def test_loop_stays_responsive_writing_into_a_big_agent_dir(env):
    big = env.dir / "node_modules"
    for i in range(60):
        sub = big / f"p{i}"
        sub.mkdir(parents=True)
        for j in range(500):
            (sub / f"f{j}").touch()

    async def go():
        gaps, last, stop = [], time.monotonic(), False

        async def tick():
            nonlocal last
            while not stop:
                await asyncio.sleep(0.005)
                n = time.monotonic(); gaps.append(n - last); last = n
        t = asyncio.create_task(tick())
        await asyncio.sleep(0.02)
        for i in range(3):
            await env.reg.call(env.ctx, "write_file", {"path": f"w{i}.txt", "content": "hi"})
        stop = True
        await asyncio.sleep(0.01)
        t.cancel()
        return max(gaps)
    gap = asyncio.run(go())
    assert gap < 0.08, gap
