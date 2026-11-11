"""Durable job queue (SQLite, WAL): work survives a restart and retries on failure.

A single-file queue so AVRONE needs no external broker to start. Jobs are claimed
atomically, run, then marked done or retried with backoff; a crash leaves a job
'running' and reap() returns it to the queue after a lease timeout.
For a large fleet, swap this for NATS JetStream or Redis Streams behind the same API.
"""
from __future__ import annotations

import asyncio
import json

from .safeio import json_loads as _jloads  # round 111: RecursionError -> JSONDecodeError
import logging
import math
import os
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
CLOCK_SLACK_S = 5.0  # round 112: `updated` this far ahead of now means the clock stepped back
EXTEND_MAX_S = 7 * 86400  # one heartbeat can never park a job for longer than this
# Retention (round 65). Finished jobs (done/dead, each up to RESULT_MAX of result) were never
# deleted: the file only grew. Unfinished jobs had no cap: an agent in a loop could queue without
# bound. Both are env-tunable; prune runs every PRUNE_EVERY enqueues and on demand.
DEFAULT_MAX_PENDING, HARD_MAX_PENDING = 10_000, 1_000_000
DEFAULT_KEEP_FINISHED, HARD_KEEP_FINISHED = 5_000, 1_000_000
DEFAULT_RETAIN_S = 7 * 86400
PRUNE_EVERY = 100
COMPLETE_TRIES = 4  # round 126: Worker retries recording a result before leaving it to the lease
COMPLETE_BACKOFF_S = 0.5


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


class PermanentJobError(Exception):
    """A job that can never succeed (a malformed payload): the Worker marks it dead at once
    instead of retrying it max_attempts times with backoff."""


def _encode_result(result: Any) -> str:
    """Always valid JSON. The old `json.dumps(result)[:100000]` raised TypeError on a non-JSON
    result (the finished job was then failed and RE-RUN) and cut long results mid-token, so get()
    crashed on the stored text forever after."""
    try:
        result = _redact_result(result)
    except Exception:  # noqa: BLE001  (a cyclic or odd object: json.dumps below reports it)
        pass
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
        return _jloads(text)
    except (TypeError, ValueError):
        return {"corrupt": True, "raw": str(text)[:2000]}


def _err_text(error: Any) -> str:
    """Round 155: always storable. A lone surrogate in an error (a hostile file name or model
    text inside an exception) made sqlite raise UnicodeEncodeError, and so did an exception whose
    __str__ raised: fail() itself failed, the job stayed 'running' until its lease ran out and
    then ran again, and a PermanentJobError was retried instead of going dead. The text is also
    redacted, since /api/fleet/jobs returns it and it is kept on disk ("could not reach
    https://user:pw@host")."""
    if isinstance(error, str):
        text = error
    else:
        try:
            text = str(error)
        except Exception:  # noqa: BLE001
            text = f"<{type(error).__name__}: unprintable>"
    text = text[:ERROR_MAX]
    try:
        from .ctxspawn import redact
        text = redact(text)
    except Exception:  # noqa: BLE001
        pass
    return text.encode("utf-8", "backslashreplace").decode("utf-8")[:ERROR_MAX]


def exc_text(e: BaseException) -> str:
    """'Type: message' for any exception, even one whose __str__ raises."""
    try:
        msg = str(e)
    except Exception:  # noqa: BLE001
        msg = "<unprintable>"
    return f"{type(e).__name__}: {msg}"


