"""Checkpoint / resume for long squad and deep runs.

A long run on an edge node can be killed halfway (power, thermal shutdown, a
dropped SSH session). With a checkpoint file, Trinity records progress as it
goes: the plan, each worker's (or reviewer's) finished result, each completed
deep round, and finally the whole result. Re-running the same task with the
same checkpoint file skips everything already done and continues from there.

File format (JSON, written atomically via a temp file + os.replace):

    {"version": 1, "updated": <unix ts>, "steps": {"<key>": <json value>, ...}}

Keys are namespaced by scope, e.g. "meta", "subtasks", "worker.0", "result" for a
squad; "round1.reviewer.2", "rounds", "first_answer", "wave1.worker.0" for deep
(the first-wave squad of a deep run lives under the "wave1." scope).

Rules: a scope is reused only when its "meta" (task, mode and size) matches the
new call; otherwise that scope is wiped and the run starts fresh. Failed worker
results ("[X failed: ...]") are never stored, so a resume retries them. A
corrupt or unreadable file is treated as empty, and a write that fails never
breaks the run (the checkpoint is best effort).
"""
from __future__ import annotations

import logging
import math
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("avrone.checkpoint")

VERSION = 1
MAX_BYTES_DEFAULT = 64 * 1024 * 1024
STALE_TMP_SECONDS = 3600


def max_bytes() -> int:
    """AVRONE_CHECKPOINT_MAX_BYTES (default 64 MiB, clamped 64 KiB..1 GiB). A bigger file is not
    loaded into memory; it is set aside like a corrupt one."""
    try:
        v = int(str(os.getenv("AVRONE_CHECKPOINT_MAX_BYTES", "")).strip())
    except ValueError:
        return MAX_BYTES_DEFAULT
    return max(64 * 1024, min(v, 1024 * 1024 * 1024))


def _path_key(path: Path) -> str:
    """One key per file even on a case-insensitive filesystem (macOS APFS default): "Job.json" and
    "job.json" are the same file there, so they must share one lock and one busy flag. On a
    case-sensitive filesystem this only makes two ids differing in case conflict, which is safe."""
    try:
        k = str(Path(path).resolve())
    except (OSError, RuntimeError):
        k = str(path)
    return k.casefold()


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _set_aside(path: Path, why: str) -> Path | None:
    """Rename an unusable checkpoint out of the way instead of letting the next save overwrite it."""
    dest = path.with_name(f"{path.name}.{why}-{int(time.time())}")
    n = 0
    while dest.exists() and n < 100:
        n += 1
        dest = path.with_name(f"{path.name}.{why}-{int(time.time())}-{n}")
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def _normalized(v: Any) -> Any:
    """v as it reads back after a JSON round trip (tuples -> lists, int keys -> str keys)."""
    try:
        return json.loads(json.dumps(v, default=str))
    except (TypeError, ValueError, RecursionError):
        return v

# One re-entrant lock per checkpoint file (resolved path), shared by every _Store on it. A put()
# used to mutate the shared dict and serialise/os.replace it with no lock: two threads could
# serialise in one order and os.replace in the other, so the file on disk lost a finished step
# (a resume then re-ran it), and two _Stores on one file lost each other's keys outright.
_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path: Path) -> threading.RLock:
    k = _path_key(path)
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(k, threading.RLock())


