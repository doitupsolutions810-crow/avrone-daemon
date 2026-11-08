"""Round 112: a wall clock stepped backwards (NTP correction, VM resume) never strands state.

v0.104:
- the gateway breaker's open_until was wall-clock: a step back of an hour kept it open an hour;
- presence.environment's cache compared time.time() ages: a step back made the cached
  environment live until the clock caught up;
- fleet cooldown_until written before a step back kept a healthy peer out of select_nodes;
- jobqueue leases (and retry backoffs) written before a step back parked a dead worker's job for
  the whole step (up to EXTEND_MAX_S).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

from avrone import gateway, jobqueue, presence

REAL = time.time


def test_breaker_reopens_after_clock_step_back(monkeypatch):
    up = gateway.Upstream(provider="p", base_url="http://x", model="m")
    for _ in range(3):
        up.record(False, threshold=3, cooldown=0.05)
    assert not up.healthy()
    monkeypatch.setattr(time, "time", lambda: REAL() - 3600)
    time.sleep(0.08)
    assert up.healthy(), "breaker stayed open for the whole clock step"
    assert up.status()["reopen_in"] == 0.0


def test_breaker_still_opens_and_reports():
    up = gateway.Upstream(provider="p", base_url="http://x", model="m")
    for _ in range(3):
        up.record(False, threshold=3, cooldown=30)
    st = up.status()
    assert st["state"] == "open" and 0 < st["reopen_in"] <= 30


def test_presence_cache_expires_across_step_back(monkeypatch):
    calls = []
    core = SimpleNamespace(runner=SimpleNamespace(kind="local"), settings=SimpleNamespace(),
                           tools=SimpleNamespace(names=lambda: calls.append(1) or []), active={})
    presence._CACHE.pop(id(core), None)
    asyncio.run(presence.environment(core, ttl=0.05))
    n = len(calls)
    monkeypatch.setattr(time, "time", lambda: REAL() - 3600)
    time.sleep(0.08)
    asyncio.run(presence.environment(core, ttl=0.05))
    presence._CACHE.pop(id(core), None)
    assert len(calls) > n, "cached environment never expired after the clock stepped back"


def _shift(path, jid, **cols):
    db = sqlite3.connect(path)
    db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), jid))
    db.commit()
    db.close()


def test_running_lease_from_future_clock_reclaimed(tmp_path):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "q.db", lease_s=900)
        jid = await q.enqueue("k", {"a": 1})
        got = await q.claim()
        assert got and got["id"] == jid
        # the worker died, then the clock stepped back a day: its lease and `updated` are a day ahead
        now = time.time()
        _shift(tmp_path / "q.db", jid, updated=now + 86400, lease_until=now + 86400)
        again = await q.claim()
        await q.close()
        return jid, again
    jid, again = asyncio.run(go())
    assert again and again["id"] == jid, "a dead worker's job stayed leased for the whole clock step"


def test_queued_backoff_from_future_clock_rebased(tmp_path):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "q.db")
        jid = await q.enqueue("k", {"a": 1})
        now = time.time()
        _shift(tmp_path / "q.db", jid, updated=now + 86400, lease_until=now + 86400 + 2)
        first = await q.claim()  # rebased to ~2 s from now: not yet
        await asyncio.sleep(2.2)
        second = await q.claim()
        await q.close()
        return jid, first, second
    jid, first, second = asyncio.run(go())
    assert first is None
    assert second and second["id"] == jid


def test_live_lease_untouched(tmp_path):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "q.db", lease_s=900)
        await q.enqueue("k", {"a": 1})
        got = await q.claim()
        again = await q.claim()
        await q.close()
        return got, again
    got, again = asyncio.run(go())
    assert got and again is None


def test_fleet_cooldown_from_future_clock_ignored():
    from avrone import fleet
    fm = fleet.FleetManager.__new__(fleet.FleetManager)
    fm.heartbeat_s = 10
    n = {"node_id": "kid", "last_seen": time.time(), "cooldown_until": time.time() + 3600}
    fm.nodes = {"kid": n}
    fm.status_of = lambda rec: "online"
    fm.allowed = lambda nid: True
    fm.node_state = lambda rec: "ok"
    picked = fleet.FleetManager.select_nodes(fm, 1)
    assert picked and picked[0]["node_id"] == "kid", "a cooldown an hour ahead kept a healthy peer out"
