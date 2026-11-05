"""Durable job queue (SQLite, WAL): work survives a restart and retries on failure.

A single-file queue so AVRONE needs no external broker to start. Jobs are claimed
atomically, run, then marked done or retried with backoff; a crash leaves a job
'running' and reap() returns it to the queue after a lease timeout.
For a large fleet, swap this for NATS JetStream or Redis Streams behind the same API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("avrone.jobqueue")

RESULT_MAX = 100000
ERROR_MAX = 4000
PRIORITY_MAX = 2**62
EXTEND_MAX_S = 7 * 86400  # one heartbeat can never park a job for longer than this
# Retention (round 65). Finished jobs (done/dead, each up to RESULT_MAX of result) were never
# deleted: the file only grew. Unfinished jobs had no cap: an agent in a loop could queue without
# bound. Both are env-tunable; prune runs every PRUNE_EVERY enqueues and on demand.
DEFAULT_MAX_PENDING, HARD_MAX_PENDING = 10_000, 1_000_000
DEFAULT_KEEP_FINISHED, HARD_KEEP_FINISHED = 5_000, 1_000_000
DEFAULT_RETAIN_S = 7 * 86400
PRUNE_EVERY = 100


class QueueFull(Exception):
    """enqueue() refused: AVRONE_QUEUE_MAX_PENDING unfinished jobs already waiting."""


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    import os
    try:
        v = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, v))


def max_pending() -> int:
    return _env_int("AVRONE_QUEUE_MAX_PENDING", DEFAULT_MAX_PENDING, 1, HARD_MAX_PENDING)


def keep_finished() -> int:
    return _env_int("AVRONE_QUEUE_KEEP_FINISHED", DEFAULT_KEEP_FINISHED, 0, HARD_KEEP_FINISHED)


def retain_s() -> int:
    return _env_int("AVRONE_QUEUE_RETAIN_S", DEFAULT_RETAIN_S, 0, 10 * 365 * 86400)


def _encode_result(result: Any) -> str:
    """Always valid JSON. The old `json.dumps(result)[:100000]` raised TypeError on a non-JSON
    result (the finished job was then failed and RE-RUN) and cut long results mid-token, so get()
    crashed on the stored text forever after."""
    try:
        text = json.dumps(result, default=str, allow_nan=False)
    except (TypeError, ValueError):
        text = json.dumps(repr(result))
    if len(text) > RESULT_MAX:
        text = json.dumps({"truncated": True, "bytes": len(text), "head": text[:RESULT_MAX - 200]})
    return text


def _decode(text: Any) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"corrupt": True, "raw": str(text)[:2000]}


def _err_text(error: Any) -> str:
    return (error if isinstance(error, str) else str(error))[:ERROR_MAX]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, priority INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
  created REAL, updated REAL, lease_until REAL DEFAULT 0, result TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, priority DESC, created);
CREATE INDEX IF NOT EXISTS jobs_finished ON jobs(status, updated);
"""