class _Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": VERSION, "steps": {}}
        self.write_errors = 0
        self.set_aside: Path | None = None
        self.lock = _file_lock(self.path)
        # keys this process wrote / deleted since the last save: on save the file is re-read under a
        # process lock and only THESE are applied on top, so two processes writing one checkpoint
        # (the MCP server and the HTTP node on one data dir) no longer erase each other's steps
        self._dirty: set[str] = set()
        self._dropped: set[str] = set()
        # (inode, size, mtime_ns) of the file as this store last wrote it: when the file still has
        # it at the next save, nobody else wrote in between and the re-read is skipped. Every step
        # used to re-read and re-parse the whole file (18 MB: ~40 ms of an ~80 ms save)
        self._sig: tuple[int, int, int] | None = None
        self.load()

    def load(self) -> None:
        """Read the file. A missing file is a fresh start. A file that exists but cannot be used
        (corrupt JSON, wrong shape, another format version, over the size cap, not a regular file)
        used to be treated as empty and then silently overwritten by the next save; it is now
        renamed aside (<name>.corrupt-<ts> / .v<N>-<ts> / .toobig-<ts>) so nothing is lost."""
        with self.lock:
            self._clean_stale_tmp()
            try:
                st = self.path.stat()
            except FileNotFoundError:
                return
            except OSError:
                return
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode):
                return  # a directory or device: never read, never renamed
            if st.st_size > max_bytes():
                self.set_aside = _set_aside(self.path, "toobig")
                return
            from .safeio import FileTooBig, read_capped
            try:
                # round 106/107: stat() then read_bytes() followed a symlink and blocked forever
                # on a FIFO swapped in between; read_capped is O_NOFOLLOW|O_NONBLOCK, regular only
                data = read_capped(self.path, max_bytes())
            except FileTooBig:  # grew past the cap after the stat
                self.set_aside = _set_aside(self.path, "toobig")
                return
            except OSError:
                return
            if data is None:
                return
            try:
                raw = json.loads(data.decode("utf-8"))
            except OSError:
                return
            except (ValueError, RecursionError):
                if st.st_size == 0:  # a crash between create and write: nothing to keep
                    return
                self.set_aside = _set_aside(self.path, "corrupt")
                return
            if isinstance(raw, dict) and isinstance(raw.get("steps"), dict) and raw.get("version") == VERSION:
                self.data = raw
            elif isinstance(raw, dict) and raw.get("version") not in (None, VERSION) \
                    and isinstance(raw.get("version"), int) and not isinstance(raw.get("version"), bool):
                self.set_aside = _set_aside(self.path, f"v{raw.get('version')}")
            else:
                self.set_aside = _set_aside(self.path, "corrupt")

    def _clean_stale_tmp(self) -> None:
        """Temp files from a writer killed between mkstemp and os.replace pile up forever."""
        try:
            now = time.time()
            for t in self.path.parent.glob(glob_escape(self.path.name) + ".*.tmp"):
                try:
                    if now - t.stat().st_mtime > STALE_TMP_SECONDS:
                        t.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def save(self) -> bool:
        with self.lock:
            return self._save_locked()

    def _stat_sig(self) -> "tuple[int, int, int] | None":
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def _merge_disk(self) -> None:
        if not (self._dirty or self._dropped):
            return
        if self._sig is not None and self._stat_sig() == self._sig:
            return  # unchanged since our own last write
        try:
            st = self.path.stat()
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode) or st.st_size > max_bytes() or st.st_size == 0:
                return
            from .safeio import read_capped  # round 107: runs under the save lock; never a FIFO
            data = read_capped(self.path, max_bytes())
            if data is None:
                return
            raw = json.loads(data.decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return
        if not (isinstance(raw, dict) and isinstance(raw.get("steps"), dict) and raw.get("version") == VERSION):
            return
        mine = self.data["steps"]
        steps = {k: v for k, v in raw["steps"].items() if k not in self._dropped}
        for k in self._dirty:
            if k in mine:
                steps[k] = mine[k]
        self.data["steps"] = steps
        for k, v in raw.items():  # e.g. an owner stamped by another process
            if k not in ("steps", "updated") and k not in self.data:
                self.data[k] = v

    def _save_locked(self) -> bool:
        fd = None
        try:
            # round 102: O_NOFOLLOW + regular only. A symlink at <run>.json.lock created its
            # target (anywhere the daemon can write); a FIFO there was flock'ed as if a lock file.
            from .safeio import _open_regular
            fd = _open_regular(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            from .safeio import flock_wait
            if not flock_wait(fd):  # round 104: a hung peer holding the lock: report a failed save
                os.close(fd)
                log.warning("checkpoint %s: lock held by another process; save skipped", self.path.name)
                return False
        except (OSError, ImportError):
            pass  # no data dir yet / no flock: the save below reports any real error
        try:
            self._merge_disk()
            ok = self._write_locked()
            if ok:
                self._dirty.clear()
                self._dropped.clear()
            return ok
        finally:
            if fd is not None:
                os.close(fd)

    def _write_locked(self) -> bool:
        self.data["updated"] = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, default=str)
                    # durable before the rename: without fsync a power cut after os.replace can
                    # leave a zero-length checkpoint (delayed allocation), losing every step
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                _fsync_dir(self.path.parent)
                self._sig = self._stat_sig()
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except (OSError, TypeError, ValueError, RecursionError):
            self.write_errors += 1
            return False


def glob_escape(name: str) -> str:
    import glob as _glob
    return _glob.escape(name)


class Checkpoint:
    """A (scoped) view on one checkpoint file."""

    def __init__(self, path: str | Path | _Store, prefix: str = ""):
        self._store = path if isinstance(path, _Store) else _Store(Path(path))
        self.prefix = prefix

    @classmethod
    def coerce(cls, obj: "Checkpoint | str | Path | None") -> "Checkpoint | None":
        """None -> None; a path -> a Checkpoint on that file; a Checkpoint -> itself."""
        if obj is None or obj == "" or obj is False:
            return None
        if isinstance(obj, Checkpoint):
            return obj
        return cls(Path(obj))

    @property
    def path(self) -> Path:
        return self._store.path

    def scope(self, name: str) -> "Checkpoint":
        """A sub-view whose keys live under '<prefix><name>' in the same file."""
        return Checkpoint(self._store, self.prefix + name)

    # ------------------------------------------------------------ key/value
    def get(self, key: str, default: Any = None) -> Any:
        return self._store.data["steps"].get(self.prefix + key, default)

    def put(self, key: str, value: Any) -> bool:
        """Store one step. A value that cannot be serialised (a cycle, non-string dict keys such as
        tuples) used to go into the shared dict anyway, so every later save failed too and the run
        silently stopped checkpointing. It is now refused alone (False, write_errors += 1) and the
        stored copy is the JSON round trip, so a resume sees exactly what a fresh process would."""
        try:
            encoded = json.dumps(value, default=str)
            stored = json.loads(encoded)
        except (TypeError, ValueError, RecursionError):
            with self._store.lock:
                self._store.write_errors += 1
            return False
        with self._store.lock:  # mutate + serialise + replace as one critical section
            self._store.data["steps"][self.prefix + key] = stored
            self._store._dirty.add(self.prefix + key)
            self._store._dropped.discard(self.prefix + key)
            return self._store.save()

    def keys(self) -> list[str]:
        n = len(self.prefix)
        with self._store.lock:
            return [k[n:] for k in list(self._store.data["steps"]) if k.startswith(self.prefix)]

    def clear(self) -> None:
        """Drop every key in this scope (other scopes in the file are kept)."""
        with self._store.lock:
            steps = self._store.data["steps"]
            for k in [k for k in steps if k.startswith(self.prefix)]:
                del steps[k]
                self._store._dirty.discard(k)
                self._store._dropped.add(k)
            self._store.save()

    # ------------------------------------------------------------ run binding
    def resume_point(self, task: str, mode: str, **extra: Any) -> dict[str, Any] | None:
        """The stored meta if this scope holds a run of the same task/mode/extra, else None.

        A scope holding a different run is cleared so stale results can never leak in."""
        with self._store.lock:
            meta = self.get("meta")
            # compare as the values read back from JSON: a tuple extra (stored as a list) never
            # matched, so every resume wiped the scope and started over
            want = _normalized({"task": task, "mode": mode, **extra})
            if isinstance(meta, dict) and isinstance(want, dict) and all(meta.get(k) == v for k, v in want.items()):
                return meta
            if self.keys():
                self.clear()
            return None

    def begin(self, task: str, mode: str, run_id: str, **extra: Any) -> None:
        with self._store.lock:
            meta = self.get("meta")
            if not (isinstance(meta, dict) and meta.get("run_id") == run_id and meta.get("task") == task):
                self.put("meta", {"task": task, "mode": mode, "run_id": run_id, "started": time.time(), **extra})

    def summary(self) -> dict[str, Any]:
        keys = self.keys()
        return {"path": str(self.path), "steps": len(keys),
                "done": self.get("result") is not None, "write_errors": self._store.write_errors}


# ---------------------------------------------------------------- server-managed checkpoints
# Over the API, MCP, CLI (--checkpoint-id) and fleet, a client names a checkpoint by id only.
# The file always lives at <data_dir>/checkpoints/<key>.json; a client never supplies a path.
# Fleet jobs are keyed "fleet-<sender>-<digest>-<id>" and stamped with their owner, so one sender can
# neither collide with nor resume (read) another sender's checkpoint.
CKPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
FLEET_PREFIX = "fleet-"
CKPT_MODES = ("squad", "deep")
_active: set[str] = set()  # resolved paths of checkpoints a run in this process is using
_active_lock = threading.Lock()  # claim's "in use?" check and its add are one critical section


class CheckpointError(ValueError):
    """A bad checkpoint id (-> HTTP 400)."""


class CheckpointConflict(ValueError):
    """The id holds a different task/mode/owner, or is in use by a running job (-> HTTP 409)."""


def sanitize_id(raw: Any) -> str:
    """A client checkpoint id: 1-64 chars of [A-Za-z0-9_-], else CheckpointError."""
    if not isinstance(raw, str) or not CKPT_ID_RE.match(raw):
        raise CheckpointError("checkpoint_id must be 1-64 characters of A-Z a-z 0-9 _ -")
    return raw


def sanitize_client_id(raw: Any) -> str:
    """sanitize_id plus: the fleet- prefix is reserved for checkpoints of remote jobs."""
    cid = sanitize_id(raw)
    if cid.startswith(FLEET_PREFIX):
        raise CheckpointError(f"checkpoint ids starting with {FLEET_PREFIX!r} are reserved for fleet jobs")
    return cid


def fleet_key(sender: str, ckpt_id: Any) -> str:
    cid = sanitize_id(ckpt_id)
    if not isinstance(sender, str) or not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", sender):
        raise CheckpointError("fleet checkpoint needs a valid sender node id")
    # sender and id may both contain "-", so "a" + "b-c" and "a-b" + "c" would collide on
    # a bare join; a short digest of the (sender, id) pair keeps keys unique per pair.
    digest = hashlib.sha256(f"{sender}\0{cid}".encode()).hexdigest()[:10]
    return f"{FLEET_PREFIX}{sender}-{digest}-{cid}"


def managed_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "checkpoints"


def managed_path(data_dir: str | Path, key: str) -> Path:
    """The file for a (validated) key; never escapes <data_dir>/checkpoints."""
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise CheckpointError("bad checkpoint id")
    return managed_dir(data_dir) / f"{key}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        p = Path(path)
        from .safeio import read_capped  # round 107: no is_file()/read_bytes() swap window
        data = read_capped(p, max_bytes())
        if data is None:
            return None
        raw = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("steps"), dict) else None


