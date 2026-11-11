"""Round 182: cross-connection claim uniqueness and prune isolation.

Uses independent JobQueue instances over one SQLite file to exercise the same
transaction boundary used by separate worker processes.
"""
from __future__ import annotations

import asyncio

from avrone.jobqueue import JobQueue


def test_many_independent_connections_cannot_claim_one_job_twice(tmp_path):
    async def run():
        path = tmp_path / "shared-queue.db"
        queues = [JobQueue(path) for _ in range(24)]
        try:
            owner = queues[0]
            jid = await owner.enqueue("single", {"must_have_one_owner": True})
            claims = await asyncio.gather(*(q.claim() for q in queues))
            winners = [claim for claim in claims if claim is not None]
            assert len(winners) == 1
            assert winners[0]["id"] == jid
            assert winners[0]["payload"] == {"must_have_one_owner": True}
            assert len({claim["token"] for claim in winners}) == 1
            row = await owner.get(jid)
            assert row is not None and row["status"] == "running"
            assert row["attempts"] == 1
        finally:
            await asyncio.gather(*(q.close() for q in queues))

    asyncio.run(run())


def test_prune_never_deletes_queued_or_running_jobs_during_concurrent_claims(tmp_path):
    async def run():
        path = tmp_path / "prune-race.db"
        queues = [JobQueue(path) for _ in range(8)]
        try:
            q = queues[0]
            running_id = await q.enqueue("running", {"keep": "running"})
            finished_id = await q.enqueue("finished", {"eligible": "prune"})
            queued_id = await q.enqueue("queued", {"keep": "queued"})
            running = await q.claim()
            assert running is not None and running["id"] == running_id
            finished = await q.claim()
            assert finished is not None and finished["id"] == finished_id
            assert await q.complete(finished_id, {"done": True}, finished["token"])

            # Race pruning from several independent connections. keep=0 and age=0
            # makes all finished rows eligible while unfinished rows remain protected.
            removed = await asyncio.gather(
                *(other.prune(keep=0, older_than_s=0) for other in queues)
            )
            assert sum(removed) >= 1
            assert await q.get(running_id) is not None
            assert await q.get(queued_id) is not None
            assert await q.get(finished_id) is None
            stats = await q.stats()
            assert stats.get("running") == 1
            assert stats.get("queued") == 1
            assert "done" not in stats
        finally:
            await asyncio.gather(*(q.close() for q in queues))

    asyncio.run(run())
