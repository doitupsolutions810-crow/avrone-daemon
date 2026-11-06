"""Round 73: every codex log reader sees rotated archives, and none crashes on a rotation race.

Round 72 rotates <id>.jsonl into <id>.jsonl.1..N. The readers kept looking at the active file
only: codex_search missed the run's older events (and said "run not found" in the instant between
the rename and the next write), codex_stats reported a run as nearly empty just after a rotation,
the HUD telemetry window lost the events a rotation moved, and the OSINT rollup undercounted.
codex_search also sorted the listing with a bare p.stat() (a log that rotated or vanished between
glob and stat raised FileNotFoundError) and opened symlinked logs found by the listing.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import telemetry, tools_codexstats as cs, tools_extra as tx
from avrone.codex import Codex, log_chain


@pytest.fixture
def cx(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_CODEX_LOG_MAX_BYTES", "8192")
    monkeypatch.setenv("AVRONE_CODEX_LOG_KEEP", "20")
    return Codex(tmp_path)


def _ctx(cx, run_id="r1", name="W"):
    core = SimpleNamespace(codex=cx, settings=SimpleNamespace(data_dir=str(cx.log_dir.parent)))
    return SimpleNamespace(core=core, run_id=run_id, name=name, depth=0)


def _search(cx, **args):
    return asyncio.run(tx.t_codex_search(_ctx(cx), args))


def _fill(cx, run="r1", n=200, agent="W", kind="tool.call"):
    for i in range(n):
        cx.emit(run, kind, agent, tool=f"t{i % 3}", i=i, pad="x" * 100)
    assert cx.log_dir.joinpath(f"{run}.jsonl.1").exists()


def test_search_by_run_reads_archives(cx):
    cx.emit("r1", "run.start", "lead", prompt="needle-early")
    _fill(cx)
    out = _search(cx, run_id="r1", query="needle-early")
    assert out.startswith("1 matching event(s)")


def test_search_listing_reads_archives(cx):
    cx.emit("r1", "marker", "lead", tag="oldest")
    _fill(cx)
    assert "1 matching event(s) in 1 run(s)" in _search(cx, kind="marker")


def test_search_run_found_when_active_rotated_away(cx):
    _fill(cx)
    (cx.log_dir / "r1.jsonl").unlink()  # the instant after a rename, before the next write
    out = _search(cx, run_id="r1", kind="tool.call")
    assert "matching event(s)" in out and "not found" not in out


def test_search_listing_survives_vanishing_and_symlinked_logs(cx, tmp_path):
    cx.emit("good", "x", "A")
    outside = tmp_path / "outside.txt"
    outside.write_text(json.dumps({"kind": "x", "agent": "LEAK", "ts": 9, "data": {}}) + "\n")
    os.symlink(outside, cx.log_dir / "planted.jsonl")
    os.symlink(tmp_path / "gone", cx.log_dir / "dangling.jsonl")  # stat() raised FileNotFoundError
    out = _search(cx, kind="x")
    assert out.startswith("1 matching event(s) in 1 run(s)") and "LEAK" not in out


def test_search_archive_hits_name_the_run(cx):
    cx.emit("r1", "marker", "lead")
    _fill(cx)
    out = _search(cx, kind="marker")
    assert "[r1]" in out and ".jsonl" not in out


def test_stats_counts_across_rotation(cx):
    _fill(cx, n=300)
    st = cs.codex_stats(_ctx(cx), scope="self")
    assert st["events"] == 300  # was the handful left in the active file
    assert sum(st["tools_called"].values()) == 300


def test_stats_budget_still_caps_and_flags_truncation(cx, monkeypatch):
    _fill(cx, n=300)
    monkeypatch.setattr(cs, "CODEX_STATS_MAX_BYTES", 10_000)
    st = cs.codex_stats(_ctx(cx), scope="self")
    assert 0 < st["events"] < 300 and st.get("truncated") in (True, None)


def test_stats_active_missing_reads_archives(cx):
    _fill(cx, n=300)
    (cx.log_dir / "r1.jsonl").unlink()
    assert cs.codex_stats(_ctx(cx), scope="self")["events"] > 0


def test_stats_symlinked_archive_ignored(cx, tmp_path):
    cx.emit("r1", "tool.call", "W", tool="a")
    bad = tmp_path / "bad"
    bad.write_text("\n".join(json.dumps({"run_id": "r1", "kind": "tool.call", "agent": "W",
                                         "ts": 1, "data": {"tool": "EVIL"}}) for _ in range(5)) + "\n")
    os.symlink(bad, cx.log_dir / "r1.jsonl.1")
    st = cs.codex_stats(_ctx(cx), scope="self")
    assert "EVIL" not in st["tools_called"] and st["events"] == 1


def test_telemetry_window_includes_rotated_events(cx):
    since = time.time() - 60
    _fill(cx, n=300)
    evs = [e for e in telemetry._iter_events(cx.log_dir, since) if e.get("run_id") == "r1"]
    assert len(evs) == 300


def test_telemetry_skips_archives_older_than_window(cx):
    _fill(cx, n=300)
    old = time.time() - 10_000
    for a in cx.archives(cx.log_dir / "r1.jsonl"):
        os.utime(a, (old, old))
    since = time.time() - 60
    n = sum(1 for _ in telemetry._iter_events(cx.log_dir, since))
    assert n < 300


def test_osint_metrics_counts_archived_probes(cx):
    for i in range(200):
        cx.emit("r1", "recon.dns", "W", host=f"h{i % 4}.example.com", pad="x" * 100)
    from avrone import osint
    out = asyncio.run(osint.t_osint_metrics(_ctx(cx), {}))
    assert "200" in out


def test_log_chain_edges(cx, tmp_path):
    p = cx.log_dir / "z.jsonl"
    assert log_chain(p) == []
    (cx.log_dir / "z.jsonl.1").write_text("")
    assert log_chain(p) == [cx.log_dir / "z.jsonl.1"]
    p.mkdir()
    assert log_chain(p) == [cx.log_dir / "z.jsonl.1"]  # a directory is not the active log
    (cx.log_dir / "z.jsonl.3").write_text("")  # gap at .2: stop
    assert log_chain(p) == [cx.log_dir / "z.jsonl.1"]
