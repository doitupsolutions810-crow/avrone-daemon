"""Round 178: abrupt process death inside an uncommitted SQLite write transaction."""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
import time

from avrone.jobqueue import JobQueue


def _die_with_open_transaction(db_path: str, ready) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO jobs(id,kind,payload,status,priority,max_attempts,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("crash-uncommitted", "crash-test", "{}", "queued", 0, 3, time.time(), time.time()),
        )
        ready.set()
        time.sleep(60)  # parent terminates this process before COMMIT
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_killed_writer_rolls_back_uncommitted_wal_transaction(tmp_path):
    db_path = tmp_path / "crash-recovery.db"

    async def seed():
        q = JobQueue(db_path)
        try:
            return await q.enqueue("survivor", {"must": "remain"})
        finally:
            await q.close()

    survivor_id = asyncio.run(seed())
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    writer = ctx.Process(target=_die_with_open_transaction, args=(str(db_path), ready))
    writer.start()
    try:
        assert ready.wait(20), "writer did not reach its uncommitted insert"
        writer.terminate()
        writer.join(timeout=10)
        assert not writer.is_alive(), "writer process survived terminate()"
        assert writer.exitcode is not None and writer.exitcode != 0
    finally:
        if writer.is_alive():
            writer.kill()
        writer.join(timeout=5)

    async def recover():
        q = JobQueue(db_path)
        try:
            assert await q.get("crash-uncommitted") is None
            survivor = await q.get(survivor_id)
            assert survivor is not None and survivor["status"] == "queued"
            claimed = await q.claim()
            assert claimed is not None and claimed["id"] == survivor_id
            assert await q.complete(survivor_id, {"recovered": True}, claimed["token"])
            assert (await q.get(survivor_id))["result"] == {"recovered": True}
            return await q.stats()
        finally:
            await q.close()

    assert asyncio.run(recover()) == {"done": 1}
