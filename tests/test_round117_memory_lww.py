"""Round 117: memory last-writer-wins across clock steps and equal timestamps.

v0.109: a local save after the wall clock stepped back got a ts older than the value it
replaced; it won locally (file order) but every fleet peer skipped it in merge(), so the new
value never replicated until the clock caught up. Two nodes saving one key in the same clock
tick each skipped the other's value (>= ts) and diverged for good.
"""
from __future__ import annotations

import time
from unittest import mock

from avrone import memstore


def _val(store, ns, key):
    return store._fold(store._records())[ns][key]["value"]


def _all(store, ns):
    return list(store._fold(store._records())[ns].values())


def test_save_after_clock_step_back_replicates(tmp_path):
    a = memstore.MemoryStore(tmp_path / "a.jsonl")
    b = memstore.MemoryStore(tmp_path / "b.jsonl")
    real = time.time
    a.save("ns", "k", "v1")
    b.merge("ns", _all(a, "ns"))
    with mock.patch.object(memstore.time, "time", lambda: real() - 600):
        rec = a.save("ns", "k", "v2")
    assert _val(a, "ns", "k") == "v2"
    st = b.merge("ns", _all(a, "ns"))
    assert st["merged"] == 1 and _val(b, "ns", "k") == "v2", st
    assert rec["ts"] > b._fold(b._records())["ns"]["k"]["ts"] - 1  # returned record carries the bumped ts


def test_save_ts_monotonic_per_key(tmp_path):
    a = memstore.MemoryStore(tmp_path / "a.jsonl")
    with mock.patch.object(memstore.time, "time", lambda: 1_000_000.0):
        t1 = a.save("ns", "k", "x")["ts"]
        t2 = a.save("ns", "k", "y")["ts"]  # same clock reading
    assert t2 > t1


def test_far_future_local_record_does_not_drag_saves(tmp_path):
    a = memstore.MemoryStore(tmp_path / "a.jsonl")
    a.save("ns", "k", "x")
    raw = (tmp_path / "a.jsonl").read_text().replace('"ts": ', '"ts": 9e15, "old_ts": ', 1)
    (tmp_path / "a.jsonl").write_text(raw)
    t = a.save("ns", "k", "y")["ts"]
    assert t <= time.time() + memstore.MAX_FUTURE_SKEW_S + 1


def test_equal_ts_converges(tmp_path):
    a = memstore.MemoryStore(tmp_path / "a.jsonl")
    b = memstore.MemoryStore(tmp_path / "b.jsonl")
    with mock.patch.object(memstore.time, "time", lambda: 1_700_000_000.0):
        a.save("ns", "k", "from-a")
        b.save("ns", "k", "from-b")
    ea, eb = _all(a, "ns"), _all(b, "ns")
    a.merge("ns", eb)
    b.merge("ns", ea)
    assert _val(a, "ns", "k") == _val(b, "ns", "k") == "from-b"


def test_equal_ts_same_value_skipped(tmp_path):
    a = memstore.MemoryStore(tmp_path / "a.jsonl")
    with mock.patch.object(memstore.time, "time", lambda: 1_700_000_000.0):
        a.save("ns", "k", "same")
    st = a.merge("ns", [{"key": "k", "value": "same", "ts": 1_700_000_000.0}])
    assert st == {"merged": 0, "skipped": 1, "rejected": 0}
