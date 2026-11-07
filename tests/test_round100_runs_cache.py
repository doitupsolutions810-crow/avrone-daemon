"""Round 100: Codex.runs() reuses its directory scan and parsed start lines.

v0.92: every listing lstat'ed every log (100k logs: 3.4 s warm, ~17 s cold) and re-parsed each
listed run's first line (up to 4 MB) even when nothing had changed.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from avrone import codex, safeio


def _log(d, run_id, prompt="hi", ts=1.0):
    p = d / f"{run_id}.jsonl"
    p.write_text(json.dumps({"run_id": run_id, "kind": "run.start", "agent": "A",
                             "data": {"prompt": prompt}, "ts": ts, "id": "x"}) + "\n")
    return p


def _age(d, secs=60):
    t = time.time() - secs
    os.utime(d, (t, t))


@pytest.fixture
def cx(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_RUNS_CACHE_S", "30")
    c = codex.Codex(tmp_path)
    for i in range(5):
        p = _log(c.log_dir, f"r{i}", f"p{i}")
        t = time.time() - 100 + i
        os.utime(p, (t, t))
    _age(c.log_dir)
    return c


def _count_scans(monkeypatch):
    n = {"scans": 0}
    for name in (["newest_entries"] if hasattr(safeio, "newest_entries") else ["newest_files"]):
        real = getattr(safeio, name)

        def wrap(*a, _real=real, **k):
            n["scans"] += 1
            return _real(*a, **k)
        monkeypatch.setattr(safeio, name, wrap)
    return n


def test_second_listing_reuses_scan(cx, monkeypatch):
    n = _count_scans(monkeypatch)
    a = cx.runs(10)
    b = cx.runs(10)
    assert [r["run_id"] for r in a] == [r["run_id"] for r in b] == ["r4", "r3", "r2", "r1", "r0"]
    assert n["scans"] == 1


def test_in_process_append_reorders_without_rescan(cx, monkeypatch):
    cx.runs(10)
    n = _count_scans(monkeypatch)
    cx._append(cx.log_dir / "r1.jsonl", b'{"run_id": "r1", "kind": "x", "agent": "A", "data": {}, "ts": 9}\n')
    out = cx.runs(10)
    assert out[0]["run_id"] == "r1" and out[0]["prompt"] == "p1" and n["scans"] == 0


def test_new_log_shows_at_once(cx):
    cx.runs(10)
    _log(cx.log_dir, "new", "fresh")  # moves the directory mtime
    assert cx.runs(10)[0]["run_id"] == "new"


def test_deleted_log_drops_at_once(cx):
    cx.runs(10)
    (cx.log_dir / "r4.jsonl").unlink()
    assert "r4" not in [r["run_id"] for r in cx.runs(10)]


def test_ttl_expiry_picks_up_foreign_append(cx, monkeypatch):
    cx.runs(10)
    p = cx.log_dir / "r0.jsonl"
    t = time.time()
    os.utime(p, (t, t))  # another process appended
    _age(cx.log_dir)
    real = time.monotonic
    monkeypatch.setattr(codex.time, "monotonic", lambda: real() + 31)
    assert cx.runs(10)[0]["run_id"] == "r0"


def test_cache_off(cx, monkeypatch):
    monkeypatch.setenv("AVRONE_RUNS_CACHE_S", "0")
    n = _count_scans(monkeypatch)
    cx.runs(10)
    cx.runs(10)
    assert n["scans"] == 2


@pytest.mark.parametrize("junk", ["abc", "nan", "-3", "inf"])
def test_cache_setting_junk(monkeypatch, junk):
    monkeypatch.setenv("AVRONE_RUNS_CACHE_S", junk)
    v = getattr(codex, "runs_cache_s", lambda: pytest.fail("no runs_cache_s"))()
    assert v == 0.0 or v == 5.0


def test_start_line_parsed_once_and_rewrite_seen(cx, monkeypatch):
    calls = {"n": 0}
    real = codex.Codex._parse_line

    def spy(line):
        calls["n"] += 1
        return real(line)
    monkeypatch.setattr(codex.Codex, "_parse_line", staticmethod(spy))
    cx.runs(10)
    cx.runs(10)
    assert calls["n"] == 5
    p = cx.log_dir / "r2.jsonl"  # e.g. fix_runs redacting a secret in the prompt
    st = p.stat()
    p.write_text(p.read_text().replace('"p2"', '"[REDACTED]"'))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    _age(cx.log_dir)
    out = {r["run_id"]: r["prompt"] for r in cx.runs(10)}
    assert out["r2"] == "[REDACTED]"


def test_symlink_and_dotfile_never_listed(cx, tmp_path):
    cx.runs(10)
    outside = tmp_path / "x.jsonl"
    _log(tmp_path, "x", "leak")
    os.symlink(outside, cx.log_dir / "evil.jsonl")
    _log(cx.log_dir, ".hidden")
    ids = [r["run_id"] for r in cx.runs(10)]
    assert "evil" not in ids and ".hidden" not in ids
