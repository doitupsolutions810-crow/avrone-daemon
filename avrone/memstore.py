"""Durable per-agent memory namespaces (Eternal CodeX memory store).

One append-only JSONL file (``<data_dir>/memory.jsonl``), the same file the original
memory_save/memory_recall tools used. Every record now carries ``ns``, the namespace it
belongs to, so each agent has its own isolated key space that reloads from disk on restart.

Namespaces
  - a local agent's namespace is its *agent id*: the agent name with the per-process
    counter suffix removed and lower-cased (``Builder-3`` -> ``builder``), so it is stable
    across restarts. An embedder may pin one explicitly with ``ctx.memory_namespace``.
    Local namespaces match ``[a-z0-9][a-z0-9_-]{0,62}``: they can never contain ':'.
  - memory merged in from another fleet node lives under ``fleet:<sender-node>/<ns>``;
    the ':' guarantees it can never collide with (overwrite) a local namespace.
  - legacy records written before namespaces existed (no ``ns``) are read into the
    namespace derived from their ``agent`` field.

Caps (env knobs, each clamped to a hard ceiling so a bad value can't disable the limit)
  AVRONE_MEMORY_MAX_ENTRIES     live keys per namespace (default 500, hard cap 5000);
                                saving a NEW key into a full namespace evicts its oldest key
  AVRONE_MEMORY_MAX_VALUE       bytes per value, UTF-8 (default 8192, hard cap 65536); larger
                                values are refused, never silently truncated
  AVRONE_MEMORY_MAX_NAMESPACES  namespaces per store (default 256, hard cap 4096); a save that
                                would create one more is refused
Keys are capped at 200 characters.

Concurrency: every mutation holds an in-process lock for the file *and* an exclusive
``fcntl.flock`` on ``memory.jsonl.lock`` (cross-process). Appends are single ``write`` calls
under that lock; compaction / eviction rewrites go to a temp file, fsync, then ``os.replace``,
so a crash mid-write leaves either the old or the new file, never a torn one.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX only; on other platforms the in-process lock still serialises writers
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

NS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
REMOTE_PREFIX = "fleet:"
MAX_KEY = 200
# merged (foreign) entries are clamped to at most this far into the future: a peer with a skewed
# or hostile clock would otherwise pin its value forever under last-writer-wins
MAX_FUTURE_SKEW_S = 3600.0


LWW_STEP_S = 0.001


def _next_ts(have: "dict[str, Any] | None", now: float) -> float:
    """A local save's LWW timestamp: the wall clock, but never at or behind the record it replaces.
    Round 117: after the clock stepped back (NTP fix, VM resume) a save got a ts older than the
    value it overwrote; it won locally (file order) but every fleet peer skipped it in merge(), so
    the new value never replicated until the clock caught up. The old ts is capped at
    now + MAX_FUTURE_SKEW_S so a hand-edited far-future record cannot drag saves forward."""
    if not have:
        return now
    prev = min(_ts(have.get("ts")), now + MAX_FUTURE_SKEW_S)
    return now if now > prev else prev + LWW_STEP_S


def _ts(v: Any) -> float:
    """A record's ts as a finite float; anything else (str, bool, NaN, inf, null) reads as 0.0.
    A single corrupt ts used to poison the whole store: eviction/compaction sort and min() over
    mixed str/float raised TypeError on every later save."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    try:
        f = float(v)
    except OverflowError:  # a hand-edited 400-digit integer ts
        return 0.0
    return f if math.isfinite(f) else 0.0

DEFAULT_MAX_ENTRIES, HARD_MAX_ENTRIES = 500, 5000
DEFAULT_MAX_VALUE, HARD_MAX_VALUE = 8192, 65536
DEFAULT_MAX_NAMESPACES, HARD_MAX_NAMESPACES = 256, 4096

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _cap(env: str, default: int, hard: int) -> int:
    try:
        n = int(os.getenv(env, "").strip() or default)
    except ValueError:
        n = default
    return max(1, min(hard, n))


def max_entries() -> int:
    return _cap("AVRONE_MEMORY_MAX_ENTRIES", DEFAULT_MAX_ENTRIES, HARD_MAX_ENTRIES)


def max_value() -> int:
    return _cap("AVRONE_MEMORY_MAX_VALUE", DEFAULT_MAX_VALUE, HARD_MAX_VALUE)


