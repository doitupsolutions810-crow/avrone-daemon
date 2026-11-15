"""Round 185: lease extension, reclamation, and completion races across connections."""
from __future__ import annotations

import asyncio
import math
import time

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
            assert 1 <= remaining <= (7 * 86400) + 1
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


def test_dead_job_rejects_every_late_owner_mutation(tmp_path):
    async def run():
        queue = JobQueue(tmp_path / "terminal-fence.db")
        try:
            jid = await queue.enqueue("terminal-fence", {}, max_attempts=1)
            claim = await queue.claim()
            assert claim is not None
            assert await queue.fail(jid, "permanent terminal failure", claim["token"]) == "dead"

            before = await queue.get(jid)
            assert before is not None and before["status"] == "dead"
            assert await queue.complete(jid, {"late": True}, claim["token"]) is False
            assert await queue.fail(jid, "late second failure", claim["token"]) == "stale"
            assert await queue.extend(jid, claim["token"], 60) is False
            assert await queue.release(jid, claim["token"]) is False
            assert await queue.complete(jid, {"tokenless late": True}) is False

            after = await queue.get(jid)
            assert after is not None
            assert after["status"] == "dead"
            assert after["error"] == before["error"]
            assert after["result"] is None
            assert after["attempts"] == 1
        finally:
            await queue.close()

    asyncio.run(run())


def test_priority_order_is_descending_and_stable_for_ties(tmp_path):
    async def run():
        queue = JobQueue(tmp_path / "priority-order.db")
        try:
            low = await queue.enqueue("priority", {"name": "low"}, priority=-10)
            high_first = await queue.enqueue("priority", {"name": "high-first"}, priority=50)
            high_second = await queue.enqueue("priority", {"name": "high-second"}, priority=50)
            extreme = await queue.enqueue("priority", {"name": "extreme"}, priority=10 ** 100)
            negative_extreme = await queue.enqueue("priority", {"name": "negative"}, priority=-(10 ** 100))
            # Fix creation timestamps explicitly so equal-priority ordering is deterministic.
            queue._db.execute("UPDATE jobs SET created=100 WHERE id=?", (high_first,))
            queue._db.execute("UPDATE jobs SET created=200 WHERE id=?", (high_second,))

            claimed = [await queue.claim() for _ in range(5)]
            assert [job["id"] for job in claimed] == [
                extreme, high_first, high_second, low, negative_extreme
            ]
            assert [job["payload"]["name"] for job in claimed] == [
                "extreme", "high-first", "high-second", "low", "negative"
            ]
        finally:
            await queue.close()

    asyncio.run(run())


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), float("-inf"),
    {"nested": [float("nan")]}, {"nested": {"value": float("inf")}},
])
def test_non_json_payload_numbers_are_rejected_without_partial_enqueue(tmp_path, bad):
    async def run():
        queue = JobQueue(tmp_path / "strict-payload.db")
        try:
            with pytest.raises(ValueError):
                await queue.enqueue("strict-json", {"bad": bad})
            assert await queue.stats() == {}
        finally:
            await queue.close()

    asyncio.run(run())


def test_prune_never_deletes_queued_or_running_jobs(tmp_path):
    async def run():
        queue = JobQueue(tmp_path / "prune-active.db")
        try:
            running_id = await queue.enqueue("retention", {"state": "running"}, priority=30)
            queued_id = await queue.enqueue("retention", {"state": "queued"}, priority=20)
            done_id = await queue.enqueue("retention", {"state": "done"}, priority=10)
            dead_id = await queue.enqueue("retention", {"state": "dead"}, priority=0)

            running_claim = await queue.claim()
            assert running_claim is not None and running_claim["id"] == running_id
            now = time.time()
            queue._db.execute(
                "UPDATE jobs SET status='done', result='true', updated=? WHERE id=?",
                (now, done_id),
            )
            queue._db.execute(
                "UPDATE jobs SET status='dead', error='terminal', updated=? WHERE id=?",
                (now, dead_id),
            )

            assert await queue.prune(keep=0, older_than_s=0) == 2
            active = await queue.get(running_id)
            pending = await queue.get(queued_id)
            assert active is not None and active["status"] == "running"
            assert active["attempts"] == 1
            assert pending is not None and pending["status"] == "queued"
            assert await queue.get(done_id) is None
            assert await queue.get(dead_id) is None
            assert await queue.stats() == {"queued": 1, "running": 1}
        finally:
            await queue.close()

    asyncio.run(run())


@pytest.mark.parametrize("payload_json", ["[]", "null", "42", '"text"'])
def test_valid_non_object_database_payload_is_quarantined_and_claim_continues(tmp_path, payload_json):
    async def run():
        queue = JobQueue(tmp_path / "non-object-payload.db")
        try:
            poison = await queue.enqueue("payload", {}, priority=10)
            good = await queue.enqueue("payload", {"safe": True}, priority=0)
            queue._db.execute("UPDATE jobs SET payload=? WHERE id=?", (payload_json, poison))

            claim = await queue.claim()
            assert claim is not None and claim["id"] == good
            assert claim["payload"] == {"safe": True}
            row = await queue.get(poison)
            assert row is not None and row["status"] == "dead"
            assert "corrupt payload: not an object" in row["error"]
        finally:
            await queue.close()

    asyncio.run(run())


def test_sqlite_statement_failure_rolls_back_transaction_before_connection_reuse(tmp_path):
    async def run():
        import sqlite3

        queue = JobQueue(tmp_path / "rollback-reuse.db")
        try:
            queue._db.execute("""
                CREATE TRIGGER reject_synthetic_job
                BEFORE INSERT ON jobs
                WHEN NEW.kind='explode'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic insert failure');
                END;
            """)
            with pytest.raises(sqlite3.IntegrityError, match="synthetic insert failure"):
                await queue.enqueue("explode", {})
            assert queue._db.in_transaction is False
            assert await queue.stats() == {}

            queue._db.execute("DROP TRIGGER reject_synthetic_job")
            jid = await queue.enqueue("normal", {"after_rollback": True})
            claim = await queue.claim()
            assert claim is not None and claim["id"] == jid
            assert claim["payload"] == {"after_rollback": True}
            assert await queue.complete(jid, {"ok": True}, claim["token"]) is True
        finally:
            await queue.close()

    asyncio.run(run())


def test_retry_backoff_grows_exponentially_and_caps_at_sixty_seconds(tmp_path):
    async def run():
        queue = JobQueue(tmp_path / "retry-backoff.db")
        try:
            jid = await queue.enqueue("backoff", {}, max_attempts=8)
            for attempt in range(1, 8):
                claim = await queue.claim()
                assert claim is not None and claim["id"] == jid
                assert claim["attempts"] == attempt
                before = time.time()
                assert await queue.fail(jid, f"attempt {attempt}", claim["token"]) == "requeued"
                lease_until = queue._db.execute(
                    "SELECT lease_until FROM jobs WHERE id=?", (jid,)
                ).fetchone()[0]
                expected = min(2 ** attempt, 60)
                delay = lease_until - before
                assert expected - 0.2 <= delay <= expected + 0.5
                queue._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))

            final_claim = await queue.claim()
            assert final_claim is not None and final_claim["attempts"] == 8
            assert await queue.fail(jid, "attempt limit reached", final_claim["token"]) == "dead"
            final = await queue.get(jid)
            assert final is not None and final["status"] == "dead"
            assert final["attempts"] == 8
        finally:
            await queue.close()

    asyncio.run(run())