def check_compatible(path: str | Path, task: str, mode: str, owner: str | None = None) -> dict[str, Any] | None:
    """Raise CheckpointConflict unless `path` is free, unreadable, or holds this same task/mode/owner.

    Returns the stored top-level meta (a resume) or None (a fresh start)."""
    path = Path(path)
    key = path.stem
    if is_busy(path):
        raise CheckpointConflict(f"checkpoint {key!r} is in use by a running job; wait for it or use another id")
    data = _read(path)
    if data is None:
        return None
    if data.get("owner") != owner:
        raise CheckpointConflict(f"checkpoint {key!r} belongs to another owner; use a different checkpoint_id")
    meta = data["steps"].get("meta")
    if not isinstance(meta, dict):
        return None
    if meta.get("mode") != mode or meta.get("task") != task:
        raise CheckpointConflict(
            f"checkpoint {key!r} holds a different {meta.get('mode')} task "
            f"({_preview(meta.get('task'))!r}); re-post the same task to resume, "
            f"use a new checkpoint_id, or DELETE /api/checkpoints/{key}")
    return meta


def stamp_owner(path: str | Path, owner: str) -> None:
    """Record who owns a fleet checkpoint before its run starts writing it."""
    path = Path(path)
    with _file_lock(path):  # read-modify-write of the file: never interleave with a put()/save()
        data = _read(path) or {"version": VERSION, "steps": {}}
        if data.get("owner") == owner:
            return
        store = _Store(path)
        store.data = {**data, "owner": owner}
        store.save()


