"""Round 77: poisoned state files at boot (fleet nonce cache, its journal, the job queue).

1. Saved nonce expiries were trusted. `Infinity` entries never expired, so a fleet_nonces.json
   holding NONCE_CACHE_MAX of them refused every fleet request forever ("replay cache full"); a
   400-digit integer raised OverflowError out of FleetManager(), which left the node with no fleet.
   Expiries are now clamped to now + 2 * WINDOW_S (+ slack), the newest a real nonce can have.
2. The nonce journal was read whole: a multi-GB file exhausted memory at boot. Above
   NONCE_JOURNAL_MAX_BYTES it is skipped and the boot guard refuses anything signed before now.
3. A corrupt jobs.db ("file is not a database") raised out of start_queue at boot, skipping
   fleet.start() and the fleet's cleanup. It is moved aside and the queue starts empty.
4. A job whose payload is valid JSON but not an object failed in every handler with
   AttributeError until max_attempts; claim() now marks it dead at once.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import fleet as fl
from avrone.fleet import WINDOW_S, FleetManager
from avrone.jobqueue import JobQueue

nonce_expiry = getattr(fl, "nonce_expiry", None)
NONCE_JOURNAL_MAX_BYTES = getattr(fl, "NONCE_JOURNAL_MAX_BYTES", 64 * 1024 * 1024)


def _fm(d: Path) -> FleetManager:
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(d)), active={})
    return FleetManager(core, data_dir=d)


@pytest.fixture
def d(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "r77-" + "m" * 40)
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    for v in ("AVRONE_FLEET_ALLOW", "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_PARENT"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def _nonce(i: int) -> str:
    return f"{i:032x}"


def test_infinite_nonces_are_clamped(d):
    (d / "fleet_nonces.json").write_text(json.dumps({_nonce(i): float("inf") for i in range(50)}))
    fm = _fm(d)
    now = time.time()
    assert len(fm._nonces) == 50
    assert max(fm._nonces.values()) <= now + 2 * WINDOW_S + 10
    fm._nonces = {k: v for k, v in fm._nonces.items() if now + 3 * WINDOW_S <= v}
    assert fm._nonces == {}  # all of them expire within the replay window


def test_huge_integer_expiry_does_not_kill_fleet(d):
    (d / "fleet_nonces.json").write_text('{"%s": 1%s, "%s": 5}' % (_nonce(1), "0" * 400, _nonce(2)))
    fm = _fm(d)  # OverflowError before
    assert _nonce(1) in fm._nonces and _nonce(2) not in fm._nonces
    assert fm._nonces[_nonce(1)] <= time.time() + 2 * WINDOW_S + 10


def test_journal_entries_clamped(d):
    (d / "fleet_nonces.json").write_text("{}")
    (d / "fleet_nonces.json.journal").write_text(
        "".join(f"{_nonce(i)} inf\n" for i in range(10)) + f"{_nonce(99)} 1e300\n" + "junk line\n")
    fm = _fm(d)
    now = time.time()
    assert len(fm._nonces) == 11
    assert all(v <= now + 2 * WINDOW_S + 10 for v in fm._nonces.values())


def test_oversized_journal_skipped_with_boot_guard(d, monkeypatch):
    monkeypatch.setattr(fl, "NONCE_JOURNAL_MAX_BYTES", 4096, raising=False)
    (d / "fleet_nonces.json").write_text("{}")
    line = f"{_nonce(1)} {time.time() + 100}\n"
    (d / "fleet_nonces.json.journal").write_text(line * 400)  # ~16 KB > 4 KB cap
    fm = _fm(d)
    assert fm._nonces == {}
    assert fm._boot_guard is not None and abs(fm._boot_guard - time.time()) < 5


def test_nonce_expiry_helper():
    if nonce_expiry is None:
        pytest.fail("nonce_expiry missing")
    now = 1000.0
    assert nonce_expiry(True, now) is None and nonce_expiry("5", now) is None
    assert nonce_expiry(float("nan"), now) is None and nonce_expiry(999, now) is None
    assert nonce_expiry(1050.0, now) == 1050.0
    assert nonce_expiry(float("inf"), now) == now + 2 * WINDOW_S + fl.NONCE_EXP_SLACK_S
    assert nonce_expiry(10 ** 400, now) == now + 2 * WINDOW_S + fl.NONCE_EXP_SLACK_S
    assert nonce_expiry(-10 ** 400, now) is None


def test_corrupt_jobs_db_moved_aside(tmp_path):
    p = tmp_path / "jobs.db"
    p.write_bytes(b"not a database" * 100)
    q = JobQueue(p)  # DatabaseError before
    aside = list(tmp_path.glob("jobs.db.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes().startswith(b"not a database")
    jid = asyncio.run(q.enqueue("run", {"task": "x"}))
    assert jid
    asyncio.run(q.close())


def test_locked_db_is_not_treated_as_corrupt(tmp_path, monkeypatch):
    p = tmp_path / "jobs.db"
    asyncio.run(JobQueue(p).close())
    real = JobQueue._open

    def locked(self):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(JobQueue, "_open", locked, raising=False)
    with pytest.raises(sqlite3.OperationalError):
        JobQueue(p)
    monkeypatch.setattr(JobQueue, "_open", real, raising=False)
    assert not list(tmp_path.glob("jobs.db.corrupt-*"))


def test_non_object_payload_goes_dead_on_claim(tmp_path):
    q = JobQueue(tmp_path / "jobs.db")

    async def go():
        jid = await q.enqueue("run", {"task": "x"})
        q._db.execute("UPDATE jobs SET payload='[1, 2]' WHERE id=?", (jid,))
        assert await q.claim() is None
        row = q._db.execute("SELECT status, error, attempts FROM jobs WHERE id=?", (jid,)).fetchone()
        return row
    status, error, attempts = asyncio.run(go())
    assert status == "dead" and "not an object" in error and attempts == 0
    asyncio.run(q.close())
