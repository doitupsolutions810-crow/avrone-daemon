"""Round 129: runs whose ids differ only in characters the bridge file name drops kept separate bridges.

v0.121 (probe /workspace/work/capsur.py): bridge.safe_run_id stripped non-ASCII, surrogates and
punctuation and cut at 80 characters, so "r\\ud800" and "r" (or "运行1" and "运行2") shared one
bridge file: each run read, and counted against its post cap, the other's posts. The task board
already added a hash suffix for changed ids; the bridge now does the same.
"""
from __future__ import annotations

import pytest

from avrone import bridge


@pytest.mark.parametrize("a,b", [("r\ud800", "r"), ("运行1", "运行2"), ("a.b/", "a.b"),
                                 ("x" * 80 + "1", "x" * 80 + "2"), ("...", ""), ("Ω", "Ψ")])
def test_distinct_ids_distinct_files(a, b):
    assert bridge.safe_run_id(a) != bridge.safe_run_id(b)


@pytest.mark.parametrize("rid", ["r1", "abc-DEF_0.9", "0123456789abcdef", "x" * 80])
def test_safe_ids_unchanged(rid):
    assert bridge.safe_run_id(rid) == rid


@pytest.mark.parametrize("rid", ["r\ud800", "运行1", "a/b", "../etc", "x" * 500, ""])
def test_names_are_safe_and_stable(rid):
    out = bridge.safe_run_id(rid)
    assert out == bridge.safe_run_id(rid)
    assert out and len(out) <= 93 and not out.startswith(".")
    assert all(c.isascii() and (c.isalnum() or c in "_.-") for c in out)
    out.encode("utf-8")


def test_two_runs_do_not_share_a_bridge(tmp_path):
    a = bridge.Bridge("运行1", data_dir=tmp_path) if hasattr(bridge, "Bridge") else None
    if a is None:
        pytest.skip("no Bridge class")
    b = bridge.Bridge("运行2", data_dir=tmp_path)
    assert a.path != b.path
