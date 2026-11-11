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



def test_payload_depth_limit_is_explicit_and_cross_version(tmp_path):
    async def enqueue_depth(depth):
        payload = {}
        cursor = payload
        for _ in range(depth):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        q = JobQueue(tmp_path / f"depth-{depth}.db")
        try:
            return await q.enqueue("depth", payload)
        finally:
            await q.close()

    async def run():
        await enqueue_depth(64)
        with pytest.raises(ValueError, match="nested too deeply"):
            await enqueue_depth(65)

    asyncio.run(run())


def test_circular_payload_is_rejected_without_poisoning_queue(tmp_path):
    async def run():
        q = JobQueue(tmp_path / "circular.db")
        try:
            payload = {}
            payload["self"] = payload
            with pytest.raises(ValueError, match="Circular reference"):
                await q.enqueue("cycle", payload)
            assert await q.stats() == {}
            jid = await q.enqueue("after-cycle", {"valid": True})
            assert await q.get(jid) is not None
        finally:
            await q.close()

    asyncio.run(run())


def test_shared_nested_object_is_checked_at_each_depth(tmp_path):
    async def run():
        q = JobQueue(tmp_path / "shared-depth.db")
        try:
            shared = {"leaf": True}
            shallow = {"shared": shared}
            deep = {}
            cursor = deep
            for _ in range(64):
                cursor["next"] = {}
                cursor = cursor["next"]
            cursor["shared"] = shared
            with pytest.raises(ValueError, match="nested too deeply"):
                await q.enqueue("shared-depth", {"shallow": shallow, "deep": deep})
            assert await q.stats() == {}
        finally:
            await q.close()

    asyncio.run(run())