def _redact_result(v: Any, depth: int = 0) -> Any:
    """Mask secrets at every traversed depth; never return an uninspected deep subtree."""
    from .ctxspawn import redact
    if isinstance(v, str):
        return redact(v)
    if depth > 64:
        # Returning the original subtree here leaked secrets nested beyond the traversal limit.
        # Replace containers with a marker, while still redacting scalar strings above.
        if isinstance(v, (dict, list, tuple)):
            return "[OMITTED: result nesting limit]"
        return v
    if isinstance(v, list):
        return [_redact_result(x, depth + 1) for x in v]
    if isinstance(v, tuple):
        return tuple(_redact_result(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return {
            redact(k) if isinstance(k, str) else k: _redact_result(x, depth + 1)
            for k, x in v.items()
        }
    return v

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
        try:
            self._open()
        except sqlite3.DatabaseError as e:
            # "file is not a database" / "malformed": raised out of start_queue at boot, which
            # skipped fleet.start() and the fleet's cleanup (round 77). Move it aside, start fresh.
            # A locked file (OperationalError) is not corruption and still raises.
            if isinstance(e, sqlite3.OperationalError):
                raise
            self._set_aside(e)
            self._open()
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

    def _open(self) -> None:
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            # Several JobQueue instances (processes) may share one file: wait for the write lock
            # instead of failing with "database is locked".
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("PRAGMA journal_mode=WAL")
            # round 162: a pruned job's payload/result was left readable in the file's free pages
            self._db.execute("PRAGMA secure_delete=ON")
            self._db.executescript(SCHEMA)
        except BaseException:
            self._db.close()
            raise

    def _set_aside(self, err: BaseException) -> None:
        stamp = f"corrupt-{int(time.time())}"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                try:
                    os.replace(src, Path(f"{self.path}{suffix}.{stamp}"))
                except OSError:
                    pass
        log.warning("job queue %s was unreadable (%s); moved aside as *.%s, starting empty",
                    self.path.name, err, stamp)

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
        # round 164: None/objects were stored as their repr, and a lone surrogate in it raised a raw
        # UnicodeEncodeError out of sqlite; both are now a ValueError naming the problem
        if isinstance(kind, (int, float)) and not isinstance(kind, bool):
            kind = str(kind)  # a numeric kind was always stored as its text (test_bad_priority_and_kind)
        if not isinstance(kind, str) or not kind or len(kind) > 64 or not kind.isprintable():
            raise ValueError("job kind must be a short printable string")
        try:
            kind.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("job kind must be valid text (no lone surrogates)") from None
        if not isinstance(payload, dict):
            raise ValueError("job payload must be a JSON object")
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        # Reject NaN/Infinity: Python's default encoder emits these non-standard JSON tokens,
        # which downstream strict-JSON consumers cannot reliably parse.
        try:
            body = json.dumps(payload, allow_nan=False)  # unserializable values fail before insert
        except RecursionError:
            raise ValueError("job payload is nested too deeply") from None
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
            # round 112: rebase leases written under a clock that has since stepped back. A job
            # whose `updated` is in the future got its lease_until from that clock too: a dead
            # worker's job stayed "running" (and a retry backoff stayed parked) for the whole step,
            # up to EXTEND_MAX_S. Keep the lease time it had left, measured from now.
            self._db.execute(
                "UPDATE jobs SET lease_until=CASE WHEN lease_until>0 THEN ?+MAX(0,MIN(lease_until-updated,?)) "
                "ELSE 0 END, updated=? WHERE status IN ('queued','running') AND updated>?",
                (now - 0.001, EXTEND_MAX_S, now, now + CLOCK_SLACK_S))  # -1 ms: no time left = expired now
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
                    payload = _jloads(row[2]) if row[2] is not None else {}
                except (TypeError, ValueError, RecursionError) as e:
                    # used to raise out of claim() after COMMIT and kill the Worker loop for good
                    self._db.execute("UPDATE jobs SET status='dead', error=?, updated=? WHERE id=?",
                                     (_err_text(f"corrupt payload: {e}"), now, row[0]))
                    continue
                if not isinstance(payload, dict):
                    # a list/number payload (hand edit, another writer) failed in every handler
                    # with AttributeError, retried max_attempts times; it can never run
                    self._db.execute("UPDATE jobs SET status='dead', error=?, updated=? WHERE id=?",
                                     (_err_text(f"corrupt payload: not an object ({type(payload).__name__})"),
                                      now, row[0]))
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

    async def fail(self, jid: str, error: Any, token: str | None = None, permanent: bool = False) -> str:
        """'dead', 'requeued', 'missing' (no such job) or 'stale' (the lease was lost).
        permanent=True: dead now, whatever attempts remain."""
        err = _err_text(error)
        clause, extra = self._owner(token)

        def _fail() -> str:
            now = time.time()
            r = self._db.execute(f"SELECT attempts,max_attempts FROM jobs WHERE id=?{clause}", (jid, *extra)).fetchone()
            if r is None:
                exists = self._db.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone()
                return "stale" if exists else "missing"
            if permanent or r[0] >= r[1]:
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
                except asyncio.CancelledError:
                    if token:
                        try:  # hand the job back so the next worker can run it now, not after the lease
                            await asyncio.shield(self.q.release(jid, token))
                        except BaseException:  # noqa: BLE001
                            pass
                    raise
                except Exception as e:  # noqa: BLE001
                    try:
                        await self.q.fail(jid, exc_text(e), token,
                                          permanent=isinstance(e, PermanentJobError))
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        log.warning("job %s: could not record failure", jid, exc_info=True)
                    continue
                await self._complete(jid, result, token)
            finally:
                if beat is not None:
                    beat.cancel()
                    try:
                        await beat
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

    async def _complete(self, jid: str, result: Any, token: str | None) -> bool:
        """Record a finished job's result. Round 126: a complete() that raised (full disk, "database
        is locked" past busy_timeout, an I/O error) was handled as if the HANDLER had failed: fail()
        requeued the job with that storage error as its error, so work that succeeded ran again and
        a job could go 'dead' after max_attempts successful runs. The write is now retried with
        backoff while the lease heartbeat keeps running; if it never lands the job is left to its
        lease (reaped and run again: at-least-once), never counted as a failed run."""
        for i in range(COMPLETE_TRIES):
            try:
                return await self.q.complete(jid, result, token)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if getattr(self.q, "_closed", False):
                    return False
                log.warning("job %s: could not record result (try %d of %d)", jid, i + 1,
                            COMPLETE_TRIES, exc_info=True)
                if i < COMPLETE_TRIES - 1:
                    await asyncio.sleep(min(COMPLETE_BACKOFF_S * (2 ** i), 5.0))
        log.error("job %s: result not recorded; it runs again when its lease ends", jid)
        return False

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


def scrub_db(path: Path | str, fix: bool = False, batch: int = 500) -> dict[str, int]:
    """Round 158/159: find (and with fix=True mask) secrets in stored job results and errors.

    Results stored before v0.148 and errors stored before v0.148 were written raw, and every
    `/api/fleet/jobs` read served them back. Uses its own connection with BEGIN IMMEDIATE per
    batch, so it is safe beside running workers in other processes. Payloads (the task input)
    are counted but never rewritten: a queued job must still run as submitted. The file is
    opened read-write only if it exists (a scan never creates an empty jobs.db)."""
    from .ctxspawn import redact
    p = Path(path)
    out = {"rows": 0, "flagged": 0, "fixed": 0, "payloads": 0}
    if p.is_symlink() or not p.is_file():
        return out
    db = sqlite3.connect(f"file:{p}?mode={'rw' if fix else 'ro'}", uri=True, isolation_level=None)
    try:
        db.execute("PRAGMA busy_timeout=10000")
        if fix:  # round 162: freed cells are zeroed, not left holding the old raw value
            db.execute("PRAGMA secure_delete=ON")
        last = ""
        while True:
            if fix:
                db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute("SELECT id, payload, result, error FROM jobs WHERE id > ? ORDER BY id LIMIT ?",
                                  (last, int(batch))).fetchall()
                for jid, payload, result, error in rows:
                    out["rows"] += 1
                    if isinstance(payload, str) and redact(payload) != payload:
                        out["payloads"] += 1
                    new_err = redact(error) if isinstance(error, str) else error
                    new_res = result
                    if isinstance(result, str) and result and redact(result) != result:
                        try:
                            new_res = json.dumps(_redact_result(_jloads(result)), default=str, allow_nan=False)
                        except (TypeError, ValueError, RecursionError):
                            new_res = json.dumps(redact(result))  # unreadable JSON: keep it as one masked string
                    if new_err != error or new_res != result:
                        out["flagged"] += 1
                        if fix:
                            db.execute("UPDATE jobs SET result=?, error=? WHERE id=?", (new_res, new_err, jid))
                            out["fixed"] += 1
            except BaseException:
                if fix:
                    db.execute("ROLLBACK")
                raise
            if fix:
                db.execute("COMMIT")
            if len(rows) < batch:
                break
            last = rows[-1][0]
        if fix and out["fixed"]:
            # round 162: the raw values still sat in jobs.db-wal (every page version written since
            # the last checkpoint) for as long as any queue kept the file open, which on a running
            # daemon is always. Copy the WAL into the file and truncate it to zero bytes.
            busy, _log, _done = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            out["wal_cleared"] = int(not busy)
    finally:
        db.close()
    return out
