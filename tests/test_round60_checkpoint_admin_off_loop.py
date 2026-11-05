"""Round 60: checkpoint saves and priv_admin CodeX views ran on the event loop.

An AST sweep listed every sync store call reachable from an async function outside to_thread.
Measured and fixed:

- Checkpoint.put re-read, re-parsed and rewrote the whole file with an fsync, on the loop, for
  every finished worker, the plan, every deep round and the final result. At 18 MB one put took
  ~81 ms (the cap is 64 MB). Trinity now calls it through asyncio.to_thread (worker steps,
  subtasks, deep rounds, results), and _Store skips the re-read when the file still has the
  (inode, size, mtime_ns) of its own last write (~48 ms, all of it off the loop). Another
  process's write changes the signature, so cross-process merging still happens.
- priv_admin codex_runs stats and reads every run log, and codex_tail streams the whole log to
  keep its last N events; both ran on the loop (the /api twins already used to_thread).

Probed and left: _ck_open/resume load happens once per run; ck.get is a dict read.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import checkpoint as ckmod
from avrone.checkpoint import Checkpoint


def _probe():
    try:
        asyncio.get_running_loop()
        return "on-loop"
    except RuntimeError:
        return "thread"


# ---------------------------------------------------------------- checkpoint store
def test_put_skips_reread_when_file_unchanged(tmp_path, monkeypatch):
    ck = Checkpoint(tmp_path / "c.json")
    ck.put("a", "1")
    reads = []
    orig = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (reads.append(self.name), orig(self))[1])
    for i in range(5):
        ck.put(f"k{i}", "v")
    assert reads == []
    assert json.loads((tmp_path / "c.json").read_text())["steps"]["k4"] == "v"


def test_put_still_merges_another_writer(tmp_path):
    p = tmp_path / "c.json"
    a = Checkpoint(ckmod._Store(p))
    a.put("mine", "1")
    b = Checkpoint(ckmod._Store(p))  # a second process, in effect
    b.put("theirs", "2")
    a.put("mine2", "3")
    steps = json.loads(p.read_text())["steps"]
    assert steps["theirs"] == "2" and steps["mine"] == "1" and steps["mine2"] == "3"


def test_put_merges_after_external_rewrite_same_size(tmp_path):
    p = tmp_path / "c.json"
    a = Checkpoint(ckmod._Store(p))
    a.put("x", "1")
    data = json.loads(p.read_text())
    data["steps"]["y"] = "Z"  # an edit by hand / another tool, then a rename into place
    tmp = tmp_path / "t.json"
    tmp.write_text(json.dumps(data))
    tmp.replace(p)
    a.put("w", "2")
    steps = json.loads(p.read_text())["steps"]
    assert steps["y"] == "Z" and steps["w"] == "2"


def test_sig_is_none_until_first_write(tmp_path):
    s = ckmod._Store(tmp_path / "c.json")
    assert s._sig is None
    Checkpoint(s).put("a", 1)
    assert s._sig is not None and s._sig[1] == (tmp_path / "c.json").stat().st_size


# ---------------------------------------------------------------- trinity call sites
def _core_env(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setenv("AVRONE_LLM_MODE", "mock")
    monkeypatch.setenv("AVRONE_LESSONS", "off")
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "0")
    (tmp_path / "ws").mkdir()
    (tmp_path / "d").mkdir()
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "d")
    monkeypatch.setattr(server.settings, "llm_mode", "mock")


async def _with_core(fn):
    from avrone import server
    async with server.lifespan(server.app):
        return await fn(server.core())


@pytest.mark.parametrize("mode", ["squad", "deep"])
def test_checkpoint_puts_run_in_a_thread(tmp_path, monkeypatch, mode):
    _core_env(tmp_path, monkeypatch)
    seen: list[tuple[str, str]] = []
    orig = Checkpoint.put

    def spy(self, key, value):
        seen.append((key, _probe()))
        return orig(self, key, value)
    monkeypatch.setattr(Checkpoint, "put", spy)
    p = tmp_path / "ck.json"

    async def go(c):
        if mode == "squad":
            return await c.squad("summarize caching", 3, "r60s", checkpoint=p)
        return await c.deep("plan a cache", None, 2, "r60d", checkpoint=p)
    out = asyncio.run(_with_core(go))
    assert isinstance(out, dict)
    keys = [k for k, _ in seen]
    assert any(k.endswith("result") for k in keys), keys
    if mode == "deep":
        assert any(k.endswith("rounds") for k in keys), keys
    on_loop = [k for k, where in seen if where == "on-loop" and not k.endswith("meta")]
    assert on_loop == [], on_loop


def test_checkpointed_squad_resumes_after_thread_saves(tmp_path, monkeypatch):
    _core_env(tmp_path, monkeypatch)
    p = tmp_path / "ck.json"

    async def go(c):
        a = await c.squad("resume me", 3, "r60r", checkpoint=p)
        b = await c.squad("resume me", 3, "r60r", checkpoint=p)
        return a, b
    a, b = asyncio.run(_with_core(go))
    assert a["answer"] == b["answer"]
    assert json.loads(p.read_text())["steps"]["result"]


# ---------------------------------------------------------------- priv_admin codex views
class _Codex:
    def __init__(self):
        self.where: list[str] = []

    def replay(self, run_id, limit=None):
        self.where.append(_probe())
        return [{"ts": 1, "kind": "k", "agent": "A", "data": {"i": i}} for i in range(3)]

    def runs(self, limit=20):
        self.where.append(_probe())
        return [{"run_id": "r1"}]


def test_admin_codex_tail_and_runs_off_loop():
    from avrone import tools_extra as tx
    cx = _Codex()
    ctx = SimpleNamespace(core=SimpleNamespace(codex=cx), run_id="r1")
    tail = asyncio.run(tx._admin_codex_tail(ctx, {"limit": 2}))
    runs = asyncio.run(tx._admin_codex_runs(ctx, {"limit": 5}))
    assert len(tail) == 3 and runs == [{"run_id": "r1"}]
    assert cx.where == ["thread", "thread"]


def test_admin_codex_tail_old_codex_without_limit():
    from avrone import tools_extra as tx

    class Old:
        def replay(self, run_id):
            return [{"ts": i, "kind": "k", "agent": "A", "data": None} for i in range(10)]
    ctx = SimpleNamespace(core=SimpleNamespace(codex=Old()), run_id="r1")
    out = asyncio.run(tx._admin_codex_tail(ctx, {"limit": 4}))
    assert [e["ts"] for e in out] == [6, 7, 8, 9]


def test_admin_codex_tail_still_refuses_bad_run_id():
    from avrone import tools_extra as tx
    ctx = SimpleNamespace(core=SimpleNamespace(codex=_Codex()), run_id="")
    with pytest.raises(ValueError):
        asyncio.run(tx._admin_codex_tail(ctx, {"run_id": "../etc"}))
