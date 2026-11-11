"""Round 165: JobQueue rejects payloads that are not strict JSON objects."""
from __future__ import annotations

import asyncio

import pytest

from avrone.jobqueue import JobQueue


@pytest.mark.parametrize("payload", [None, [], "text", 7, True])
def test_non_object_payload_is_rejected_before_insert(tmp_path, payload):
    async def go():
        q = JobQueue(tmp_path / "jobs.db")
        try:
            with pytest.raises(ValueError, match="JSON object"):
                await q.enqueue("run", payload)
            assert await q.stats() == {}
        finally:
            await q.close()
    asyncio.run(go())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected_before_insert(tmp_path, value):
    async def go():
        q = JobQueue(tmp_path / "jobs.db")
        try:
            with pytest.raises(ValueError):
                await q.enqueue("run", {"nested": [1, {"value": value}]})
            assert await q.stats() == {}
        finally:
            await q.close()
    asyncio.run(go())


def test_valid_nested_payload_still_round_trips(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "jobs.db")
        try:
            jid = await q.enqueue("run", {"nested": [1, {"ok": True, "value": None}]})
            job = await q.claim()
            assert job is not None and job["id"] == jid
            assert job["payload"] == {"nested": [1, {"ok": True, "value": None}]}
        finally:
            await q.close()
    asyncio.run(go())


def test_extreme_nesting_is_rejected_without_insert(tmp_path):
    payload = {}
    cursor = payload
    for _ in range(1500):
        cursor["nested"] = {}
        cursor = cursor["nested"]

    async def go():
        q = JobQueue(tmp_path / "jobs.db")
        try:
            with pytest.raises(ValueError, match="nested too deeply"):
                await q.enqueue("run", payload)
            assert await q.stats() == {}
        finally:
            await q.close()
    asyncio.run(go())
