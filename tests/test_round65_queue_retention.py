"""Round 65: the durable job queue had no retention and no backlog cap; queue_submit raised on junk.

- Finished jobs (done/dead, each up to RESULT_MAX = 100 KB of result) were never deleted, so
  jobs.db only grew. JobQueue.prune() drops finished jobs older than AVRONE_QUEUE_RETAIN_S (7 days)
  and all but the newest AVRONE_QUEUE_KEEP_FINISHED (5000); it runs every PRUNE_EVERY enqueues.
  Unfinished jobs are never pruned.
- Unfinished jobs had no cap: enqueue() now raises QueueFull at AVRONE_QUEUE_MAX_PENDING (10000),
  checked inside the same IMMEDIATE transaction as the insert (no overshoot across processes).
- t_queue_submit did int(args["priority"]) itself: "x", NaN, "1e400" raised ValueError/OverflowError
  out of the tool, and a missing task raised KeyError. A task over 1 MB is refused before it is stored.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from avrone import fleet, jobqueue
from avrone.jobqueue import JobQueue

QueueFull = getattr(jobqueue, "QueueFull", type("QueueFull", (Exception,), {}))  # lets v0.57 collect


def run(c):
    return asyncio.run(c)


def test_pending_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_QUEUE_MAX_PENDING", "5")

    async def go():
        q = JobQueue(tmp_path / "j.db")
        for i in range(5):
            await q.enqueue("run", {"i": i})
        with pytest.raises(QueueFull):
            await q.enqueue("run", {"i": 5})
        j = await q.claim()
        await q.complete(j["id"], {"ok": 1}, j["token"])  # a finished job frees a slot
        await q.enqueue("run", {"i": 6})
        st = await q.stats()
        await q.close()
        return st
    assert run(go()) == {"queued": 5, "done": 1}


def test_pending_cap_holds_under_concurrent_enqueue(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_QUEUE_MAX_PENDING", "20")

    async def go():
        qa, qb = JobQueue(tmp_path / "j.db"), JobQueue(tmp_path / "j.db")  # two "processes", one file
        res = await asyncio.gather(*(q.enqueue("run", {}) for q in [qa, qb] * 30), return_exceptions=True)
        n = (await qa.stats()).get("queued", 0)
        await qa.close(); await qb.close()
        return res, n
    res, n = run(go())
    assert n == 20 and sum(isinstance(r, QueueFull) for r in res) == 40


def test_prune_keeps_unfinished_and_newest_finished(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "j.db")
        ids = []
        for i in range(12):
            ids.append(await q.enqueue("run", {"i": i}))
        for _ in range(8):
            j = await q.claim()
            await q.complete(j["id"], "x" * 1000, j["token"])
        gone = await q.prune(keep=3, older_than_s=10 * 86400)
        st = await q.stats()
        await q.close()
        return gone, st
    gone, st = run(go())
    assert gone == 5 and st == {"queued": 4, "done": 3}


def test_prune_by_age(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "j.db")
        for _ in range(3):
            await q.enqueue("run", {})
        j = await q.claim()
        await q.fail(j["id"], "boom", j["token"])  # requeued, not finished
        q._db.execute("UPDATE jobs SET status='dead', updated=? WHERE id=?", (time.time() - 30 * 86400, j["id"]))
        j2 = await q.claim()
        await q.complete(j2["id"], 1, j2["token"])
        gone = await q.prune()
        st = await q.stats()
        await q.close()
        return gone, st
    gone, st = run(go())
    assert gone == 1 and st == {"queued": 1, "done": 1}


def test_enqueue_prunes_periodically(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_QUEUE_KEEP_FINISHED", "2")
    monkeypatch.setattr(jobqueue, "PRUNE_EVERY", 10)

    async def go():
        q = JobQueue(tmp_path / "j.db")
        for _ in range(9):
            await q.enqueue("run", {})
        for _ in range(9):
            j = await q.claim()
            await q.complete(j["id"], 1, j["token"])
        await q.enqueue("run", {})  # the 10th enqueue prunes
        st = await q.stats()
        await q.close()
        return st
    assert run(go()) == {"done": 2, "queued": 1}


def test_enqueue_stays_fast_with_a_big_backlog(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "j.db")
        q._db.execute("BEGIN")
        q._db.executemany("INSERT INTO jobs(id,kind,payload,status,priority,max_attempts,created,updated) "
                          "VALUES(?,?,?,?,?,?,?,?)",
                          ((f"d{i}", "run", "{}", "done", 0, 3, 0.0, time.time()) for i in range(50_000)))
        q._db.execute("COMMIT")
        t = time.monotonic()
        for _ in range(50):
            await q.enqueue("run", {})
        dt = time.monotonic() - t
        await q.close()
        return dt
    assert run(go()) < 2.0


class _Q:
    def __init__(self):
        self.calls = []

    async def enqueue(self, kind, payload, priority=0, max_attempts=3):
        self.calls.append((priority, max_attempts))
        return "jid1"


def _ctx():
    f = SimpleNamespace(queue=_Q(), node_id="n1", check_outbound=lambda *a: None)
    core = SimpleNamespace(fleet=f, fleet_hops=0)
    return SimpleNamespace(core=core, name="A"), f


@pytest.mark.parametrize("prio,tries,want", [
    ("x", "y", (0, 3)), (float("nan"), float("inf"), (0, 3)), ("1e400", None, (0, 3)),
    (True, False, (0, 3)), (7, 2, (7, 2)), ("5", "4", (5, 4)), (10**30, -5, (2**62, -5)),
])
def test_queue_submit_junk_numbers(monkeypatch, prio, tries, want):
    ctx, f = _ctx()
    monkeypatch.setattr(fleet, "_fleet", lambda c: f)
    out = run(fleet.t_queue_submit(ctx, {"task": "do it", "priority": prio, "max_attempts": tries}))
    assert out.startswith("queued job jid1"), out
    assert f.queue.calls == [want]


@pytest.mark.parametrize("args", [{}, {"task": ""}, {"task": "   "}, {"task": None}, {"task": 5}])
def test_queue_submit_needs_a_task(monkeypatch, args):
    ctx, f = _ctx()
    monkeypatch.setattr(fleet, "_fleet", lambda c: f)
    assert run(fleet.t_queue_submit(ctx, args)).startswith("ERROR: task is required")
    assert f.queue.calls == []


def test_queue_submit_refuses_huge_task_and_full_queue(monkeypatch, tmp_path):
    ctx, f = _ctx()
    monkeypatch.setattr(fleet, "_fleet", lambda c: f)
    assert "over" in run(fleet.t_queue_submit(ctx, {"task": "x" * (fleet.QUEUE_TASK_MAX + 1)}))
    monkeypatch.setenv("AVRONE_QUEUE_MAX_PENDING", "1")

    async def go():
        f.queue = JobQueue(tmp_path / "j.db")
        a = await fleet.t_queue_submit(ctx, {"task": "one"})
        b = await fleet.t_queue_submit(ctx, {"task": "two"})
        await f.queue.close()
        return a, b
    a, b = run(go())
    assert a.startswith("queued job") and b.startswith("ERROR: queue full")
