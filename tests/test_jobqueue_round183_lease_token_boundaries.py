"""Round 183: lease-token boundary and replay invariants.

Tokenless completion is a documented compatibility path (covered by the legacy
suite); supplied tokens must never degrade into that path when malformed.
"""
from __future__ import annotations

import asyncio

import pytest

from avrone.jobqueue import JobQueue


@pytest.mark.parametrize(
    ("bad_token", "expected_fail"),
    [("", "stale"), ("wrong-token", "stale"), (7, "missing"), ("\ud800", "missing")],
)
def test_invalid_supplied_token_cannot_mutate_a_running_job(tmp_path, bad_token, expected_fail):
    async def run():
        q = JobQueue(tmp_path / "invalid-token.db")
        try:
            jid = await q.enqueue("lease", {"stable": True})
            claimed = await q.claim()
            assert claimed is not None and claimed["id"] == jid

            assert await q.complete(jid, {"attacker": "complete"}, bad_token) is False
            assert await q.fail(jid, "attacker failure", bad_token) == expected_fail
            assert await q.extend(jid, bad_token) is False
            assert await q.release(jid, bad_token) is False

            row = await q.get(jid)
            assert row is not None
            assert row["status"] == "running"
            assert row["attempts"] == 1
            assert row["result"] is None
            assert row["error"] is None
            return row
        finally:
            await q.close()

    row = asyncio.run(run())
    assert row["payload"] == {"stable": True}


def test_current_token_cannot_replay_completion_after_success(tmp_path):
    async def run():
        q = JobQueue(tmp_path / "completion-replay.db")
        try:
            jid = await q.enqueue("lease", {})
            claimed = await q.claim()
            assert claimed is not None
            assert await q.complete(jid, {"winner": 1}, claimed["token"]) is True

            assert await q.complete(jid, {"winner": 2}, claimed["token"]) is False
            assert await q.fail(jid, "late failure", claimed["token"]) == "stale"
            assert await q.extend(jid, claimed["token"]) is False
            assert await q.release(jid, claimed["token"]) is False

            row = await q.get(jid)
            assert row is not None
            assert row["status"] == "done"
            assert row["result"] == {"winner": 1}
            assert row["error"] is None
        finally:
            await q.close()

    asyncio.run(run())


def test_tokenless_compatibility_path_is_explicit_and_bounded_by_job_state(tmp_path):
    async def run():
        q = JobQueue(tmp_path / "legacy-tokenless.db")
        try:
            jid = await q.enqueue("legacy", {})
            claimed = await q.claim()
            assert claimed is not None
            # Legacy API callers intentionally omit token; retain this contract.
            assert await q.complete(jid, {"legacy": True}) is True
            # Even compatibility calls cannot change a terminal row again.
            assert await q.complete(jid, {"legacy": False}) is False
            row = await q.get(jid)
            assert row is not None and row["status"] == "done"
            assert row["result"] == {"legacy": True}
        finally:
            await q.close()

    asyncio.run(run())
