"""Round 126: a job whose result cannot be recorded is not counted as a failed run.

v0.118 (probe /workspace/work/jqfull.py, RLIMIT_FSIZE): on a full disk JobQueue.complete() raised
sqlite3.OperationalError ("disk I/O error"). Worker handled that as if the handler had failed:
fail() requeued the job with the storage error as its error. Work that had succeeded ran again,
and once space was short for long enough a job went 'dead' after max_attempts successful runs.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from avrone import jobqueue
from avrone.jobqueue import JobQueue, Worker


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(jobqueue, "COMPLETE_BACKOFF_S", 0.01, raising=False)


async def _run_one(q: JobQueue, runs: list, until) -> None:
    async def handler(kind, payload):
        runs.append(payload["i"])
        return {"ok": payload["i"]}
    w = Worker(q, handler, idle_s=0.01)
    w.start()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if await until():
            break
    await w.stop()


def test_transient_complete_failure_is_retried_not_rerun(tmp_path, monkeypatch):
    async def main():
        q = JobQueue(tmp_path / "q.db")
        jid = await q.enqueue("k", {"i": 1})
        real = q.complete
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database or disk is full")
            return await real(*a, **k)
        monkeypatch.setattr(q, "complete", flaky)
        runs: list = []

        async def done():
            j = await q.get(jid)
            return j and j["status"] == "done"
        await _run_one(q, runs, done)
        j = await q.get(jid)
        await q.close()
        return runs, j
    runs, j = asyncio.run(main())
    assert j["status"] == "done", j
    assert runs == [1]  # ran once: the storage error did not requeue it
    assert not j.get("error")


def test_lasting_complete_failure_never_recorded_as_handler_error(tmp_path, monkeypatch):
    async def main():
        q = JobQueue(tmp_path / "q.db")
        jid = await q.enqueue("k", {"i": 7}, max_attempts=1)

        async def broken(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(q, "complete", broken)
        failed: list = []
        real_fail = q.fail

        async def spy(*a, **k):
            failed.append(a)
            return await real_fail(*a, **k)
        monkeypatch.setattr(q, "fail", spy)
        runs: list = []
        n = {"t": 0}

        async def gave_up():
            n["t"] += 1
            return n["t"] > 40
        await _run_one(q, runs, gave_up)
        j = await q.get(jid)
        await q.close()
        return runs, j, failed
    runs, j, failed = asyncio.run(main())
    assert runs == [7]
    assert failed == []  # the handler succeeded: no failure recorded against it
    assert j["status"] == "running"  # left to its lease, so it is reaped and run again
    assert j["status"] != "dead"


def test_handler_errors_still_fail(tmp_path):
    async def main():
        q = JobQueue(tmp_path / "q.db")
        jid = await q.enqueue("k", {"i": 1}, max_attempts=1)

        async def handler(kind, payload):
            raise ValueError("boom")
        w = Worker(q, handler, idle_s=0.01)
        w.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            j = await q.get(jid)
            if j["status"] == "dead":
                break
        await w.stop()
        j = await q.get(jid)
        await q.close()
        return j
    j = asyncio.run(main())
    assert j["status"] == "dead" and "boom" in j["error"]


def test_constants_exist():
    assert getattr(jobqueue, "COMPLETE_TRIES", 0) >= 2 or pytest.fail("no COMPLETE_TRIES")
