"""Round 176: multi-process queue ownership and transaction rollback faults.

These tests use separate JobQueue connections and OS processes against one SQLite file.
They assert externally visible invariants rather than timing-specific implementation details.
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from avrone.jobqueue import JobQueue


def _claim_and_complete(db_path: str, ready, start, results) -> None:
    """Spawn-safe worker: open an independent connection, then race on a shared start gate."""
    async def run_worker():
        q = JobQueue(Path(db_path), lease_s=30)
        try:
            ready.put("ready")
            if not start.wait(20):
                raise TimeoutError("start gate was not released")
            claimed = []
            while True:
                job = await q.claim()
                if job is None:
                    break
                claimed.append(job["id"])
                if not await q.complete(job["id"], {"worker_result": "ok"}, job["token"]):
                    raise AssertionError("current lease owner could not complete its job")
            return claimed
        finally:
            await q.close()
    try:
        results.put(("ok", asyncio.run(run_worker())))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_separate_processes_never_claim_the_same_job(tmp_path):
    """Six processes race to claim 48 rows; every row must complete exactly once."""
    db_path = tmp_path / "multi-process.db"

    async def seed():
        q = JobQueue(db_path)
        try:
            return [await q.enqueue("process-race", {"index": i}) for i in range(48)]
        finally:
            await q.close()

    expected = set(asyncio.run(seed()))
    ctx = mp.get_context("spawn")
    ready, start, results = ctx.Queue(), ctx.Event(), ctx.Queue()
    workers = [
        ctx.Process(target=_claim_and_complete, args=(str(db_path), ready, start, results))
        for _ in range(6)
    ]
    for p in workers:
        p.start()
    try:
        for _ in workers:
            assert ready.get(timeout=25) == "ready"
        start.set()
        outputs = [results.get(timeout=35) for _ in workers]
        for p in workers:
            p.join(timeout=10)
        assert all(not p.is_alive() for p in workers), "worker process did not exit"
        assert all(p.exitcode == 0 for p in workers), [p.exitcode for p in workers]
        errors = [value for status, value in outputs if status != "ok"]
        assert not errors, errors
        claimed = [jid for status, batch in outputs if status == "ok" for jid in batch]
        assert len(claimed) == len(set(claimed)), "a job was claimed by more than one process"
        assert set(claimed) == expected, "one or more jobs were lost or never claimed"
    finally:
        start.set()
        for p in workers:
            if p.is_alive():
                p.terminate()
            p.join(timeout=5)
        ready.close()
        results.close()

    async def verify():
        q = JobQueue(db_path)
        try:
            assert await q.stats() == {"done": 48}
            for jid in expected:
                row = await q.get(jid)
                assert row is not None and row["status"] == "done"
                assert row["result"] == {"worker_result": "ok"}
        finally:
            await q.close()

    asyncio.run(verify())


def test_insert_failure_rolls_back_and_connection_remains_usable(tmp_path):
    """An injected SQLite abort must leave no partial row or open transaction behind."""
    async def run_case():
        q = JobQueue(tmp_path / "rollback.db")
        try:
            q._db.execute("""CREATE TRIGGER reject_insert BEFORE INSERT ON jobs
                BEGIN SELECT RAISE(ABORT, 'injected write refusal'); END""")
            with pytest.raises(sqlite3.IntegrityError, match="injected write refusal"):
                await q.enqueue("fault", {"should": "rollback"})
            assert await q.stats() == {}
            assert q._db.in_transaction is False
            q._db.execute("DROP TRIGGER reject_insert")
            jid = await q.enqueue("recovery", {"after": "rollback"})
            job = await q.claim()
            assert job is not None and job["id"] == jid
            assert await q.complete(jid, {"recovered": True}, job["token"])
            return await q.get(jid)
        finally:
            await q.close()

    row = asyncio.run(run_case())
    assert row["status"] == "done"
    assert row["result"] == {"recovered": True}
