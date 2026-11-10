"""Round 132: junk lesson timestamps no longer break or skew lesson_recall.

v0.124: age() raised ValueError on NaN and OverflowError on +-inf, so one lesson with ts "inf"
(hand-edited or peer-synced store) failed the whole recall with "ERROR: OverflowError"; a ts of
"inf" also sorted that lesson above every real one forever, and 1e300 printed a 300-digit age.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from avrone import bridge as bm
from avrone import tools_extra as tx
from avrone import tools_lessonrecall as lr
from avrone.tools import builtin_registry


@pytest.mark.parametrize("v", [float("nan"), float("inf"), -float("inf"), "x", None])
def test_age_junk(v):
    assert lr.age(v) == "?"


def test_age_huge_and_normal():
    assert len(lr.age(1e300)) < 12
    assert lr.age(5) == "5s" and lr.age(-5) == "0s" and lr.age(7200) == "2h"


def _recall(monkeypatch, tmp_path, recs, **args):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    monkeypatch.setattr(bm, "_store", lambda d: SimpleNamespace(load=lambda ns: dict(enumerate(recs))))
    ctx = SimpleNamespace(core=SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path))),
                          run_id="r", name="W", depth=0)
    return asyncio.run(reg.call(ctx, "lesson_recall", args))


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", float("inf"), float("nan"), 1e300, -1e300, [1]])
def test_recall_survives(monkeypatch, tmp_path, bad):
    now = time.time()
    out = _recall(monkeypatch, tmp_path, [{"key": "bad", "value": "alpha", "ts": bad},
                                          {"key": "good", "value": "beta", "ts": now - 60}])
    assert not out.startswith("ERROR"), out
    assert "good" in out and "bad" in out


@pytest.mark.parametrize("bad", ["inf", float("nan"), float("inf")])
def test_junk_ts_not_newest(monkeypatch, tmp_path, bad):
    now = time.time()
    out = _recall(monkeypatch, tmp_path, [{"key": "bad", "value": "alpha", "ts": bad},
                                          {"key": "good", "value": "beta", "ts": now - 60}])
    assert out.index("good") < out.index("bad"), out
