"""Round 45: the durable job queue and the CodeX action log under crashes, races and junk.

  * jobqueue: several PROCESSES claiming from one SQLite file never run a job twice; a worker whose
    lease expired cannot complete/fail/requeue a job someone else re-claimed; a poison job goes dead
    after max_attempts; hostile enqueue arguments (huge priority, NaN, junk max_attempts) and hostile
    extend() seconds never raise out of the queue or park a job forever; corrupt rows never kill claim;
  * codex: a crash mid-write (torn last line) costs only the torn event, never the next one; several
    processes appending large events never interleave bytes; replay/runs survive junk files.
"""
from __future__ import annotations

import asyncio
import json
import math
import multiprocessing as mp
import os
import random
import sqlite3
import time
from pathlib import Path

import pytest

from avrone.codex import Codex
from avrone.jobqueue import JobQueue

FORK = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ jobqueue: processes
def _proc_drain(path: str, q) -> None:
    async def go():
        jq = JobQueue(Path(path), lease_s=60)
        got = []
        while True:
            j = await jq.claim()
            if j is None:
                break
            got.append(j["id"])
            await jq.complete(j["id"], {"ok": True}, token=j["token"])
        await jq.close()
        return got
    q.put(asyncio.run(go()))


@FORK
def test_processes_never_run_a_job_twice(tmp_path):
    path = tmp_path / "q.db"

    async def fill():
        jq = JobQueue(path)
        ids = [await jq.enqueue("t", {"i": i}) for i in range(150)]
        await jq.close()
        return ids
    ids = run(fill())
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    ps = [ctx.Process(target=_proc_drain, args=(str(path), q)) for _ in range(4)]
    for p in ps:
        p.start()
    got = [q.get(timeout=120) for _ in ps]
    for p in ps:
        p.join(30)
    flat = [j for g in got for j in g]
    assert len(flat) == len(set(flat)) == 150, (len(flat), len(set(flat)))
    assert set(flat) == set(ids)


def test_expired_lease_owner_is_fenced(tmp_path):
    async def go():
        jq = JobQueue(tmp_path / "q.db", lease_s=1)
        jid = await jq.enqueue("t", {})
        a = await jq.claim()
        jq._db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, jid))
        b = await jq.claim()          # lease ran out: someone else takes it
        assert b and b["id"] == jid and b["token"] != a["token"]
        assert await jq.complete(jid, "late", token=a["token"]) is False
        assert await jq.fail(jid, "late", token=a["token"]) == "stale"
        assert await jq.extend(jid, a["token"]) is False
        assert await jq.release(jid, a["token"]) is False
        assert await jq.complete(jid, "mine", token=b["token"]) is True
        assert (await jq.get(jid))["result"] == "mine"
        await jq.close()
    run(go())


def test_poison_job_goes_dead(tmp_path):
    async def go():
        jq = JobQueue(tmp_path / "q.db", lease_s=1)
        jid = await jq.enqueue("t", {}, max_attempts=2)
        for _ in range(2):
            j = await jq.claim()
            assert j and j["id"] == jid
            jq._db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, jid))  # worker crashed
        assert await jq.claim() is None
        g = await jq.get(jid)
        assert g["status"] == "dead" and "lease expired" in g["error"]
        await jq.close()
    run(go())


@pytest.mark.parametrize("prio", [10**30, -10**30, 2**63, float("nan"), float("inf"), "high", None, True, "7"])
@pytest.mark.parametrize("tries", [10**30, -5, 0, float("nan"), "x", None])
def test_hostile_enqueue_arguments(tmp_path, prio, tries):
    async def go():
        jq = JobQueue(tmp_path / f"q{abs(hash((repr(prio), repr(tries))))}.db")
        jid = await jq.enqueue("t", {"a": 1}, priority=prio, max_attempts=tries)
        j = await jq.claim()
        assert j and j["id"] == jid
        g = await jq.get(jid)
        assert 1 <= g["max_attempts"] <= 1000
        await jq.close()
    run(go())