def max_namespaces() -> int:
    return _cap("AVRONE_MEMORY_MAX_NAMESPACES", DEFAULT_MAX_NAMESPACES, HARD_MAX_NAMESPACES)


def agent_namespace(name: Any) -> str:
    """Stable local namespace for an agent name: counter suffix stripped, lower-cased, sanitised."""
    base = re.sub(r"-\d+$", "", str(name or "").strip())
    ns = re.sub(r"[^a-z0-9_-]", "", base.lower().replace(" ", "-")).lstrip("-_")[:63]
    return ns if NS_RE.match(ns) else "agent"


def remote_namespace(sender: str, ns: str) -> str:
    return f"{REMOTE_PREFIX}{sender}/{ns}"


def is_local(ns: str) -> bool:
    return isinstance(ns, str) and bool(NS_RE.match(ns))


DEFAULT_REMOTE_NS_PER_SENDER, HARD_REMOTE_NS_PER_SENDER = 32, 4096


def remote_ns_per_sender() -> int:
    """Namespaces one fleet peer may own on this node (AVRONE_MEMORY_MAX_REMOTE_NS, default 32)."""
    return _cap("AVRONE_MEMORY_MAX_REMOTE_NS", DEFAULT_REMOTE_NS_PER_SENDER, HARD_REMOTE_NS_PER_SENDER)