def is_busy(path: str | Path) -> bool:
    return _path_key(Path(path)) in _active


def claim(path: str | Path) -> Any:
    """Mark the checkpoint in use; returns a release() callable. CheckpointConflict when taken."""
    k = _path_key(Path(path))
    with _active_lock:
        if k in _active:
            raise CheckpointConflict(f"checkpoint {Path(path).stem!r} is in use by a running job")
        # ...and not by a job in ANOTHER process on this data dir (the in-process set cannot see it).
        # A non-blocking flock on <ckpt>.claim: the kernel drops it if that process dies, so a crash
        # never leaves the checkpoint stuck "in use".
        # Round 131: the same open used to follow a symlink (O_RDWR|O_CREAT created the link's
        # target anywhere the daemon could write, then flock'ed that file) and treated a FIFO,
        # socket or directory as "no flock here", so claim() succeeded with no cross-process
        # exclusion. A non-regular claim file is a conflict, not a best-effort in-process claim.
        fd = None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            from .safeio import NotRegularFile, _open_regular
            try:
                fd = _open_regular(str(path) + ".claim", os.O_RDWR | os.O_CREAT, 0o600)
            except NotRegularFile as e:
                raise CheckpointConflict(
                    f"checkpoint {Path(path).stem!r} claim file is not a regular file") from e
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except CheckpointConflict:
            raise
        except BlockingIOError:
            os.close(fd)
            fd = None
            raise CheckpointConflict(f"checkpoint {Path(path).stem!r} is in use by a job in another process") from None
        except (OSError, ImportError):
            if fd is not None:
                os.close(fd)
            fd = None
        _active.add(k)

    def release() -> None:
        nonlocal fd
        with _active_lock:
            _active.discard(k)
            if fd is not None:
                os.close(fd)
                fd = None
    return release


