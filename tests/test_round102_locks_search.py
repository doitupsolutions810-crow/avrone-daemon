"""Round 102: lock files that never follow symlinks, and codex_search on the reused run scan.

v0.94: checkpoint's <file>.lock and fleet_nonces.json.lock were os.open(O_RDWR|O_CREAT) with no
O_NOFOLLOW, so a symlink planted there created its target anywhere the daemon could write; and
codex_search lstat'ed every run log on every call (100k logs: seconds per search).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from avrone import checkpoint, codex, fleet, safeio, tools_extra


def test_checkpoint_lock_symlink_creates_nothing(tmp_path):
    d = tmp_path / "ck"
    d.mkdir()
    outside = tmp_path / "outside" / "planted"
    outside.parent.mkdir()
    path = d / "run.json"
    os.symlink(outside, str(path) + ".lock")
    ck = checkpoint.Checkpoint(path)
    assert ck.put("k", {"v": 1}) is True  # saves (unlocked) instead of failing
    assert not outside.exists()
    assert json.loads(path.read_text())["steps"]


def test_checkpoint_lock_fifo_not_used(tmp_path):
    d = tmp_path / "ck"
    d.mkdir()
    path = d / "run.json"
    os.mkfifo(str(path) + ".lock")
    ck = checkpoint.Checkpoint(path)
    assert ck.put("k", 1) is True


def test_checkpoint_regular_lock_still_used(tmp_path):
    path = tmp_path / "run.json"
    assert checkpoint.Checkpoint(path).put("k", 1) is True
    assert Path(str(path) + ".lock").is_file()


def test_nonce_lock_symlink_creates_nothing(tmp_path):
    outside = tmp_path / "elsewhere"
    p = tmp_path / "fleet_nonces.json"
    os.symlink(outside, str(p) + ".lock")
    fake = SimpleNamespace(_nonce_path=p)
    with fleet.FleetManager._nonce_file_lock(fake):
        pass
    assert not outside.exists()


def test_nonce_lock_regular(tmp_path):
    p = tmp_path / "fleet_nonces.json"
    with fleet.FleetManager._nonce_file_lock(SimpleNamespace(_nonce_path=p)):
        assert Path(str(p) + ".lock").is_file()


def _cx(tmp_path, monkeypatch, n=6):
    monkeypatch.setenv("AVRONE_RUNS_CACHE_S", "30")
    c = codex.Codex(tmp_path)
    for i in range(n):
        f = c.log_dir / f"r{i}.jsonl"
        f.write_text(json.dumps({"run_id": f"r{i}", "kind": "run.start", "agent": "A",
                                 "data": {"prompt": f"needle {i}"}, "ts": 1.0, "id": "x"}) + "\n")
        t = time.time() - 100 + i
        os.utime(f, (t, t))
    t = time.time() - 60
    os.utime(c.log_dir, (t, t))
    return c


def _search(c, **a):
    ctx = SimpleNamespace(core=SimpleNamespace(codex=c), run_id="me", name="A")
    return asyncio.run(tools_extra.t_codex_search(ctx, a))


def test_codex_search_reuses_run_scan(tmp_path, monkeypatch):
    c = _cx(tmp_path, monkeypatch)
    n = {"scans": 0}
    real = safeio.newest_entries

    def wrap(*a, **k):
        n["scans"] += 1
        return real(*a, **k)
    monkeypatch.setattr(safeio, "newest_entries", wrap)
    a = _search(c, query="needle")
    b = _search(c, query="needle")
    c.runs(10)
    assert a == b and "needle 5" in a
    assert n["scans"] == 1


def test_codex_search_sees_new_run_at_once(tmp_path, monkeypatch):
    c = _cx(tmp_path, monkeypatch)
    _search(c, query="needle")
    (c.log_dir / "fresh.jsonl").write_text(json.dumps({"run_id": "fresh", "kind": "run.start", "agent": "A",
                                                        "data": {"prompt": "brand new"}, "ts": 2.0}) + "\n")
    assert "brand new" in _search(c, query="brand")


def test_codex_search_fake_codex_without_cache(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps({"run_id": "a", "kind": "run.start", "agent": "A",
                                           "data": {"prompt": "plain"}, "ts": 1.0}) + "\n")
    out = _search(SimpleNamespace(log_dir=d), query="plain")
    assert "plain" in out


def test_codex_search_runs_cap_beyond_cache(tmp_path, monkeypatch):
    c = _cx(tmp_path, monkeypatch, n=3)
    monkeypatch.setattr(codex, "RUNS_CACHE_KEEP", 2)
    out = _search(c, query="needle", runs=3)
    assert all(f"needle {i}" in out for i in range(3))
