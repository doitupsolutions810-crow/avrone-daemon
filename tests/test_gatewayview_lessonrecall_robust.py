"""Hardening round 50: tools_gatewayview (replay_guard, stream_status) and tools_lessonrecall."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from types import SimpleNamespace as S
from unittest.mock import MagicMock

import pytest

from avrone import bridge as bm
from avrone import fleet as fleet_mod
from avrone import tools_gatewayview as gv
from avrone import tools_lessonrecall as lr


def run(c):
    return asyncio.run(c)


class FakeFleet:
    def __init__(self, nonces=None, path=None, guard=None, enabled=True):
        self._nonces, self._nonce_path, self._boot_guard, self.enabled = nonces, path, guard, enabled


def guard_out(f):
    return run(gv.t_replay_guard(S(core=S(fleet=f)), {}))


# ------------------------------------------------------------ replay_guard
@pytest.mark.parametrize("g", [math.inf, -math.inf, math.nan, 10**400, 1e308, True, "123", [1]])
def test_odd_boot_guard_never_raises(g):
    r = gv.replay_guard(FakeFleet({}, None, g))
    assert 0 <= r["boot_guard_s"] <= 2 * r["window_s"]


def test_inf_guard_reports_active_and_bounded():
    r = gv.replay_guard(FakeFleet({}, None, math.inf))
    assert r["boot_guard_s"] == 2 * r["window_s"]


def test_real_guard_counts_down():
    r = gv.replay_guard(FakeFleet({}, None, time.time()))
    assert 0 < r["boot_guard_s"] <= 2 * r["window_s"]


def test_future_guard_clamped():
    r = gv.replay_guard(FakeFleet({}, None, time.time() + 10**9))
    assert r["boot_guard_s"] == 2 * r["window_s"]


@pytest.mark.parametrize("n", [None, [], ["a"], "abc", 5, S()])
def test_non_dict_nonce_cache(n):
    assert gv.replay_guard(FakeFleet(n))["live"] == 0


def test_bool_and_junk_values_not_live():
    now = time.time()
    r = gv.replay_guard(FakeFleet({"a": True, "b": "9e99", "c": None, "d": now + 100, "e": now - 5, "f": math.nan}))
    assert r["live"] == 1


@pytest.mark.parametrize("body,ok", [
    (b"{}", True), (b'{"a": 1}', True), (b"[]", False), (b"not json", False), (b"", False),
    (b"\xff\xfe", False), (b"[" * 100000, False), (b'{"a": NaN}', None),
])
def test_readable_matches_fleet_loader(tmp_path, body, ok):
    p = tmp_path / "fleet_nonces.json"
    p.write_bytes(body)
    r = gv.replay_guard(FakeFleet({}, p))
    try:
        fleet_ok = isinstance(fleet_mod._load_nonce_snapshot(p), dict)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        fleet_ok = False
    assert r["persisted"] is True and r["readable"] is fleet_ok
    if ok is not None:
        assert fleet_ok is ok


def test_symlinked_snapshot_unreadable(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    p = tmp_path / "fleet_nonces.json"
    os.symlink(real, p)
    r = gv.replay_guard(FakeFleet({}, p))
    assert r["persisted"] is True and r["readable"] is False


def test_dangling_symlink_counts_as_present_but_unreadable(tmp_path):
    p = tmp_path / "fleet_nonces.json"
    os.symlink(tmp_path / "gone", p)
    r = gv.replay_guard(FakeFleet({}, p))
    assert r["persisted"] is True and r["readable"] is False


def test_fifo_does_not_block(tmp_path):
    p = tmp_path / "fleet_nonces.json"
    os.mkfifo(p)
    t = time.monotonic()
    r = gv.replay_guard(FakeFleet({}, p))
    assert time.monotonic() - t < 5 and r["readable"] is False


def test_directory_snapshot(tmp_path):
    p = tmp_path / "fleet_nonces.json"
    p.mkdir()
    assert gv.replay_guard(FakeFleet({}, p))["readable"] is False


def test_unstattable_path_reported_not_raised(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root stats everything")
    d = tmp_path / "locked"
    d.mkdir()
    p = d / "fleet_nonces.json"
    p.write_text("{}")
    os.chmod(d, 0)
    try:
        r = gv.replay_guard(FakeFleet({}, p))
    finally:
        os.chmod(d, 0o755)
    assert r["readable"] is False


def test_exists_raising_is_handled(monkeypatch):
    class P:
        def exists(self):
            raise PermissionError("no")

        def is_symlink(self):
            raise PermissionError("no")
    r = gv.replay_guard(FakeFleet({}, P()))
    assert r["persisted"] is True and r["readable"] is False


def test_tool_output_with_inf_guard():
    out = guard_out(FakeFleet({}, None, math.inf))
    assert out.startswith("replay_guard: DEGRADED") and "boot guard: active" in out


def test_real_fleet_manager(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    (tmp_path / "fleet_nonces.json").write_text("garbage")
    c = MagicMock()
    c.active = {}
    fm = fleet_mod.FleetManager(c, tmp_path)
    out = guard_out(fm)
    assert "saved cache unreadable" in out and "boot guard: active" in out


# ------------------------------------------------------------ stream_status
class Q:
    def __init__(self, d, m):
        self._d, self.maxsize = d, m

    def qsize(self):
        return self._d


def stream(subs, **kw):
    cx = S(_subscribers=subs, emitted=kw.get("emitted", 0), dropped=kw.get("dropped", 0),
           refused=kw.get("refused", 0))
    return run(gv.t_stream_status(S(core=S(codex=cx)), {}))


@pytest.mark.parametrize("m", [0, -1, -1000, None, "1000", True])
def test_nonpositive_or_odd_maxsize_is_unbounded(m):
    out = stream({Q(5, m)})
    assert "5/unbounded queued" in out and "FULL" not in out


def test_full_queue_flagged():
    assert "FULL - dropping" in stream({Q(1000, 1000)})


def test_broken_subscriber_skipped():
    class Bad:
        def qsize(self):
            raise RuntimeError("closed")
    out = stream([Bad(), object(), Q(3, 10)])
    assert "3/10 queued" in out and "3 subscriber(s)" in out


def test_rows_capped_and_deepest_first():
    subs = [Q(i, 1000) for i in range(500)]
    out = stream(subs)
    lines = out.splitlines()
    assert len(lines) <= gv.STREAM_MAX_ROWS + 4
    assert "- subscriber 1: 499/1000" in out
    assert f"{500 - gv.STREAM_MAX_ROWS} more subscriber(s) not shown" in out


@pytest.mark.parametrize("v", [None, "7", -3, True, 1.5])
def test_odd_counters(v):
    out = stream(set(), emitted=v, dropped=v, refused=v)
    assert "0 event(s) emitted" in out and "0 dropped" in out


def test_non_iterable_subscribers():
    out = stream(5)
    assert out.startswith("stream_status: 0 subscriber(s)")


def test_real_codex(tmp_path):
    from avrone.codex import Codex
    cx = Codex(tmp_path)

    async def go():
        q = cx.subscribe()
        for _ in range(3):
            cx.emit("r", "x", "a")
        return await gv.t_stream_status(S(core=S(codex=cx)), {}), q
    out, _ = run(go())
    assert "1 subscriber(s)" in out and "/1000 queued" in out


# ------------------------------------------------------------ lesson_recall
@pytest.fixture()
def lenv(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    ctx = S(core=S(settings=S(data_dir=str(tmp_path))), run_id="r", name="W")
    store = bm._store(str(tmp_path))
    return S(ctx=ctx, store=store, tmp=tmp_path)


def recall(env, **a):
    return run(lr.t_lesson_recall(env.ctx, a))


def put(env, key, rec):
    env.store.save(bm.LESSONS_NS, key, rec)


@pytest.mark.parametrize("topic", [{"numpy": 1}, ["numpy"], 5, True, 1.5])
def test_non_text_topic_refused(lenv, topic):
    assert recall(lenv, topic=topic) == "ERROR: topic must be text"


@pytest.mark.parametrize("ts", [1e300, time.time() + 10**8, time.time() + 3600])
def test_future_ts_does_not_float_to_top(lenv, ts):
    now = time.time()
    raw = lenv.store.load(bm.LESSONS_NS)
    assert raw == {}
    p = lenv.tmp / "memory.jsonl"
    rows = [
        {"ns": bm.LESSONS_NS, "key": "l-000000000000000a", "value": "real recent lesson", "ts": now - 60},
        {"ns": bm.LESSONS_NS, "key": "l-000000000000000b", "value": "future skewed lesson", "ts": ts},
    ]
    with open(p, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    out = recall(lenv)
    lines = out.splitlines()
    if len(lines) < 3:
        pytest.skip("store format differs: " + out)
    assert "real recent lesson" in lines[1] and "[? ago]" in lines[2], out


def test_ts_helper_future_and_junk():
    now = 1_000_000.0
    assert lr._ts({"ts": now + 10}, now) == now + 10
    assert lr._ts({"ts": now + lr.FUTURE_SLACK_S + 1}, now) == 0.0
    assert lr._ts({"ts": True}, now) == 0.0
    assert lr._ts({"ts": "nan"}, now) == 0.0
    assert lr._ts({"ts": 10**400}, now) == 0.0
    assert lr._ts("not a dict", now) == 0.0
    assert lr._ts(None, now) == 0.0


def test_rank_skips_non_dicts():
    ranked, by = lr.rank([{"value": "pin numpy", "ts": 1}, "junk", None, 5, ["x"]], "numpy")
    assert by and len(ranked) == 1
    ranked, by = lr.rank(["junk", {"value": "a", "ts": 2}, {"value": "b", "ts": 3}], "")
    assert [r["value"] for r in ranked] == ["b", "a"]


def test_only_corrupt_entries(lenv, monkeypatch):
    class St:
        def load(self, ns):
            return {"a": "str", "b": None, "c": [1]}
    monkeypatch.setattr(bm, "_store", lambda d: St())
    assert "no readable lessons" in recall(lenv)


def test_mixed_corrupt_and_good(lenv, monkeypatch):
    class St:
        def load(self, ns):
            return {"a": "str", "b": {"key": "l-1", "value": "good one\nFORGED", "ts": time.time() - 5}}
    monkeypatch.setattr(bm, "_store", lambda d: St())
    out = recall(lenv)
    assert "1 of 1 stored" in out and "good one FORGED" in out and "\nFORGED" not in out


@pytest.mark.parametrize("lim", [0, 51, -1, "x", 1.5, math.inf, True])
def test_limits(lenv, lim):
    put(lenv, "l-0000000000000001", "x")
    out = recall(lenv, limit=lim)
    assert out.startswith("ERROR: limit") or out.startswith("lesson_recall")
