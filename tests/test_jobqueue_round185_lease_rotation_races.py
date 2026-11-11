"""Round 185: lease extension, reclamation, and completion races across connections."""
from __future__ import annotations

import asyncio

from avrone.jobqueue import JobQueue


def test_extend_vs_reclaim_has_one_authoritative_lease_owner(tmp_path):
    async def run():
        path = tmp_path / "extend-reclaim.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("lease-race", {})
            first = await q1.claim()
            assert first is not None
            q1._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))

            extended, reclaimed = await asyncio.gather(
                q1.extend(jid, first["token"], 60),
                q2.claim(),
            )

            row = await q1.get(jid)
            assert row is not None and row["status"] == "running"
            if extended:
                assert reclaimed is None
                assert row["attempts"] == 1
                db_token = q1._db.execute("SELECT token FROM jobs WHERE id=?", (jid,)).fetchone()[0]
                assert db_token == first["token"]
            else:
                assert reclaimed is not None
                assert reclaimed["id"] == jid
                assert reclaimed["token"] != first["token"]
                assert row["attempts"] == 2
                assert await q1.complete(jid, {"stale": True}, first["token"]) is False
                assert await q2.complete(jid, {"new_owner": True}, reclaimed["token"]) is True
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())


def test_complete_vs_reclaim_has_one_terminal_or_reclaimed_outcome(tmp_path):
    async def run():
        path = tmp_path / "complete-reclaim.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("lease-race", {})
            first = await q1.claim()
            assert first is not None
            q1._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))

            completed, reclaimed = await asyncio.gather(
                q1.complete(jid, {"first_owner": True}, first["token"]),
                q2.claim(),
            )

            row = await q1.get(jid)
            assert row is not None
            if completed:
                assert reclaimed is None
                assert row["status"] == "done"
                assert row["result"] == {"first_owner": True}
                assert row["attempts"] == 1
            else:
                assert reclaimed is not None
                assert reclaimed["id"] == jid
                assert reclaimed["token"] != first["token"]
                assert row["status"] == "running"
                assert row["attempts"] == 2
                assert await q1.complete(jid, {"stale": True}, first["token"]) is False
                assert await q2.complete(jid, {"new_owner": True}, reclaimed["token"]) is True
                final = await q1.get(jid)
                assert final is not None and final["status"] == "done"
                assert final["result"] == {"new_owner": True}
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())


def test_fail_vs_reclaim_keeps_old_token_fenced(tmp_path):
    async def run():
        path = tmp_path / "fail-reclaim.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("lease-race", {})
            first = await q1.claim()
            assert first is not None
            q1._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))

            failed, reclaimed = await asyncio.gather(
                q1.fail(jid, "worker failed", first["token"]),
                q2.claim(),
            )
            row = await q1.get(jid)
            assert row is not None
            if failed == "requeued":
                assert reclaimed is None
                assert row["status"] == "queued"
                assert row["attempts"] == 1
                assert await q1.complete(jid, {"stale": True}, first["token"]) is False
            else:
                assert failed == "stale"
                assert reclaimed is not None and reclaimed["id"] == jid
                assert reclaimed["token"] != first["token"]
                assert row["status"] == "running" and row["attempts"] == 2
                assert await q1.fail(jid, "stale failure", first["token"]) == "stale"
                assert await q2.complete(jid, {"new_owner": True}, reclaimed["token"]) is True
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())


def test_release_vs_reclaim_rotates_token_and_attempt_once(tmp_path):
    async def run():
        path = tmp_path / "release-reclaim.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("lease-race", {})
            first = await q1.claim()
            assert first is not None
            q1._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))

            released, reclaimed = await asyncio.gather(
                q1.release(jid, first["token"]),
                q2.claim(),
            )
            assert reclaimed is not None and reclaimed["id"] == jid
            assert reclaimed["token"] != first["token"]
            assert await q1.complete(jid, {"stale": True}, first["token"]) is False
            row = await q1.get(jid)
            assert row is not None and row["status"] == "running"
            assert row["attempts"] == (1 if released else 2)
            assert await q2.complete(jid, {"new_owner": True}, reclaimed["token"]) is True
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())


def test_stale_owner_cannot_extend_after_reclaim(tmp_path):
    async def run():
        path = tmp_path / "stale-extend.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("lease-race", {})
            first = await q1.claim()
            assert first is not None
            q1._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))
            reclaimed = await q2.claim()
            assert reclaimed is not None and reclaimed["token"] != first["token"]

            assert await q1.extend(jid, first["token"], 3600) is False
            row = await q1.get(jid)
            assert row is not None and row["status"] == "running"
            assert row["attempts"] == 2
            assert await q2.complete(jid, {"new_owner": True}, reclaimed["token"]) is True
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())
