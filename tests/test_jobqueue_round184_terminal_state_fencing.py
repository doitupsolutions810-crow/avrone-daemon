"""Round 184: terminal-state fencing across simultaneous queue writers."""
from __future__ import annotations

import asyncio

from avrone.jobqueue import JobQueue


def test_two_connections_cannot_both_complete_the_same_lease(tmp_path):
    async def run():
        path = tmp_path / "double-complete.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("race", {"winner": "one"})
            claim = await q1.claim()
            assert claim is not None and claim["id"] == jid

            outcomes = await asyncio.gather(
                q1.complete(jid, {"writer": "A"}, claim["token"]),
                q2.complete(jid, {"writer": "B"}, claim["token"]),
            )
            assert sorted(outcomes) == [False, True]
            row = await q1.get(jid)
            assert row is not None and row["status"] == "done"
            assert row["result"] in ({"writer": "A"}, {"writer": "B"})
            assert row["attempts"] == 1
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())


def test_complete_and_fail_race_has_exactly_one_accepted_transition(tmp_path):
    async def run():
        path = tmp_path / "complete-fail-race.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("race", {}, max_attempts=3)
            claim = await q1.claim()
            assert claim is not None

            completed, failed = await asyncio.gather(
                q1.complete(jid, {"winner": "completion"}, claim["token"]),
                q2.fail(jid, "competing failure", claim["token"]),
            )
            assert (completed, failed) in ((True, "stale"), (False, "requeued"))

            row = await q1.get(jid)
            assert row is not None
            assert row["attempts"] == 1
            if completed:
                assert row["status"] == "done"
                assert row["result"] == {"winner": "completion"}
                assert row["error"] is None
            else:
                assert row["status"] == "queued"
                assert row["result"] is None
                assert row["error"] == "competing failure"
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())


def test_tokenless_legacy_calls_cannot_mutate_terminal_rows(tmp_path):
    async def run():
        path = tmp_path / "tokenless-terminal.db"
        q1, q2 = JobQueue(path), JobQueue(path)
        try:
            jid = await q1.enqueue("legacy", {})
            claim = await q1.claim()
            assert claim is not None

            # Compatibility calls may complete a currently running job.
            assert await q1.complete(jid, {"accepted": True}) is True

            # But neither repeated completion nor tokenless failure may rewrite a terminal row.
            assert await q2.complete(jid, {"accepted": False}) is False
            assert await q2.fail(jid, "late tokenless failure") == "stale"
            assert await q2.extend(jid, "irrelevant-token") is False
            assert await q2.release(jid, "irrelevant-token") is False

            row = await q1.get(jid)
            assert row is not None
            assert row["status"] == "done"
            assert row["result"] == {"accepted": True}
            assert row["error"] is None
        finally:
            await asyncio.gather(q1.close(), q2.close())

    asyncio.run(run())


def test_tokenless_completion_cannot_complete_a_queued_job(tmp_path):
    async def run():
        q = JobQueue(tmp_path / "tokenless-queued.db")
        try:
            jid = await q.enqueue("not-claimed", {})
            assert await q.complete(jid, {"must_not": "complete"}) is False
            assert await q.fail(jid, "must not fail") == "stale"
            row = await q.get(jid)
            assert row is not None
            assert row["status"] == "queued"
            assert row["result"] is None
            assert row["error"] is None
        finally:
            await q.close()

    asyncio.run(run())