def remote_ns_total() -> int:
    """All peers together may own at most half the namespace cap, so the node's own agents always
    have room. Before: one child pushing 200 one-entry namespaces filled the default 256-namespace
    cap and every local agent's first memory_save on this node was refused from then on."""
    return max(1, max_namespaces() // 2)


def remote_sender(ns: str) -> str:
    if isinstance(ns, str) and ns.startswith(REMOTE_PREFIX) and "/" in ns:
        return ns[len(REMOTE_PREFIX):].split("/", 1)[0]
    return ""


def merge_key(v: Any) -> str | None:
    """A foreign entry's key, or None when it is not a string or an integer. A dict or list key used
    to be stored as its Python repr ("{'a': 1}")."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return None
    return str(v).strip()


def merge_value(v: Any) -> str | None:
    """A foreign entry's value, or None when unusable. Local saves always store text, but merge used
    str(): a null value arrived as the text "None" and a dict as its Python repr. A number is kept
    as text; bools, null, objects and lists are refused."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int) or (isinstance(v, float) and math.isfinite(v)):
        return str(v)
    return None


from .safeio import NotRegularFile, open_append, open_lock

class MemoryRefused(ValueError):
    """A save refused by a cap or a bad key/namespace."""


class _Oldest:
    """Per-namespace min-heap of (ts, insertion order, key, record) for one merge_many call.

    Eviction and the "older than everything kept" check each scanned the whole namespace per
    incoming entry: a 4000-entry fleet merge into a full 5000-key namespace took 6.7 s holding the
    store lock (quadratic). Entries go stale when their key is replaced or evicted; they are
    dropped lazily (`space.get(key) is rec`). Ties on ts break by dict order, as min() did."""

    def __init__(self) -> None:
        self._heaps: dict[str, list[tuple[float, int, int, str, dict[str, Any]]]] = {}
        self._seq: dict[str, dict[str, int]] = {}
        self._next: dict[str, int] = {}
        self._uniq = 0  # keeps tuples from ever comparing the record dicts (same ts, same key)

    def _heap(self, ns: str, space: dict[str, dict[str, Any]]) -> list[tuple[float, int, int, str, dict[str, Any]]]:
        h = self._heaps.get(ns)
        if h is None:
            seq = {k: i for i, k in enumerate(space)}
            h = [(_ts(r.get("ts", 0)), seq[k], 0, k, r) for k, r in space.items()]
            heapq.heapify(h)
            self._heaps[ns], self._seq[ns], self._next[ns] = h, seq, len(seq)
        return h

    def _top(self, ns: str, space: dict[str, dict[str, Any]]) -> tuple[float, int, int, str, dict[str, Any]]:
        h = self._heap(ns, space)
        while h and space.get(h[0][3]) is not h[0][4]:
            heapq.heappop(h)
        return h[0]

    def min_ts(self, ns: str, space: dict[str, dict[str, Any]]) -> float:
        return self._top(ns, space)[0]

    def pop(self, ns: str, space: dict[str, dict[str, Any]]) -> str:
        top = self._top(ns, space)
        heapq.heappop(self._heaps[ns])
        self._seq[ns].pop(top[3], None)
        return top[3]

    def put(self, ns: str, space: dict[str, dict[str, Any]], key: str, rec: dict[str, Any]) -> None:
        h = self._heap(ns, space)
        seq = self._seq[ns]
        if key not in seq:  # a replaced key keeps its dict position, so it keeps its order too
            seq[key] = self._next[ns]
            self._next[ns] += 1
        self._uniq += 1
        heapq.heappush(h, (_ts(rec.get("ts", 0)), seq[key], self._uniq, key, rec))


RECORDS_CACHE_FILES = 8
_RECORDS_CACHE: dict[str, tuple[tuple[int, int, int, int], list[dict[str, Any]]]] = {}
_RECORDS_GUARD = threading.Lock()


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._key = str(self.path.resolve())
        with _LOCKS_GUARD:
            self._tlock = _LOCKS.setdefault(self._key, threading.Lock())

    # ------------------------------------------------------------ locking / io
    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._tlock:
            try:
                fh = open_lock(self.lock_path)
            except OSError as e:  # a symlink/FIFO at memory.lock was followed (or blocked)
                raise MemoryRefused(f"memory lock unusable: {e}") from e
            held = False
            try:
                if fcntl is not None:
                    from .safeio import flock_wait, lock_wait_s
                    if not flock_wait(fh.fileno()):  # round 104: a hung peer no longer pins this thread
                        raise MemoryRefused(f"memory is locked by another process (waited {lock_wait_s():g} s); "
                                            "try again later")
                    held = True
                yield
            finally:
                if held:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()

    def _read_raw(self) -> tuple[tuple[int, int, int, int] | None, bytes]:
        """(identity, bytes) of memory.jsonl, or (None, b"") when it is missing or not a regular
        file. read_text() blocked forever on a FIFO there, so one FIFO hung every memory op."""
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None, b""
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None, b""
            ident = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            with _RECORDS_GUARD:
                hit = _RECORDS_CACHE.get(self._key)
            if hit is not None and hit[0] == ident:
                return ident, b""
            return ident, fh.read()

    def _records(self) -> list[dict[str, Any]]:
        """Every usable record, oldest first. Parsing is cached per file identity (dev, inode,
        size, mtime_ns): load() re-parsed the whole file on every call, ~0.9 s at 200,000 lines,
        and ctxspawn calls it on the event loop for every spawn (round 80). Callers get fresh
        shallow copies, so mutating a result never touches the cache."""
        ident, raw = self._read_raw()
        if ident is None:
            return []
        key = self._key
        with _RECORDS_GUARD:
            hit = _RECORDS_CACHE.get(key)
        if hit is not None and hit[0] == ident:
            return [dict(r) for r in hit[1]]
        out = self._parse(raw)
        with _RECORDS_GUARD:
            _RECORDS_CACHE.pop(key, None)
            _RECORDS_CACHE[key] = (ident, out)
            while len(_RECORDS_CACHE) > RECORDS_CACHE_FILES:
                _RECORDS_CACHE.pop(next(iter(_RECORDS_CACHE)))
        return [dict(r) for r in out]

    @staticmethod
    def _parse(raw: bytes) -> list[dict[str, Any]]:
        out = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (ValueError, RecursionError):  # torn/corrupt line, or absurdly nested JSON
                continue
            if isinstance(rec, dict) and "key" in rec:
                # a hand-edited or foreign line with a dict/list/bool key or a null/object value used
                # to be served as is: load() returned {"{'x': 1}": {... "value": 5}} and tools that
                # slice or join values raised on it. Keys/values read the way merge() takes them.
                key, value = merge_key(rec.get("key")), merge_value(rec.get("value"))
                if not key or value is None or len(key) > MAX_KEY:
                    continue  # no save can make a key over MAX_KEY (round 80); big values stay readable (v0.20)
                rec["key"], rec["value"] = key, value
                if not isinstance(rec.get("ns"), str) or not rec["ns"]:
                    rec["ns"] = agent_namespace(rec.get("agent"))
                if "ts" in rec:
                    rec["ts"] = _ts(rec["ts"])
                out.append(rec)
        return out

    @staticmethod
    def _fold(records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
        spaces: dict[str, dict[str, dict[str, Any]]] = {}
        for rec in records:
            spaces.setdefault(rec["ns"], {})[str(rec["key"])] = rec
        return spaces

    def _append(self, recs: list[dict[str, Any]]) -> None:
        try:
            st = os.lstat(self.path)  # lstat: a symlink to a regular file passed os.stat (round 84)
            if not stat.S_ISREG(st.st_mode):  # a FIFO here blocked open("a") forever
                raise MemoryRefused("memory file is not a regular file")
        except FileNotFoundError:
            pass
        data = "".join(json.dumps(r) + "\n" for r in recs)
        # A crash mid-append can leave a torn last line with no newline; appending straight
        # onto it would glue the new record to the torn one and lose both. Start a fresh line.
        try:
            # round 106: lstat above, then open() here: a FIFO swapped in between blocked this
            # thread forever WITH the memory lock held, so every memory op on the node stopped.
            from .safeio import open_read
            with open_read(self.path) as f:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        data = "\n" + data
        except FileNotFoundError:
            pass
        except NotRegularFile as e:
            raise MemoryRefused("memory file is not a regular file") from e
        try:
            f = open_append(self.path, "a")
        except NotRegularFile as e:  # a symlink here appended to its target (os.stat followed it)
            raise MemoryRefused("memory file is not a regular file") from e
        with f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def _rewrite(self, spaces: dict[str, dict[str, dict[str, Any]]]) -> None:
        recs = sorted((r for ns in spaces.values() for r in ns.values()), key=lambda r: r.get("ts", 0))
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:  # a symlink planted at the temp name was followed and its target overwritten (round 80)
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
        with os.fdopen(fd, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ reads
    def namespaces(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Every namespace -> {key: latest record}."""
        return self._fold(self._records())

    def load(self, ns: str) -> dict[str, dict[str, Any]]:
        return self.namespaces().get(ns, {})

    # ------------------------------------------------------------ writes
    def _check(self, ns: str, key: str, value: str) -> None:
        if not key:
            raise MemoryRefused("key is required")
        if len(key) > MAX_KEY:
            raise MemoryRefused(f"key longer than {MAX_KEY} characters")
        try:  # a lone surrogate raised UnicodeEncodeError, which callers catching MemoryRefused missed
            key.encode("utf-8")
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise MemoryRefused("key/value is not valid text (lone surrogate)") from None
        if size > max_value():
            raise MemoryRefused(f"value is {size} bytes; cap is {max_value()} (AVRONE_MEMORY_MAX_VALUE)")
        if not ns:
            raise MemoryRefused("namespace is required")

    def _put_locked(self, spaces: dict[str, dict[str, dict[str, Any]]], rec: dict[str, Any],
                    pending: list[dict[str, Any]], oldest: "_Oldest | None" = None) -> bool:
        """Apply one record to the folded view; returns True when the file must be rewritten
        (an eviction happened). Raises MemoryRefused on the namespace cap. `oldest` (merge_many)
        finds the key to evict from a heap instead of a scan of the namespace."""
        ns, key = rec["ns"], rec["key"]
        if ns not in spaces and len(spaces) >= max_namespaces():
            raise MemoryRefused(f"namespace cap reached ({max_namespaces()}, AVRONE_MEMORY_MAX_NAMESPACES)")
        space = spaces.setdefault(ns, {})
        evicted = False
        if key not in space:
            while len(space) >= max_entries():
                victim = oldest.pop(ns, space) if oldest is not None else \
                    min(space, key=lambda k: space[k].get("ts", 0))
                del space[victim]
                evicted = True
        space[key] = rec
        if oldest is not None:
            oldest.put(ns, space, key, rec)
        pending.append(rec)
        return evicted

    def _commit(self, records: list[dict[str, Any]], spaces: dict[str, dict[str, dict[str, Any]]],
                pending: list[dict[str, Any]], evicted: bool) -> None:
        live = sum(len(v) for v in spaces.values())
        if evicted or len(records) + len(pending) > max(256, 4 * live):
            self._rewrite(spaces)  # atomic compaction: drops superseded + evicted records
        elif pending:
            self._append(pending)

    def save(self, ns: str, key: str, value: str, **meta: Any) -> dict[str, Any]:
        key, value = str(key).strip(), str(value)
        self._check(ns, key, value)
        rec = {"ns": ns, "key": key, "value": value, "ts": time.time(), **meta}
        with self._locked():
            records = self._records()
            spaces = self._fold(records)
            rec["ts"] = _next_ts(spaces.get(ns, {}).get(key), rec["ts"])
            pending: list[dict[str, Any]] = []
            evicted = self._put_locked(spaces, rec, pending)
            self._commit(records, spaces, pending, evicted)
        return rec

    def merge(self, ns: str, entries: list[dict[str, Any]], **meta: Any) -> dict[str, int]:
        """Last-writer-wins merge of foreign entries into `ns` (by their own ts). Entries that are
        older than or equal to what `ns` already holds are skipped; oversize/bad ones are rejected."""
        return self.merge_many({ns: entries}, **meta)

    def merge_many(self, groups: dict[str, list[dict[str, Any]]], **meta: Any) -> dict[str, int]:
        """merge() into several namespaces under ONE lock, read and rewrite. A fleet push spread over
        32 namespaces used to re-read and rewrite the whole store 32 times (~0.8 s each at 200k lines)."""
        stats = {"merged": 0, "skipped": 0, "rejected": 0}
        with self._locked():
            records = self._records()
            spaces = self._fold(records)
            pending: list[dict[str, Any]] = []
            evicted = False
            oldest = _Oldest()
            for ns, e in ((n, x) for n, items in groups.items() for x in items):
                if not isinstance(e, dict):
                    stats["rejected"] += 1
                    continue
                key, value = merge_key(e.get("key", "")), merge_value(e.get("value", ""))
                if key is None or value is None:
                    stats["rejected"] += 1
                    continue
                try:
                    raw_ts = e.get("ts", 0)
                    if isinstance(raw_ts, bool):
                        raise TypeError("bool ts")
                    ts = float(raw_ts)
                    if not math.isfinite(ts):  # NaN always "won" LWW; inf pinned the key forever
                        raise ValueError("ts not finite")
                    ts = min(ts, time.time() + MAX_FUTURE_SKEW_S)  # clamp far-future claims
                    self._check(ns, key, value)
                except (TypeError, ValueError, OverflowError):
                    stats["rejected"] += 1
                    continue
                space = spaces.get(ns, {})
                have = space.get(key)
                if have is not None:
                    hts = _ts(have.get("ts"))
                    # round 117: equal ts (two nodes saving in one coarse clock tick) was "skipped"
                    # on both sides, so each kept its own value forever. Break the tie on the value
                    # itself: every node converges on the same winner whatever the merge order.
                    if hts > ts or (hts == ts and str(have.get("value", "")) >= value):
                        stats["skipped"] += 1
                        continue
                # A NEW key into a full namespace evicts the oldest key. When the incoming entry is
                # itself older than everything kept, it would evict a newer key and then be the
                # next one evicted, so it was counted "merged" while the store lost a newer memory.
                if have is None and len(space) >= max_entries() and space and \
                        ts <= oldest.min_ts(ns, space):
                    stats["skipped"] += 1
                    continue
                rec = {"ns": ns, "key": key, "value": value, "ts": ts, **meta}
                if e.get("agent"):
                    rec["agent"] = str(e["agent"])[:100]
                try:
                    evicted = self._put_locked(spaces, rec, pending, oldest) or evicted
                except MemoryRefused:
                    stats["rejected"] += 1
                    continue
                stats["merged"] += 1
            self._commit(records, spaces, pending, evicted)
        return stats

    def delete(self, ns: str, key: str) -> bool:
        """Remove one key from `ns` (atomic rewrite). False when it was not there."""
        with self._locked():
            spaces = self._fold(self._records())
            space = spaces.get(ns, {})
            if key not in space:
                return False
            del space[key]
            self._rewrite(spaces)
        return True

    def trim(self, ns: str, keep: int) -> int:
        """Drop the oldest keys of `ns` until at most `keep` remain (atomic rewrite).
        Returns how many keys were evicted."""
        try:  # "abc", NaN, inf, None used to raise out of trim()
            keep = max(0, int(keep)) if not isinstance(keep, bool) else 0
        except (TypeError, ValueError, OverflowError):
            raise MemoryRefused("keep must be a whole number") from None
        with self._locked():
            spaces = self._fold(self._records())
            space = spaces.get(ns, {})
            extra = len(space) - keep
            if extra <= 0:
                return 0
            for key in sorted(space, key=lambda k: space[k].get("ts", 0))[:extra]:
                del space[key]
            self._rewrite(spaces)
        return extra
