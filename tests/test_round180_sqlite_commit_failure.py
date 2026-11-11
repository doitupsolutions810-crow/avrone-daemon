"""Round 180: a failed SQLite COMMIT must not poison the queue connection."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from avrone.jobqueue import JobQueue


def test_commit_failure_rolls_back_before_connection_reuse(tmp_path):
    async def run_case():
        q = JobQueue(tmp_path / "commit-failure.db")
        deny_commit = {"once": True}

        def authorizer(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT" and deny_commit["once"]:
                deny_commit["once"] = False
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        try:
            q._db.set_authorizer(authorizer)
            with pytest.raises(sqlite3.DatabaseError):
                await q.enqueue("commit-fault", {"must": "not persist"})
            assert q._db.in_transaction is False
            assert await q.stats() == {}

            q._db.set_authorizer(None)
            jid = await q.enqueue("recovered", {"connection": "usable"})
            claimed = await q.claim()
            assert claimed is not None and claimed["id"] == jid
            assert await q.complete(jid, {"ok": True}, claimed["token"])
            row = await q.get(jid)
            assert row is not None and row["status"] == "done"
            assert row["result"] == {"ok": True}
        finally:
            q._db.set_authorizer(None)
            await q.close()

    asyncio.run(run_case())
