"""Round 185: lease extension, reclamation, and completion races across connections."""
from __future__ import annotations

import asyncio
import time
import math

import pytest

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


def test_three_concurrent_claimers_produce_exactly_one_new_owner(tmp_path):
    async def run():
        path = tmp_path / "three-claimers.db"
        q1, q2, q3 = JobQueue(path), JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("lease-race", {})
            first = await q1.claim()
            assert first is not None
            q1._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))

            claims = await asyncio.gather(q2.claim(), q3.claim())
            winners = [claim for claim in claims if claim is not None]
            assert len(winners) == 1
            winner = winners[0]
            assert winner["id"] == jid
            assert winner["token"] != first["token"]

            row = await q1.get(jid)
            assert row is not None and row["status"] == "running"
            assert row["attempts"] == 2
            assert await q1.complete(jid, {"stale": True}, first["token"]) is False

            owner = q2 if claims[0] is not None else q3
            assert await owner.complete(jid, {"single_winner": True}, winner["token"]) is True
            final = await q1.get(jid)
            assert final is not None and final["status"] == "done"
            assert final["result"] == {"single_winner": True}
        finally:
            await asyncio.gather(q1.close(), q2.close(), q3.close())

    asyncio.run(run())


def test_eight_queue_connections_claim_backlog_once(tmp_path):
    async def run():
        path = tmp_path / "eight-connection-backlog.db"
        queues = [JobQueue(path) for _ in range(8)]
        try:
            expected = {
                await queues[0].enqueue("parallel-claim", {"sequence": i})
                for i in range(32)
            }
            winners = []
            while True:
                batch = await asyncio.gather(*(queue.claim() for queue in queues))
                if all(job is None for job in batch):
                    break
                winners.extend((queue, job) for queue, job in zip(queues, batch) if job is not None)

            claimed_ids = [job["id"] for _, job in winners]
            tokens = [job["token"] for _, job in winners]
            assert len(claimed_ids) == 32
            assert set(claimed_ids) == expected
            assert len(set(claimed_ids)) == 32
            assert len(set(tokens)) == 32

            completed = await asyncio.gather(*(
                queue.complete(job["id"], {"completed_by": "one-owner"}, job["token"])
                for queue, job in winners
            ))
            assert all(completed)

            rows = [await queues[0].get(jid) for jid in expected]
            assert all(row is not None and row["status"] == "done" and row["attempts"] == 1
                       for row in rows)
        finally:
            await asyncio.gather(*(queue.close() for queue in queues))

    asyncio.run(run())

@pytest.mark.parametrize("operation,expected", [
    ("complete", False),
    ("fail", "stale"),
    ("extend", False),
    ("release", False),
])
def test_forged_owner_token_cannot_mutate_live_lease(tmp_path, operation, expected):
    async def run():
        path = tmp_path / f"forged-{operation}.db"
        owner, attacker = JobQueue(path), JobQueue(path)
        try:
            jid = await owner.enqueue("lease-fence", {"safe": True})
            claim = await owner.claim()
            assert claim is not None
            token = "0" * 64 if claim["token"] != "0" * 64 else "1" * 64

            if operation == "complete":
                result = await attacker.complete(jid, {"forged": True}, token)
            elif operation == "fail":
                result = await attacker.fail(jid, "forged failure", token)
            elif operation == "extend":
                result = await attacker.extend(jid, token, 3600)
            else:
                result = await attacker.release(jid, token)

            assert result == expected
            row = await owner.get(jid)
            assert row is not None and row["status"] == "running"
            assert row["attempts"] == 1
            assert row["result"] is None
            assert await owner.complete(jid, {"legitimate": True}, claim["token"]) is True
            final = await owner.get(jid)
            assert final is not None and final["status"] == "done"
            assert final["result"] == {"legitimate": True}
        finally:
            await asyncio.gather(owner.close(), attacker.close())

    asyncio.run(run())




@pytest.mark.parametrize("seconds", [
    float("nan"), float("inf"), float("-inf"), 1e300, -1e300, "not-a-number", None,
])
def test_extend_duration_never_writes_nonfinite_or_unbounded_lease(tmp_path, seconds):
    async def run():
        queue = JobQueue(tmp_path / "bounded-extension.db", lease_s=7)
        try:
            jid = await queue.enqueue("bounded-lease", {})
            claim = await queue.claim()
            assert claim is not None
            before = time.time()
            assert await queue.extend(jid, claim["token"], seconds) is True
            row = await queue.get(jid)
            assert row is not None
            assert math.isfinite(row["lease_until"])
            remaining = row["lease_until"] - before
            assert 1 <= remaining <= 7.5
        finally:
            await queue.close()

    asyncio.run(run())


@pytest.mark.parametrize("lease_s", [
    0, -1, float("nan"), float("inf"), float("-inf"), "bad", None,
])
def test_constructor_normalizes_invalid_lease_defaults(tmp_path, lease_s):
    async def run():
        queue = JobQueue(tmp_path / "normalized-lease.db", lease_s=lease_s)
        try:
            assert queue.lease_s == 900.0
        finally:
            await queue.close()

    asyncio.run(run())
