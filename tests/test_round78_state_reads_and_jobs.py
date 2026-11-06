"""Round 78: evolve state reads and queued job payloads.

1. Evolve read proposal, loop and history files with read_text(): it follows symlinks, blocks
   forever on a FIFO and reads any size whole. reconcile_interrupted() runs at boot over
   evolve/loops/*.json, so a FIFO named x.json there hung startup, and a symlink there was read
   (and rewritten as a copy) from outside the data dir. All reads now go through _read_state:
   regular file, O_NOFOLLOW, size cap.
2. run_job trusted the payload from jobs.db (shared by every process on the data dir, and
   outliving versions): a job without "task" failed with KeyError on every attempt, a non-string
   task reached squad()/dispatch(), and an unknown kind ran as a dispatch. They now raise
   PermanentJobError, which the Worker turns into an immediate dead job.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from avrone import evolve
from avrone import jobqueue as jq
from avrone.jobqueue import JobQueue, Worker

PermanentJobError = getattr(jq, "PermanentJobError", type("PermanentJobError", (Exception,), {}))


def _in_thread(fn, timeout=5.0):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # unblock the reader so the thread can finish
        raise AssertionError("hung (blocked on a FIFO)")
    if "e" in box:
        raise box["e"]
    return box["v"]


def _loops(tmp_path):
    d = tmp_path / "evolve" / "loops"
    d.mkdir(parents=True)
    return d


def _unblock(fifo):
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


def test_reconcile_at_boot_skips_fifo(tmp_path):
    d = _loops(tmp_path)
    fifo = d / "pipe.json"
    os.mkfifo(fifo)
    settings = SimpleNamespace(data_dir=str(tmp_path))
    try:
        out = _in_thread(lambda: evolve.reconcile_interrupted(settings))
    finally:
        _unblock(fifo)
    assert out == []
    assert fifo.exists() and not fifo.is_file()  # never replaced or read


def test_reconcile_never_follows_symlink_out_of_data_dir(tmp_path):
    d = _loops(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"loop_id": "evil", "status": "running", "pid": 999999999,
                                   "iterations": []}))
    os.symlink(outside, d / "evil.json")
    marked = evolve.reconcile_interrupted(SimpleNamespace(data_dir=str(tmp_path)))
    assert "evil" not in marked
    assert json.loads(outside.read_text())["status"] == "running"
    assert (d / "evil.json").is_symlink()


def test_list_loops_and_accepted_skip_fifo_symlink_and_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve, "STATE_JSON_MAX_BYTES", 1024, raising=False)
    d = _loops(tmp_path)
    (d / "ok.json").write_text(json.dumps({"loop_id": "ok", "status": "done", "ts": 1}))
    (d / "big.json").write_text(json.dumps({"loop_id": "big", "status": "done", "pad": "x" * 4096}))
    fifo = d / "pipe.json"
    os.mkfifo(fifo)
    outside = tmp_path / "o.json"
    outside.write_text(json.dumps({"loop_id": "leak", "status": "done"}))
    os.symlink(outside, d / "leak.json")
    ev = tmp_path / "evolve"
    (ev / "p1.json").write_text(json.dumps({"pid": "p1", "passed": True, "goal": "g", "files": [], "by": "x"}))
    os.symlink(outside, ev / "p2.json")
    os.mkfifo(ev / "p3.json")
    s = SimpleNamespace(data_dir=str(tmp_path))
    try:
        loops = _in_thread(lambda: evolve.list_loops(s))
        acc = _in_thread(lambda: evolve.list_accepted(s))
    finally:
        _unblock(fifo)
        _unblock(ev / "p3.json")
    assert [m["loop_id"] for m in loops] == ["ok"]
    assert [m["pid"] for m in acc] == ["p1"]


def test_read_state_helper(tmp_path):
    rs = getattr(evolve, "_read_state", None)
    assert rs is not None
    f = tmp_path / "a.json"
    f.write_text("{}")
    assert rs(f) == b"{}"
    with pytest.raises(ValueError):
        rs(f, cap=1)
    os.symlink(f, tmp_path / "l.json")
    with pytest.raises(OSError):
        rs(tmp_path / "l.json")
    (tmp_path / "dir.json").mkdir()
    with pytest.raises((ValueError, OSError)):
        rs(tmp_path / "dir.json")


def _fleet_stub():
    from avrone.fleet import FleetManager
    f = FleetManager.__new__(FleetManager)
    f.node_id = "root"
    calls = []

    async def dispatch(*a, **k):
        calls.append(a)
        return {"answer": "ok"}

    async def send_message(*a, **k):
        calls.append(a)
        return {"delivered": True}
    f.dispatch, f.send_message = dispatch, send_message
    return f, calls


@pytest.mark.parametrize("kind,payload", [
    ("run", {}),
    ("run", {"task": 5}),
    ("run", {"task": "   "}),
    ("run", {"task": "x" * 1_000_001}),
    ("run", {"task": "ok", "node": ["a"]}),
    ("message", {"agent": "A"}),
    ("message", {"agent": 3, "message": "hi"}),
    ("evil", {"task": "ok", "node": "n1"}),
])
def test_run_job_rejects_malformed_payload_permanently(kind, payload):
    f, calls = _fleet_stub()
    with pytest.raises(PermanentJobError):
        asyncio.run(f.run_job(kind, payload))
    assert calls == []


def test_run_job_valid_payloads_still_run():
    f, calls = _fleet_stub()
    asyncio.run(f.run_job("run", {"task": "do it", "node": "n1", "mode": "squad"}))
    asyncio.run(f.run_job("message", {"agent": "A", "message": "hi", "node": "n1"}))
    assert len(calls) == 2


def test_worker_marks_permanent_failure_dead_at_once(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "jobs.db", lease_s=30)

        async def handler(kind, p):
            raise PermanentJobError("bad payload")
        w = Worker(q, handler, idle_s=0.01)
        jid = await q.enqueue("run", {"task": "x"}, max_attempts=5)
        w.start()
        for _ in range(200):
            row = q._db.execute("SELECT status, attempts, error FROM jobs WHERE id=?", (jid,)).fetchone()
            if row[0] == "dead":
                break
            await asyncio.sleep(0.01)
        await w.stop()
        await q.close()
        return row
    status, attempts, error = asyncio.run(go())
    assert status == "dead" and attempts == 1 and "bad payload" in error


def test_worker_still_retries_ordinary_errors(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "jobs.db", lease_s=30)

        async def handler(kind, p):
            raise RuntimeError("node down")
        w = Worker(q, handler, idle_s=0.01)
        jid = await q.enqueue("run", {"task": "x"}, max_attempts=5)
        w.start()
        for _ in range(200):
            row = q._db.execute("SELECT status, attempts FROM jobs WHERE id=?", (jid,)).fetchone()
            if row[1] >= 1 and row[0] != "running":
                break
            await asyncio.sleep(0.01)
        await w.stop()
        await q.close()
        return row
    status, attempts = asyncio.run(go())
    assert status == "queued" and attempts == 1