@pytest.mark.parametrize("secs", [float("inf"), float("nan"), 1e300, -1e300, "1e400", "abc", None, 10**400])
def test_hostile_extend_never_parks_a_job_forever(tmp_path, secs):
    async def go():
        jq = JobQueue(tmp_path / "q.db", lease_s=60)
        await jq.enqueue("t", {})
        j = await jq.claim()
        try:
            await jq.extend(j["id"], j["token"], secs)
        except (TypeError, ValueError, OverflowError):
            pass  # refusing junk is fine
        until = jq._db.execute("SELECT lease_until FROM jobs WHERE id=?", (j["id"],)).fetchone()[0]
        assert math.isfinite(until) and until <= time.time() + 7 * 86400 + 5, until
        await jq.close()
    run(go())


def test_corrupt_rows_never_kill_claim(tmp_path):
    async def go():
        jq = JobQueue(tmp_path / "q.db")
        good = await jq.enqueue("t", {"ok": 1})
        now = time.time()
        for i, payload in enumerate(["{nope", "\x00", "[" * 5000, None, "1e999"]):
            jq._db.execute("INSERT INTO jobs(id,kind,payload,status,priority,created,updated) VALUES(?,?,?,?,?,?,?)",
                           (f"bad{i}", "t", payload, "queued", 5, now, now))
        seen = []
        while (j := await jq.claim()) is not None:
            seen.append(j["id"])
            await jq.complete(j["id"], None, token=j["token"])
        assert good in seen
        st = await jq.stats()
        assert st.get("dead", 0) + st.get("done", 0) == 6
        await jq.close()
    run(go())


# ------------------------------------------------------------------ codex
def test_torn_tail_costs_only_the_torn_event(tmp_path):
    cx = Codex(tmp_path)
    cx.emit("r1", "start", "lead", prompt="p")
    cx.emit("r1", "step", "lead", n=1)
    p = next((tmp_path).rglob("*.jsonl"))
    with open(p, "ab") as f:
        f.write(b'{"run_id":"r1","kind":"step","ag')  # the process died here
    cx2 = Codex(tmp_path)                                # restart
    cx2.emit("r1", "step", "lead", n=2)
    cx2.emit("r1", "end", "lead", n=3)
    kinds = [(e["kind"], (e.get("data") or {}).get("n")) for e in cx2.replay("r1")]
    assert ("step", 2) in kinds and ("end", 3) in kinds, kinds


def _proc_emit(data_dir: str, wid: int, n: int, size: int) -> None:
    cx = Codex(Path(data_dir))
    for i in range(n):
        cx.emit("shared", "blob", f"w{wid}", i=i, pad=chr(65 + wid) * size)


