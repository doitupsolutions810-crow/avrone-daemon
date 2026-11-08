"""Round 103: run_logs counts and prunes rotated archives, and never deletes a log that changed.

v0.95: usage() globbed *.jsonl only, so <run>.jsonl.N archives (round 72 rotation) were missing from
the reported size, prune() unlinked the active log and left every archive behind, and a run whose
active log was gone (archives only) could never be freed. prune also unlinked a log that a resumed
run had appended to after the scan.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

from avrone import tools_limits as tl

OLD = time.time() - 90 * 86400


def _run(d: Path, name: str, i: int, archives: int = 2, active: bool = True):
    paths = []
    if active:
        f = d / f"{name}.jsonl"
        f.write_text("x\n" * 50)
        paths.append(f)
    for k in range(1, archives + 1):
        a = d / f"{name}.jsonl.{k}"
        a.write_text("y" * 1000)
        paths.append(a)
    for p in paths:
        os.utime(p, (OLD + i, OLD + i))
    return paths


def test_usage_counts_archives(tmp_path):
    for i in range(3):
        _run(tmp_path, f"r{i}", i)
    u = tl.usage(tmp_path)
    assert u["count"] == 3
    assert u["bytes"] == 3 * (100 + 2000)
    assert u["archives"] == 6


def test_prune_removes_archives_too(tmp_path):
    for i in range(15):
        _run(tmp_path, f"r{i}", i)
    r = tl.prune(tmp_path, 30, 10, set())
    assert r["removed"] == 5 and r["freed"] == 5 * 2100
    left = {p.name for p in tmp_path.iterdir()}
    assert not any(n.startswith(("r0.", "r1.", "r2.", "r3.", "r4.")) for n in left)
    assert len(left) == 30


def test_prune_frees_orphan_archives(tmp_path):
    for i in range(10):
        _run(tmp_path, f"k{i}", 100 + i)
    _run(tmp_path, "orphan", 0, archives=3, active=False)
    u = tl.usage(tmp_path)
    assert u["count"] == 11 and u["orphans"] == 1
    r = tl.prune(tmp_path, 30, 10, set())
    assert r["removed"] == 1 and r["freed"] == 3000
    assert not list(tmp_path.glob("orphan*"))


def test_prune_keeps_active_run_archives(tmp_path):
    for i in range(12):
        _run(tmp_path, f"r{i}", i)
    tl.prune(tmp_path, 30, 10, {"r0"})
    assert (tmp_path / "r0.jsonl.2").exists() and (tmp_path / "r0.jsonl").exists()
    assert not (tmp_path / "r1.jsonl.1").exists()


def test_prune_skips_log_appended_after_scan(tmp_path, monkeypatch):
    for i in range(11):
        _run(tmp_path, f"r{i}", i, archives=0)
    real = tl.usage

    def racing(d):
        u = real(d)
        with open(tmp_path / "r0.jsonl", "a") as f:  # a resumed run writes between scan and unlink
            f.write("new event\n")
        return u
    monkeypatch.setattr(tl, "usage", racing)
    r = tl.prune(tmp_path, 30, 10, set())
    assert r["removed"] == 0
    assert "new event" in (tmp_path / "r0.jsonl").read_text()


def test_symlinked_archive_not_followed_or_deleted(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    runs = tmp_path / "runs"
    runs.mkdir()
    for i in range(11):
        _run(runs, f"r{i}", i, archives=0)
    os.symlink(outside, runs / "r0.jsonl.1")
    u = tl.usage(runs)
    assert u["archives"] == 0
    tl.prune(runs, 30, 10, set())
    assert outside.read_text() == "keep me" and (runs / "r0.jsonl.1").is_symlink()


def test_non_archive_names_ignored(tmp_path):
    for n in ("a.jsonl.torn", "b.jsonl.0", "c.jsonl.01", ".jsonl", "d.jsonl.1x", "e.jsonl.1.bak"):
        (tmp_path / n).write_text("z")
    assert tl.usage(tmp_path)["count"] == 0


def test_prune_order_oldest_archive_first(tmp_path, monkeypatch):
    for i in range(11):
        _run(tmp_path, f"r{i}", i, archives=3)
    order = []
    real = tl._unlink_if_same

    def spy(p, *a):
        if p.name.startswith("r0."):
            order.append(p.name)
        return real(p, *a)
    monkeypatch.setattr(tl, "_unlink_if_same", spy)
    tl.prune(tmp_path, 30, 10, set())
    assert order == ["r0.jsonl.3", "r0.jsonl.2", "r0.jsonl.1", "r0.jsonl"]


def test_run_logs_tool_reports_archives(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    for i in range(2):
        _run(runs, f"r{i}", i)
    _run(runs, "orph", 5, active=False)
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)), active={}),
                          run_id="me")
    out = asyncio.run(tl.t_run_logs(ctx, {}))
    assert "3 run log(s)" in out and "rotated archive" in out and "archives only" in out
