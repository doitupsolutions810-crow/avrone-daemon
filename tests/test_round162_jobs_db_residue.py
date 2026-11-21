"""Round 162: a jobs.db scrub leaves no copy of the old value on disk.

v0.152's fix masked the rows, but the raw values stayed readable: in jobs.db's freed cells (no
secure_delete) and in jobs.db-wal for as long as a queue held the file open. Pruned jobs left
their payloads in free pages the same way.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from avrone import jobqueue as jq


def _tok() -> str:  # built at run time: no token-shaped literal for push protection
    return "gh" + "p_" + "C" * 36


def _count(d) -> dict[str, int]:
    return {p.name: p.read_bytes().count(_tok().encode()) for p in d.iterdir() if p.name.startswith("jobs.db")}


def _seed(d):
    q = jq.JobQueue(d / "jobs.db")

    async def m():
        for i in range(30):
            await q.enqueue("run", {"task": "plain %d" % i})
        await q.close()
    asyncio.run(m())
    db = sqlite3.connect(d / "jobs.db")
    db.execute("UPDATE jobs SET status='done', result=?, error=?",
               (json.dumps({"a": "key " + _tok() + "x" * 3000}), "boom " + _tok()))
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()


def test_fix_leaves_no_raw_copy_while_queue_open(tmp_path):
    _seed(tmp_path)
    assert sum(_count(tmp_path).values()) > 0
    q = jq.JobQueue(tmp_path / "jobs.db")  # the daemon keeps the queue open
    try:
        r = jq.scrub_db(tmp_path / "jobs.db", fix=True)
        assert r["fixed"] == 30
        assert sum(_count(tmp_path).values()) == 0, _count(tmp_path)
    finally:
        asyncio.run(q.close())


def test_fix_on_wal_only_values(tmp_path):
    q = jq.JobQueue(tmp_path / "jobs.db")

    async def m():
        await q.enqueue("run", {"task": "plain"})
    asyncio.run(m())
    db = sqlite3.connect(tmp_path / "jobs.db")
    db.execute("UPDATE jobs SET error=?", ("boom " + _tok(),))
    db.commit()
    db.close()
    try:
        jq.scrub_db(tmp_path / "jobs.db", fix=True)
        assert sum(_count(tmp_path).values()) == 0, _count(tmp_path)
    finally:
        asyncio.run(q.close())


def test_queue_connection_uses_secure_delete(tmp_path):
    q = jq.JobQueue(tmp_path / "jobs.db")
    try:
        assert q._db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        asyncio.run(q.close())