@contextmanager
def claimed(path: str | Path):
    release = claim(path)
    try:
        yield
    finally:
        release()


def _preview(task: Any, n: int = 120) -> str:
    t = " ".join(str(task or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def describe(path: str | Path, full: bool = False) -> dict[str, Any] | None:
    """Summary of one managed checkpoint file (None if missing/unreadable)."""
    path = Path(path)
    data = _read(path)
    if data is None:
        return None
    steps = data["steps"]
    meta = steps.get("meta") if isinstance(steps.get("meta"), dict) else {}
    result = steps.get("result")
    rounds = steps.get("rounds") if isinstance(steps.get("rounds"), dict) else {}
    history = rounds.get("history")
    mode = meta.get("mode")
    out: dict[str, Any] = {
        "id": path.stem, "mode": mode if isinstance(mode, str) else None, "task": _preview(meta.get("task")),
        "done_steps": sum(1 for k in steps if not (k == "meta" or k.endswith(".meta"))),
        "done": isinstance(result, dict), "updated": _num(data.get("updated")), "busy": is_busy(path),
    }
    if full:
        n = meta.get("n")
        out.update(run_id=meta.get("run_id"), started=_num(meta.get("started")), owner=data.get("owner"),
                   squad_size=n if isinstance(n, int) and not isinstance(n, bool) else None,
                   keys=sorted(str(k) for k in steps),
                   rounds_done=len(history) if isinstance(history, list) else 0)
        if isinstance(result, dict):
            out["answer_preview"] = _preview(result.get("answer") or result.get("report"), 400)
    return out


def _num(v: Any) -> float | None:
    """A finite number or None: a hand-edited "updated": "yesterday" used to crash list sorting."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:  # "updated": 10**400 hand-edited
        return None
    return f if math.isfinite(f) else None


LIST_MANAGED_MAX = 1000


def list_managed(data_dir: str | Path) -> list[dict[str, Any]]:
    d = managed_dir(data_dir)
    rows = []
    from .safeio import newest_files  # round 92: the newest LIST_MANAGED_MAX files, not all of them
    for p in sorted(newest_files(d, ".json", LIST_MANAGED_MAX)[0]) if d.is_dir() else []:
        # symlinks are skipped: a link to any JSON file elsewhere must not be read through the API
        if _KEY_RE.match(p.stem) and not p.is_symlink() and (row := describe(p)) is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r.get("updated") or 0.0, r["id"]), reverse=True)
    return rows


def delete_managed(data_dir: str | Path, key: str) -> bool:
    """True if deleted, False if missing. CheckpointConflict while a run uses it."""
    p = managed_path(data_dir, key)
    if is_busy(p):
        raise CheckpointConflict(f"checkpoint {key!r} is in use by a running job")
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
