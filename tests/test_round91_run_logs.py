"""Round 91: secret_scan sees rotated archives; fix_runs keeps concurrent events; the newest-runs
listing is a bounded heap.

Before: secret_scan only read <run>.jsonl, so a secret that rotated into <run>.jsonl.1 was
reported CLEAN and never fixed. fix_runs read, redacted and os.replace()d each log with no lock:
every event Codex emitted in between was lost, and a log that rotated meanwhile had its archived
content put back as the active log. Codex.runs and codex_search did glob + is_file + stat per log
and sorted everything.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from avrone import tools_secrets as ts

SECRET = "password=abcdefgh1"


def test_scan_reads_rotated_archives(tmp_path):
    (tmp_path / "r.jsonl").write_text('{"a": 1}\n')
    (tmp_path / "r.jsonl.1").write_text(SECRET + "\n")
    hits = ts.scan(tmp_path)["hits"]
    assert [h["path"] for h in hits] == ["r.jsonl.1"]


def test_fix_runs_fixes_archives(tmp_path):
    (tmp_path / "r.jsonl.2").write_text(SECRET + "\n")
    assert ts.fix_runs(tmp_path, {"r.jsonl.2"}) == 1
    assert "abcdefgh1" not in (tmp_path / "r.jsonl.2").read_text()


def test_fix_runs_keeps_events_appended_during_redaction(tmp_path, monkeypatch):
    p = tmp_path / "r.jsonl"
    p.write_text(SECRET + "\n" + '{"n": 0}\n')
    from avrone import ctxspawn
    real = ctxspawn.redact
    calls = {"n": 0}

    def slow_redact(text):
        calls["n"] += 1
        if calls["n"] == 1:  # an event lands while the big read is being redacted
            with open(p, "ab") as f:
                f.write(b'{"n": 1}\n')
        return real(text)
    monkeypatch.setattr(ctxspawn, "redact", slow_redact)
    try:
        n = ts.fix_runs(tmp_path, {"r.jsonl"}, threading.Lock())
    except TypeError:  # v0.83 fix_runs took no lock argument
        n = ts.fix_runs(tmp_path, {"r.jsonl"})
    assert n == 1
    text = p.read_text()
    assert '{"n": 1}' in text and "abcdefgh1" not in text


def test_fix_runs_leaves_a_log_that_rotated(tmp_path, monkeypatch):
    p = tmp_path / "r.jsonl"
    p.write_text(SECRET + "\n")
    from avrone import ctxspawn
    real = ctxspawn.redact
    done = []

    def rotate_meanwhile(text):
        if not done:
            done.append(1)
            os.replace(p, tmp_path / "r.jsonl.1")
            p.write_text('{"fresh": 1}\n')
        return real(text)
    monkeypatch.setattr(ctxspawn, "redact", rotate_meanwhile)
    try:
        ts.fix_runs(tmp_path, {"r.jsonl"}, threading.Lock())
    except TypeError:  # v0.83 fix_runs took no lock argument
        ts.fix_runs(tmp_path, {"r.jsonl"})
    assert p.read_text() == '{"fresh": 1}\n'  # the new active log was not overwritten with old content
    assert sorted(x.name for x in tmp_path.iterdir()) == ["r.jsonl", "r.jsonl.1"]


def test_fix_runs_refuses_fifo(tmp_path):
    f = tmp_path / "r.jsonl"
    os.mkfifo(f)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("n", ts.fix_runs(tmp_path, {"r.jsonl"})), daemon=True)
    t.start()
    t.join(4)
    if t.is_alive():
        with open(f, "wb"):
            pass
        pytest.fail("fix_runs blocked on a FIFO")
    assert out["n"] == 0


def test_newest_logs_heap(tmp_path):
    from avrone import codex
    if not hasattr(codex, "newest_logs"):
        pytest.fail("codex.newest_logs missing")
    now = time.time()
    for i in range(30):
        q = tmp_path / f"r{i:02d}.jsonl"
        q.write_text("{}\n")
        os.utime(q, (now - 1000 + i, now - 1000 + i))
    os.symlink(tmp_path / "r00.jsonl", tmp_path / "zlink.jsonl")
    os.mkfifo(tmp_path / "fifo.jsonl")
    (tmp_path / "dir.jsonl").mkdir()
    got = [p.name for p in codex.newest_logs(tmp_path, 5)]
    assert got == [f"r{i:02d}.jsonl" for i in (29, 28, 27, 26, 25)]
    assert codex.newest_logs(tmp_path / "missing", 5) == []
