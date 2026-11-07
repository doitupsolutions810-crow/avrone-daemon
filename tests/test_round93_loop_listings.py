"""Round 93: evolve loop listings are bounded and evolve_status runs off the event loop.

Before: list_loops opened every loop file twice per call (reconcile, then list), evolve_list
printed one line per loop ever run, and t_evolve_status did all of that on the event loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from avrone import evolve
from tests.test_cov13_a import _ectx


def _loop(d, i, ts, **kw):
    p = d / f"L{i:05d}.json"
    p.write_text(json.dumps({"loop_id": f"L{i:05d}", "status": "done", "goal": f"goal{i}", "ts": ts,
                             "iterations": [], "max_iters": 3, **kw}))
    os.utime(p, (ts, ts))
    return p


def _dir(ctx):
    d = evolve._settings_loops_dir(ctx.core.settings)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_evolve_list_caps_loop_lines(tmp_path):
    ctx = _ectx(tmp_path)
    d = _dir(ctx)
    now = time.time()
    for i in range(80):
        _loop(d, i, now - 80 + i)
    out = asyncio.run(evolve.t_evolve_list(ctx, {}))
    assert "L00079" in out and "L00000" not in out
    assert "newest 50 of 80 shown" in out


def test_list_loops_reads_bounded_set(tmp_path, monkeypatch):
    ctx = _ectx(tmp_path)
    d = _dir(ctx)
    now = time.time()
    for i in range(40):
        _loop(d, i, now - 40 + i)
    monkeypatch.setattr(evolve, "LOOPS_LIST_MAX", 10, raising=False)
    evolve.list_loops(ctx.core.settings)  # first call per dir may sweep everything
    calls = []
    real = evolve._load_json
    monkeypatch.setattr(evolve, "_load_json", lambda p: (calls.append(p), real(p))[1])
    rows = evolve.list_loops(ctx.core.settings)
    assert len(calls) <= 20 and [r["loop_id"] for r in rows] == [f"L{i:05d}" for i in range(30, 40)]


def test_status_by_id_reads_one_file(tmp_path, monkeypatch):
    ctx = _ectx(tmp_path)
    d = _dir(ctx)
    now = time.time()
    for i in range(30):
        _loop(d, i, now - 30 + i)
    evolve.list_loops(ctx.core.settings)
    calls = []
    real = evolve._load_json
    monkeypatch.setattr(evolve, "_load_json", lambda p: (calls.append(p), real(p))[1])
    out = asyncio.run(evolve.t_evolve_status(ctx, {"loop_id": "L00003"}))
    assert "L00003" in out and len(calls) <= 2
    assert "no loop" in asyncio.run(evolve.t_evolve_status(ctx, {"loop_id": "nope"}))


def test_status_does_not_block_event_loop(tmp_path, monkeypatch):
    ctx = _ectx(tmp_path)
    d = _dir(ctx)
    _loop(d, 1, time.time())
    real = evolve._load_json
    monkeypatch.setattr(evolve, "_load_json", lambda p: (time.sleep(0.4), real(p))[1])

    async def main():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.02)
        t = asyncio.create_task(ticker())
        await evolve.t_evolve_status(ctx, {})
        stop = True
        await t
        return ticks
    assert asyncio.run(main()) >= 10


def test_old_orphan_still_marked_by_first_sweep(tmp_path, monkeypatch):
    ctx = _ectx(tmp_path)
    d = _dir(ctx)
    now = time.time()
    _loop(d, 0, now - 1000, status="running", owner_pid=2_000_000_000)
    for i in range(1, 30):
        _loop(d, i, now - 30 + i)
    monkeypatch.setattr(evolve, "LOOPS_LIST_MAX", 5, raising=False)
    monkeypatch.setattr(evolve, "_pid_alive", lambda pid: False)
    evolve.list_loops(ctx.core.settings)
    assert json.loads((d / "L00000.json").read_text())["status"] == "interrupted"
