"""Round 92: evolve_list, list_accepted and the checkpoint listing read a bounded, newest set.

Before: t_evolve_list opened and parsed every proposal file and printed them all oldest first
(5,000 proposals: 6.5 s and 440 KB), so the output clip cut the newest proposals. list_accepted
and checkpoint.list_managed also read every file in their directory on each call.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from avrone import checkpoint, evolve
from tests.test_cov13_a import _ectx


def _prop(d, i, ts, passed=True):
    p = d / f"{1_700_000_000 + i}-E-{i:06x}.json"
    p.write_text(json.dumps({"pid": p.stem, "passed": passed, "goal": f"goal{i}", "files": ["a.py"],
                             "by": "E", "ts": ts}))
    os.utime(p, (ts, ts))
    return p


def test_evolve_list_shows_newest_and_counts_the_rest(tmp_path):
    ctx = _ectx(tmp_path)
    d = evolve._data(ctx)
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    n = getattr(evolve, "EVOLVE_LIST_MAX", 200) + 50
    for i in range(n):
        _prop(d, i, now - n + i)
    out = asyncio.run(evolve.t_evolve_list(ctx, {}))
    assert f"goal{n - 1} " in out  # the newest is shown
    assert "goal0 " not in out  # the oldest are left out...
    assert "50 older proposal file(s) not shown" in out  # ...and counted
    assert out.count("[PASS]") == 200


def test_evolve_list_skips_symlink_and_fifo(tmp_path):
    ctx = _ectx(tmp_path)
    d = evolve._data(ctx)
    d.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"pid": "LEAK", "passed": True, "goal": "leak", "files": [], "by": "x"}))
    os.symlink(outside, d / "link.json")
    os.mkfifo(d / "fifo.json")
    _prop(d, 1, time.time())
    out = asyncio.run(evolve.t_evolve_list(ctx, {}))
    assert "LEAK" not in out and "goal1" in out


def test_list_accepted_bounded(tmp_path, monkeypatch):
    if not hasattr(evolve, "ACCEPTED_SCAN_MAX"):
        pytest.fail("evolve.ACCEPTED_SCAN_MAX missing")
    ctx = _ectx(tmp_path)
    d = evolve._data(ctx)
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for i in range(30):
        _prop(d, i, now - 30 + i)
    monkeypatch.setattr(evolve, "ACCEPTED_SCAN_MAX", 10)
    rows = evolve.list_accepted(ctx.core.settings)
    assert len(rows) == 10 and {r["goal"] for r in rows} == {f"goal{i}" for i in range(20, 30)}


def test_list_managed_bounded(tmp_path, monkeypatch):
    if not hasattr(checkpoint, "LIST_MANAGED_MAX"):
        pytest.fail("checkpoint.LIST_MANAGED_MAX missing")
    d = checkpoint.managed_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    real = checkpoint.describe
    seen = []
    monkeypatch.setattr(checkpoint, "describe", lambda p: (seen.append(p.name), real(p))[1])
    for i in range(20):
        (d / f"k{i:02d}.json").write_text("{}")
    monkeypatch.setattr(checkpoint, "LIST_MANAGED_MAX", 5)
    checkpoint.list_managed(tmp_path)
    assert len(seen) == 5


def test_newest_files_helper(tmp_path):
    from avrone import safeio
    if not hasattr(safeio, "newest_files"):
        pytest.fail("safeio.newest_files missing")
    now = time.time()
    for i in range(10):
        p = tmp_path / f"f{i}.json"
        p.write_text("{}")
        os.utime(p, (now + i, now + i))
    (tmp_path / ".hidden.json").write_text("{}")
    got, total = safeio.newest_files(tmp_path, ".json", 3)
    assert [p.name for p in got] == ["f7.json", "f8.json", "f9.json"] and total == 10
    assert safeio.newest_files(tmp_path / "nope", ".json", 3) == ([], 0)
