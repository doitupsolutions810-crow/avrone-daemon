"""Round 164: JobQueue.enqueue validates kind (lone surrogate raised UnicodeEncodeError)."""
from __future__ import annotations

import asyncio

import pytest

from avrone import jobqueue as jq


@pytest.mark.parametrize("kind", ["\ud800", "run\udfff", "", "a\nb", None, "x" * 65, True, object()])
def test_bad_kind_is_value_error(tmp_path, kind):
    q = jq.JobQueue(tmp_path / "jobs.db")

    async def m():
        try:
            with pytest.raises(ValueError):
                await q.enqueue(kind, {"task": "t"})
            assert await q.stats() == {}
        finally:
            await q.close()
    asyncio.run(m())


def test_good_kind_still_works(tmp_path):
    q = jq.JobQueue(tmp_path / "jobs.db")

    async def m():
        try:
            jid = await q.enqueue("run", {"task": "t"})
            assert (await q.get(jid))["kind"] == "run"
        finally:
            await q.close()
    asyncio.run(m())


def test_oversized_payload_refused(tmp_path):
    q = jq.JobQueue(tmp_path / "jobs.db")
    cap = getattr(jq, "PAYLOAD_MAX_BYTES", None)
    if cap is None:
        pytest.fail("no PAYLOAD_MAX_BYTES: enqueue stores payloads of any size")

    async def m():
        try:
            with pytest.raises(ValueError):
                await q.enqueue("run", {"big": "x" * (cap + 1)})
            assert await q.stats() == {}
            jid = await q.enqueue("run", {"ok": "x" * 1000})
            assert (await q.get(jid))["payload"]["ok"] == "x" * 1000
        finally:
            await q.close()
    asyncio.run(m())
