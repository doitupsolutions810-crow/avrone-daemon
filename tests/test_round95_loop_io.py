"""Round 95: blocking file I/O moved off the event loop in log_health, pack_list, evolve_reset and
evolve proposal edits (AST sweep of async tools without @off_loop).

Before: log_health globbed + stat'ed every run log on the loop; pack_list iterdir'd, sorted and
stat'ed every pack on the loop; evolve_reset rmtree'd a whole repo clone on the loop; and a
proposal's edits (up to 200 files / 16 MB) were written on the loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from avrone import evolve, tools_limits, tools_packpeer
from tests.test_cov13_a import _ectx

SLOW = 0.4


def _ticks(coro_fn) -> int:
    async def main():
        ticks, stop = 0, False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.02)
        t = asyncio.create_task(ticker())
        await coro_fn()
        stop = True
        await t
        return ticks
    return asyncio.run(main())


def _slow(fn):
    def wrapped(*a, **k):
        time.sleep(SLOW)
        return fn(*a, **k)
    return wrapped


def test_log_health_listing_off_loop(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.jsonl").write_text('{"kind": "x"}\n')
    monkeypatch.setattr(os, "scandir", _slow(os.scandir))
    monkeypatch.setattr(Path, "glob", _slow(Path.glob))
    ctx = S(core=S(settings=S(data_dir=str(tmp_path))))
    out = {}

    async def go():
        out["r"] = await tools_limits.t_log_health(ctx, {})
    assert _ticks(go) >= 10
    assert "1 of 1 run log(s) checked" in out["r"]


def test_log_health_skips_symlinked_log(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    host = tmp_path / "host.txt"
    host.write_text("not json and torn")
    os.symlink(host, runs / "evil.jsonl")
    (runs / "ok.jsonl").write_text('{"kind": "x"}\n')
    ctx = S(core=S(settings=S(data_dir=str(tmp_path))))
    out = asyncio.run(tools_limits.t_log_health(ctx, {}))
    assert "evil" not in out and "1 of 1 run log(s) checked" in out


def test_pack_list_listing_off_loop(tmp_path, monkeypatch):
    from avrone import agentpack
    d = agentpack.pack_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "a-1.tar.gz").write_bytes(b"junk")
    monkeypatch.setattr(os, "scandir", _slow(os.scandir))
    monkeypatch.setattr(Path, "iterdir", _slow(Path.iterdir))
    ctx = S(core=S(settings=S(data_dir=tmp_path)))
    out = {}

    async def go():
        out["r"] = await tools_packpeer.t_pack_list(ctx, {})
    assert _ticks(go) >= 10
    assert "a-1.tar.gz (4 bytes): REFUSED" in out["r"]


def test_evolve_reset_rmtree_off_loop(tmp_path, monkeypatch):
    ctx = _ectx(tmp_path)
    d = evolve._data(ctx)
    d.mkdir(parents=True, exist_ok=True)
    pid = "1700000000-E-abc123"
    wt = d / f"wt-{pid}"
    wt.mkdir()
    (wt / "f.py").write_text("x")
    (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "worktree": str(wt)}))
    monkeypatch.setattr(shutil, "rmtree", _slow(shutil.rmtree))
    out = {}

    async def go():
        out["r"] = await evolve.t_evolve_reset(ctx, {"pid": pid})
    assert _ticks(go) >= 10
    assert "discarded" in out["r"] and not wt.exists()


def test_proposal_edits_off_loop(tmp_path, monkeypatch):
    ctx = _ectx(tmp_path)
    wt = tmp_path / "evolve" / "wt-x"
    (wt / "sub").mkdir(parents=True)

    async def fake_wt(c, pid):
        return wt, "evolve/x"

    async def fake_sh(*a, **k):
        return S(exit_code=1, stdout="", stderr="")
    monkeypatch.setattr(evolve, "_ensure_worktree", fake_wt)
    monkeypatch.setattr(evolve, "_sh", fake_sh)
    monkeypatch.setattr(evolve, "_in_repo", _slow(evolve._in_repo))
    out = {}

    async def go():
        out["r"] = await evolve._propose_once(ctx, "g", [{"path": "a.py", "content": "x = 1\n"},
                                                         {"path": "sub", "content": "y"}])
    assert _ticks(go) >= 20
    r = out["r"]
    assert r["passed"] is False and "directory" in r["error"] and r["files"] == ["a.py"]
    assert not wt.exists()  # the failed proposal's clone is removed