class JobQueue:
    def __init__(self, path: Path, lease_s: float = 900):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            lease = float(lease_s)
        except (TypeError, ValueError):
            lease = 900.0
        # a zero, negative or NaN lease made every running job instantly reclaimable (double runs)
        self.lease_s = lease if math.isfinite(lease) and lease >= 1 else 900.0
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        # Several JobQueue instances (processes) may share one file: wait for the write lock
        # instead of failing with "database is locked".
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(jobs)").fetchall()}
        if "token" not in cols:  # migrate a pre-round-29 file in place
            try:
                self._db.execute("ALTER TABLE jobs ADD COLUMN token TEXT")
            except sqlite3.OperationalError:  # another process added it first
                pass
        self._lock = asyncio.Lock()
        # A cancelled await releases the asyncio lock while its worker thread is still
        # inside SQLite; the thread lock keeps close() (and every other call) from
        # touching the connection until that thread is done. Without it, shutting a
        # Worker down mid-claim could segfault the process.
        self._tlock = threading.Lock()
        self._closed = False
        self._enqueued = 0

    def _txn(self, fn: Callable[[], Any]) -> Any:
        """Run fn inside BEGIN IMMEDIATE ... COMMIT: the write lock is taken BEFORE the read, so a
        read-then-write (claim's SELECT+UPDATE, fail's attempts check+UPDATE) is atomic across every
        connection on the file, not just within this instance's asyncio lock. Without it two
        queues on one file (two worker processes) both SELECTed the same queued row and ran the
        job twice."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            out = fn()
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return out

    async def _run(self, fn: Callable[[], Any]) -> Any:
        def guarded() -> Any:
            with self._tlock:
                if self._closed:
                    raise sqlite3.ProgrammingError("job queue is closed")
                return fn()
        async with self._lock:
            return await asyncio.to_thread(guarded)

    async def enqueue(self, kind: str, payload: dict[str, Any], priority: int = 0, max_attempts: int = 3) -> str:
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        body = json.dumps(payload)  # an unserializable payload fails HERE, to the caller, not in a worker
        try:
            prio = int(priority)
        except (TypeError, ValueError, OverflowError):
            prio = 0
        # SQLite INTEGER is 64-bit: a priority like 10**30 used to raise OverflowError out of enqueue()
        prio = max(-PRIORITY_MAX, min(PRIORITY_MAX, prio))
        try:
            tries = max(1, min(int(max_attempts), 1000))
        except (TypeError, ValueError, OverflowError):
            tries = 3
        cap = max_pending()

        def _ins() -> None:
            n = self._db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if n >= cap:
                raise QueueFull(f"queue full: {n} unfinished jobs (AVRONE_QUEUE_MAX_PENDING={cap})")
            self._db.execute(
                "INSERT INTO jobs(id,kind,payload,status,priority,max_attempts,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (jid, str(kind), body, "queued", prio, tries, now, now))
        await self._run(lambda: self._txn(_ins))
        self._enqueued += 1
        if self._enqueued % PRUNE_EVERY == 0:
            try:
                await self.prune()
            except Exception:  # noqa: BLE001  (retention is housekeeping; never fail the enqueue)
                log.warning("job queue prune failed", exc_info=True)
        return jid

    async def prune(self, keep: int | None = None, older_than_s: float | None = None) -> int:
        """Delete finished (done/dead) jobs older than the retention window, then all but the newest
        `keep` finished ones. Unfinished jobs are never touched. Returns how many rows went."""
        k = keep_finished() if keep is None else max(0, int(keep))
        age = retain_s() if older_than_s is None else max(0.0, float(older_than_s))

        def _p() -> int:
            cut = time.time() - age
            n = self._db.execute("DELETE FROM jobs WHERE status IN ('done','dead') AND updated<?", (cut,)).rowcount
            n += self._db.execute(
                "DELETE FROM jobs WHERE status IN ('done','dead') AND id NOT IN "
                "(SELECT id FROM jobs WHERE status IN ('done','dead') ORDER BY updated DESC LIMIT ?)", (k,)).rowcount
            return n
        return await self._run(lambda: self._txn(_p))

    async def claim(self) -> dict[str, Any] | None:
        def _claim() -> Any:
            now = time.time()
            # A job whose lease ran out after its LAST attempt crashed its worker every time
            # (a poison job): it used to be re-claimed forever. It goes dead instead.
            self._db.execute(
                "UPDATE jobs SET status='dead', updated=?, error=COALESCE(error || ' | ', '') || "
                "'lease expired on the last attempt (worker crashed or hung)' "
                "WHERE status='running' AND lease_until<? AND attempts>=max_attempts", (now, now))
            while True:
                cur = self._db.execute(
                    "SELECT id,kind,payload,attempts FROM jobs WHERE (status='queued' AND lease_until<=?) "
                    "OR (status='running' AND lease_until<?) ORDER BY priority DESC, created LIMIT 1", (now, now))
                row = cur.fetchone()
                if not row:
                    return None
                try:
                    payload = json.loads(row[2]) if row[2] is not None else {}
                except (TypeError, ValueError) as e:
                    # used to raise out of claim() after COMMIT and kill the Worker loop for good
                    self._db.execute("UPDATE jobs SET status='dead', error=?, updated=? WHERE id=?",
                                     (_err_text(f"corrupt payload: {e}"), now, row[0]))
                    continue
                token = uuid.uuid4().hex
                self._db.execute("UPDATE jobs SET status='running', attempts=attempts+1, lease_until=?, updated=?, "
                                 "token=? WHERE id=?", (now + self.lease_s, now, token, row[0]))
                return row, payload, token
        got = await self._run(lambda: self._txn(_claim))
        if got is None:
            return None
        row, payload, token = got
        return {"id": row[0], "kind": row[1], "payload": payload, "attempts": row[3] + 1, "token": token}

    @staticmethod
    def _owner(token: str | None) -> tuple[str, tuple]:
        """Fence for writes from a claimer: a worker whose lease ran out (and whose job another
        worker re-claimed) must not mark that job done, requeue it, or overwrite its result."""
        if token is None:
            return "", ()
        return " AND status='running' AND token=?", (token,)

    async def complete(self, jid: str, result: Any, token: str | None = None) -> bool:
        """True when the result was recorded; False when the lease was lost (someone else owns it)."""
        text = _encode_result(result)
        clause, extra = self._owner(token)
        cur = await self._run(lambda: self._db.execute(
            f"UPDATE jobs SET status='done', result=?, updated=?, lease_until=0 WHERE id=?{clause}",
            (text, time.time(), jid, *extra)))
        return cur.rowcount > 0

    async def fail(self, jid: str, error: Any, token: str | None = None) -> str:
        """'dead', 'requeued', 'missing' (no such job) or 'stale' (the lease was lost)."""
        err = _err_text(error)
        clause, extra = self._owner(token)

        def _fail() -> str:
            now = time.time()
            r = self._db.execute(f"SELECT attempts,max_attempts FROM jobs WHERE id=?{clause}", (jid, *extra)).fetchone()
            if r is None:
                exists = self._db.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone()
                return "stale" if exists else "missing"
            if r[0] >= r[1]:
                self._db.execute("UPDATE jobs SET status='dead', error=?, updated=? WHERE id=?", (err, now, jid))
                return "dead"
            backoff = min(2 ** max(1, int(r[0])), 60)
            self._db.execute("UPDATE jobs SET status='queued', error=?, lease_until=?, updated=? WHERE id=?",
                             (err, now + backoff, now, jid))
            return "requeued"
        return await self._run(lambda: self._txn(_fail))

    async def extend(self, jid: str, token: str, seconds: float | None = None) -> bool:
        """Heartbeat: push a running job's lease out. False when the lease is already lost."""
        # seconds=inf (or "1e400", 1e300) used to write an infinite lease: the job could never be
        # reaped again if its worker then died. NaN/junk now means "one normal lease".
        try:
            secs = self.lease_s if seconds is None else float(seconds)
        except (TypeError, ValueError, OverflowError):
            secs = self.lease_s
        if not math.isfinite(secs):
            secs = self.lease_s
        until = time.time() + max(1.0, min(EXTEND_MAX_S, secs))
        cur = await self._run(lambda: self._db.execute(
            "UPDATE jobs SET lease_until=?, updated=? WHERE id=? AND status='running' AND token=?",
            (until, time.time(), jid, token)))
        return cur.rowcount > 0

    async def release(self, jid: str, token: str) -> bool:
        """Give a claimed job back untouched (worker shutting down): queued again right away and
        the attempt is not counted. Before, a cancelled job sat 'running' for the whole lease."""
        cur = await self._run(lambda: self._db.execute(
            "UPDATE jobs SET status='queued', attempts=MAX(attempts-1,0), lease_until=0, token=NULL, updated=? "
            "WHERE id=? AND status='running' AND token=?", (time.time(), jid, token)))
        return cur.rowcount > 0

    async def get(self, jid: str) -> dict[str, Any] | None:
        def _g() -> dict[str, Any] | None:
            cols = ["id", "kind", "payload", "status", "attempts", "max_attempts", "created", "updated", "result", "error"]
            r = self._db.execute(f"SELECT {','.join(cols)} FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                return None
            d = dict(zip(cols, r))
            for k in ("payload", "result"):
                d[k] = _decode(d[k])  # one corrupt row no longer makes get() raise forever
            return d
        return await self._run(_g)

    async def stats(self) -> dict[str, int]:
        def _s() -> dict[str, int]:
            return {k: v for k, v in self._db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()}
        return await self._run(_s)

    async def close(self) -> None:
        def _close() -> None:
            with self._tlock:
                if not self._closed:
                    self._closed = True
                    self._db.close()
        async with self._lock:
            await asyncio.to_thread(_close)


class Worker:
    """Pulls jobs and runs them with `handler(kind, payload) -> result`."""
    def __init__(self, queue: JobQueue, handler: Callable[[str, dict], Awaitable[Any]], idle_s: float = 0.5):
        self.q, self.handler, self.idle_s = queue, handler, idle_s
        self._task: asyncio.Task | None = None

    async def _heartbeat(self, jid: str, token: str) -> None:
        # extend at a third of the lease so a long job is never re-claimed (and run twice) mid-flight
        every = max(0.05, self.q.lease_s / 3)
        while True:
            await asyncio.sleep(every)
            try:
                if not await self.q.extend(jid, token):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("job %s: lease heartbeat failed", jid, exc_info=True)

    async def _loop(self) -> None:
        errors = 0
        while True:
            try:
                job = await self.q.claim()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # "database is locked", a full disk, an I/O error: these used to end the loop task
                # silently, and the node stopped running queued work until restart
                if getattr(self.q, "_closed", False):
                    return
                errors += 1
                log.warning("job queue claim failed (%d in a row)", errors, exc_info=True)
                await asyncio.sleep(min(self.idle_s * (2 ** min(errors, 6)), 30))
                continue
            errors = 0
            if job is None:
                await asyncio.sleep(self.idle_s)
                continue
            jid, token = job["id"], job.get("token")
            beat = asyncio.create_task(self._heartbeat(jid, token)) if token else None
            try:
                try:
                    result = await self.handler(job["kind"], job["payload"])
                finally:
                    if beat is not None:
                        beat.cancel()
                        try:
                            await beat
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
                await self.q.complete(jid, result, token)
            except asyncio.CancelledError:
                if token:
                    try:  # hand the job back so the next worker can run it now, not after the lease
                        await asyncio.shield(self.q.release(jid, token))
                    except BaseException:  # noqa: BLE001
                        pass
                raise
            except Exception as e:  # noqa: BLE001
                try:
                    await self.q.fail(jid, f"{type(e).__name__}: {e}", token)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.warning("job %s: could not record failure", jid, exc_info=True)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
