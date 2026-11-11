"""Round 155: job errors are always storable, and job results and errors are redacted.

On v0.147:
- An error containing a lone surrogate (a hostile file name or model text inside an exception)
  made sqlite raise UnicodeEncodeError.
- An exception whose __str__ raised also broke fail().
- In both cases the job stayed 'running' until its lease ran out and then ran again, and a
  PermanentJobError was retried instead of going dead.
- A fleet job's result and error (served by /api/fleet/jobs and kept on disk) held secrets
  verbatim, even though the run's CodeX log had them masked.
"""
from __future__ import annotations

import asyncio

import pytest

from avrone import jobqueue as jq

PW = "hunter2pass"


class _Unprintable(Exception):
    def __str__(self):
        raise RuntimeError("boom")


@pytest.fixture
def q(tmp_path):
    return jq.JobQueue(str(tmp_path / "q.db"))


async def _claimed(q):
    await q.enqueue("run", {})
    return await q.claim()


@pytest.mark.parametrize("err", ["bad \ud800 surrogate", _Unprintable(), f"token={PW}"])
def test_fail_always_records(q, err):
    async def go():
        c = await _claimed(q)
        assert await q.fail(c["id"], err, c.get("token"), permanent=True) == "dead"
        e = (await q.get(c["id"]))["error"]
        assert PW not in e and "\ud800" not in e and e
    asyncio.run(go())


def test_result_redacted(q):
    async def go():
        c = await _claimed(q)
        assert await q.complete(c["id"], {"answer": f"curl -u a:{PW}", "n": [1, "sk-" + "a" * 20]}, c.get("token"))
        r = (await q.get(c["id"]))["result"]
        assert r["n"][0] == 1 and PW not in str(r) and "sk-aaaa" not in str(r)
        assert r["answer"].startswith("curl -u a:")
    asyncio.run(go())


def test_permanent_error_with_surrogate_goes_dead(q):
    async def handler(kind, payload):
        raise jq.PermanentJobError("bad \ud800 payload")

    async def go():
        w = jq.Worker(q, handler, 0.02)
        jid = await q.enqueue("run", {})
        w.start()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if (await q.get(jid))["status"] == "dead":
                break
        await w.stop()
        assert (await q.get(jid))["status"] == "dead"
    asyncio.run(go())


def test_exc_text_never_raises():
    f = getattr(jq, "exc_text", None)
    if f is None:
        pytest.fail("jobqueue.exc_text missing")
    assert f(_Unprintable()) == "_Unprintable: <unprintable>"