@FORK
@pytest.mark.parametrize("size", [100, 20_000, 200_000])
def test_processes_appending_never_interleave(tmp_path, size):
    ctx = mp.get_context("fork")
    n = 30 if size < 100_000 else 8
    ps = [ctx.Process(target=_proc_emit, args=(str(tmp_path), w, n, size)) for w in range(4)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(120)
        assert p.exitcode == 0
    p = next(tmp_path.rglob("shared*.jsonl"))
    lines = p.read_bytes().split(b"\n")
    good = 0
    for ln in lines:
        if not ln:
            continue
        ev = json.loads(ln)  # every line is one whole event
        pad = ev["data"]["pad"]
        assert pad == pad[0] * len(pad) and len(pad) == size
        good += 1
    assert good == 4 * n


def test_replay_and_runs_survive_junk_logs(tmp_path):
    cx = Codex(tmp_path)
    cx.emit("ok", "start", "lead", prompt="hi")
    d = next(tmp_path.rglob("ok*.jsonl")).parent
    (d / "binary.jsonl").write_bytes(os.urandom(4096))
    (d / "huge.jsonl").write_bytes(b"x" * (3 << 20))
    (d / "deep.jsonl").write_text("[" * 100000 + "\n")
    (d / "list.jsonl").write_text("[1,2]\n{\"kind\":\"x\"}\n")
    (d / "dir.jsonl").mkdir()
    os.symlink("/etc/passwd", d / "link.jsonl")
    runs = cx.runs(limit=100)
    assert any(r["run_id"] == "ok" for r in runs)
    for rid in ["binary", "huge", "deep", "list", "dir", "link", "../../etc/passwd", "\x00", "ok"]:
        out = cx.replay(rid)
        assert isinstance(out, list)
    assert cx.replay("link") == [] or all(isinstance(e, dict) for e in cx.replay("link"))


@pytest.mark.parametrize("seed", [45, 4545])
def test_emit_hostile_payloads_always_write_one_json_line(tmp_path, seed):
    r = random.Random(seed)
    cx = Codex(tmp_path)

    class Weird:
        def __repr__(self):
            raise RuntimeError("repr bomb")

    cyc: list = []
    cyc.append(cyc)
    vals = [float("nan"), float("inf"), 10**5000 if False else 10**300, b"\xff\xfe", "\ud800", "\x00",
            {1: 2, (1, 2): 3}, cyc, Weird(), set([1, 2]), object(), "x" * 100_000]
    for i in range(200):
        kw = {f"k{j}": r.choice(vals) for j in range(r.randint(0, 4))}
        try:
            cx.emit("hostile", "ev", "a\nb", **kw)
        except Exception as e:  # pragma: no cover
            pytest.fail(f"emit raised {e!r} for {list(kw)}")
    p = next(tmp_path.rglob("hostile*.jsonl"))
    for ln in p.read_bytes().split(b"\n"):
        if ln:
            json.loads(ln)


# ------------------------------------------------------------------ HTTP API: hostile bodies
_BODIES = [b"", b"{", b"null", b"[]", b"[1,2,3]", b'"str"', b"1e999", b"NaN", b'{"a":NaN}',
           b"\xff\xfe\x00junk", b"[" * 50000 + b"]" * 50000, b'{"' + b"9" * 5000 + b'":1}',
           b'{"task":' + b"9" * 6000 + b"}", b'{"task":null,"mode":{"x":1}}', b'{"task":"\\ud800"}',
           b'{"task":"' + b"x" * 300_000 + b'"}', b'{"task":"hi","squad_size":-1,"rounds":1e308}',
           b'{"task":"hi","checkpoint_id":"../../etc/passwd"}', b'{"task":["a"],"agents":"x"}']


@pytest.mark.parametrize("path", ["/api/chat", "/api/fleet/register", "/api/fleet/message", "/api/fleet/tell",
                                  "/api/fleet/memory", "/api/fleet/agentpack", "/api/fleet/run", "/api/terminal"])
def test_post_endpoints_never_500_on_hostile_bodies(path):
    from fastapi.testclient import TestClient
    from avrone.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        for hdrs in ({"content-type": "application/json"},
                     {"content-type": "application/json", "x-admin-token": "test-token"},
                     {"content-type": "text/plain; charset=utf-16"}):
            for body in _BODIES:
                r = c.post(path, content=body, headers=hdrs)
                assert r.status_code < 500, (path, body[:60], r.status_code, r.text[:200])


@pytest.mark.parametrize("body", [b"1e999", b"NaN", b'{"task":"\\ud800"}', b'{"cmd":"\\udfff","x":-Infinity}'])
def test_validation_errors_are_strict_json_422(body):
    from fastapi.testclient import TestClient
    from avrone.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/terminal", content=body, headers={"content-type": "application/json",
                                                           "x-admin-token": "test-token"})
        assert r.status_code == 422, (r.status_code, r.text[:200])
        json.loads(r.content.decode("ascii"))


def test_terminal_nul_command_is_a_result_not_500():
    from fastapi.testclient import TestClient
    from avrone.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/terminal", json={"cmd": "echo a\x00b"}, headers={"x-admin-token": "test-token"})
        assert r.status_code == 200 and r.json()["exit_code"] == -1 and "NUL" in r.json()["stderr"]
