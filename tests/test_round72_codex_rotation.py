"""Round 72: codex event logs grow without bound.

fleet.jsonl gains an event per heartbeat, join, dispatch and refusal for the whole life of a node,
and a long or looping run grows its own log until the disk fills. Logs now rotate by size
(AVRONE_CODEX_LOG_MAX_BYTES, default 64 MB) into <stem>.jsonl.1 .. .N (AVRONE_CODEX_LOG_KEEP,
default 3). Archives don't match *.jsonl, so listings still show one entry per run, and replay()
reads back through the archives when the active file is short.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from avrone import codex as codex_mod
from avrone.codex import Codex


@pytest.fixture
def cx(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_CODEX_LOG_MAX_BYTES", "8192")
    monkeypatch.setenv("AVRONE_CODEX_LOG_KEEP", "3")
    monkeypatch.delenv("AVRONE_CODEX_REDACT", raising=False)
    return Codex(tmp_path)


def _files(cx, stem):
    return sorted(p.name for p in cx.log_dir.iterdir() if p.name.startswith(stem + ".jsonl"))


def _sizes_ok(cx, stem, cap):
    for n in _files(cx, stem):
        assert (cx.log_dir / n).stat().st_size <= cap


def test_log_is_bounded_with_archives(cx):
    pad = "p" * 200
    for i in range(2000):
        cx.emit("fleet", "fleet.tick", "node", i=i, pad=pad)
    assert _files(cx, "fleet") == ["fleet.jsonl", "fleet.jsonl.1", "fleet.jsonl.2", "fleet.jsonl.3"]
    _sizes_ok(cx, "fleet", 8192)
    total = sum((cx.log_dir / n).stat().st_size for n in _files(cx, "fleet"))
    assert total <= 4 * 8192  # was ~520 KB and growing
    assert cx.rotations > 50
    for n in _files(cx, "fleet"):  # every file holds whole lines only
        for line in (cx.log_dir / n).read_text().splitlines():
            json.loads(line)


def test_replay_spans_archives_newest_in_order(cx):
    for i in range(300):
        cx.emit("r1", "step", "a", i=i, pad="x" * 100)
    assert len(_files(cx, "r1")) > 2
    evs = cx.replay("r1", limit=100)
    assert [e["data"]["i"] for e in evs] == list(range(200, 300))
    active = sum(1 for _ in (cx.log_dir / "r1.jsonl").open())
    assert active < 100  # so the replay really read into the archives


def test_replay_limit_larger_than_everything_kept(cx):
    for i in range(300):
        cx.emit("r2", "step", "a", i=i, pad="x" * 100)
    evs = cx.replay("r2", limit=10_000)
    got = [e["data"]["i"] for e in evs]
    assert got == sorted(got) and got[-1] == 299 and got[0] > 0  # oldest dropped, order kept


def test_runs_lists_one_entry_with_start_prompt(cx):
    cx.emit("r3", "run.start", "lead", prompt="find the thing")
    for i in range(200):
        cx.emit("r3", "step", "a", i=i, pad="x" * 100)
    runs = [r for r in cx.runs() if r["run_id"] == "r3"]
    assert len(runs) == 1
    # start event survives in the oldest archive until it is dropped past KEEP
    assert runs[0]["prompt"] in ("find the thing", "")


def test_runs_ignores_archives(cx):
    for i in range(200):
        cx.emit("r4", "step", "a", i=i, pad="x" * 100)
    assert sorted(r["run_id"] for r in cx.runs()) == ["r4"]


def test_disabled_and_keep_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_CODEX_LOG_MAX_BYTES", "0")
    c = Codex(tmp_path / "a")
    for i in range(200):
        c.emit("r", "s", "a", pad="x" * 100)
    assert _files(c, "r") == ["r.jsonl"] and (c.log_dir / "r.jsonl").stat().st_size > 8192
    monkeypatch.setenv("AVRONE_CODEX_LOG_MAX_BYTES", "8192")
    monkeypatch.setenv("AVRONE_CODEX_LOG_KEEP", "0")
    c2 = Codex(tmp_path / "b")
    for i in range(200):
        c2.emit("r", "s", "a", i=i, pad="x" * 100)
    assert _files(c2, "r") == ["r.jsonl"] and (c2.log_dir / "r.jsonl").stat().st_size <= 8192
    assert c2.replay("r")[-1]["data"]["i"] == 199


def test_tiny_cap_is_floored_and_junk_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_CODEX_LOG_MAX_BYTES", "10")
    assert codex_mod.log_max_bytes() == 4096
    monkeypatch.setenv("AVRONE_CODEX_LOG_MAX_BYTES", "lots")
    assert codex_mod.log_max_bytes() == codex_mod.DEFAULT_LOG_MAX_BYTES
    monkeypatch.setenv("AVRONE_CODEX_LOG_KEEP", "-5")
    assert codex_mod.log_keep() == 0


def test_event_bigger_than_cap_still_written(cx):
    cx.emit("r5", "s", "a", pad="small")
    cx.emit("r5", "big", "a", pad="y" * 20_000)  # rotates, then the oversized line lands whole
    cx.emit("r5", "after", "a")
    kinds = [e["kind"] for e in cx.replay("r5")]
    assert kinds == ["s", "big", "after"]


def test_rotation_failure_never_drops_events(cx, monkeypatch):
    (cx.log_dir / "r6.jsonl.1").mkdir()  # archive slot taken by a directory
    for i in range(200):
        cx.emit("r6", "s", "a", i=i, pad="x" * 100)
    evs = cx.replay("r6", limit=10_000)
    assert len(evs) == 200  # all kept in the active log
    assert (cx.log_dir / "r6.jsonl.1").is_dir()


def test_planted_symlink_archive_not_followed(cx, tmp_path):
    secret = tmp_path / "secret.jsonl"
    secret.write_text(json.dumps({"run_id": "r7", "kind": "LEAK", "agent": "x", "data": {}, "ts": 0}) + "\n")
    cx.emit("r7", "s", "a")
    os.symlink(secret, cx.log_dir / "r7.jsonl.1")
    assert all(e["kind"] != "LEAK" for e in cx.replay("r7", limit=100))


def test_keep_shrink_prunes_extra_archives(cx, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_LOG_KEEP", "5")
    for i in range(600):
        cx.emit("r8", "s", "a", pad="x" * 100)
    assert len(_files(cx, "r8")) == 6
    monkeypatch.setenv("AVRONE_CODEX_LOG_KEEP", "2")
    for i in range(100):
        cx.emit("r8", "s", "a", pad="x" * 100)
    assert _files(cx, "r8") == ["r8.jsonl", "r8.jsonl.1", "r8.jsonl.2"]


def test_concurrent_writers_lose_nothing_within_window(cx, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_LOG_KEEP", "100")

    def w(t):
        for i in range(150):
            cx.emit("r9", "s", f"t{t}", i=i, pad="x" * 60)
    ts = [threading.Thread(target=w, args=(t,)) for t in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    evs = cx.replay("r9", limit=50_000)
    assert len(evs) == 1200
    for t in range(8):
        assert [e["data"]["i"] for e in evs if e["agent"] == f"t{t}"] == list(range(150))


def test_torn_tail_before_rotation(cx):
    cx.emit("r10", "s", "a")
    with (cx.log_dir / "r10.jsonl").open("ab") as f:
        f.write(b'{"run_id": "r10", "kind": "torn')  # crash mid-write
    for i in range(100):
        cx.emit("r10", "s", "a", i=i, pad="x" * 100)
    evs = cx.replay("r10", limit=10_000)
    assert [e["data"].get("i") for e in evs if "i" in e["data"]] == list(range(100))
