"""Round 166: unbindable job ids/tokens (lone surrogate, non-str) name no job instead of raising."""
from __future__ import annotations

import asyncio

import pytest

from avrone import jobqueue as jq


@pytest.mark.parametrize("bad", ["\ud800", "ab\udfff", None, 7])
def test_unbindable_ids(tmp_path, bad):
    q = jq.JobQueue(tmp_path / "jobs.db")

    async def m():
        try:
            jid = await q.enqueue("run", {"task": "t"})
            job = await q.claim()
            assert await q.get(bad) is None
            assert await q.complete(bad, "r") is False
            assert await q.fail(bad, "e") == "missing"
            assert await q.extend(bad, "tok") is False
            assert await q.release(bad, "tok") is False
            # a bad token for a real job: lease simply not owned
            assert await q.extend(jid, bad) is False
            assert await q.release(jid, bad) is False
            assert (await q.get(jid))["status"] == "running"
            assert await q.complete(jid, "ok", job.get("token")) is True
        finally:
            await q.close()
    asyncio.run(m())
