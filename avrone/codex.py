"""Eternal CodeX: the event bus and the permanent agent action log.

Every agent thought, tool call, spawn and result is an event. Events go to
live subscribers (the HUD over WebSocket) and are appended to a JSONL log per
run, so any run can be replayed or audited later.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import math
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Event:
    run_id: str
    kind: str  # run.start | agent.spawn | agent.think | tool.call | tool.result | agent.done | lead.* | run.done | error
    agent: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


def _redact_on() -> bool:
    import os
    return (os.getenv("AVRONE_CODEX_REDACT") or "on").strip().lower() not in ("off", "0", "false", "no")


def _scrub(v: Any, depth: int = 0) -> Any:
    """Mask secrets (env secret values, tokens, keys, URL credentials) in every string an event carries,
    before it is written to the run log or sent to /ws/events. Never raises; deep junk is stringified."""
    from .ctxspawn import redact
    if isinstance(v, str):
        return redact(v) if len(v) >= 6 else v
    if depth > 8:
        return redact(v) if not isinstance(v, (int, float, bool, type(None))) else v
    if isinstance(v, dict):
        return {k: _scrub(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x, depth + 1) for x in v]
    return v


JSON_MAX_DEPTH = 32
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9_\-.]{1,128}")


def jsonable(v: Any, depth: int = 0) -> Any:
    """Event data as strict JSON values: what json.dumps(allow_nan=False) and a browser's JSON.parse
    both accept. Before, a tuple dict key raised TypeError out of emit() (killing the agent step that
    logged it), NaN/Infinity were written as bare NaN (the HUD's JSON.parse threw, and
    /api/runs/{id} answered 500 "Out of range float"), and bytes or sets sent live made the
    WebSocket pump raise and drop the dashboard. Never raises."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if depth >= JSON_MAX_DEPTH:
        return "[...]"
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, x in v.items():
            if isinstance(k, str):
                key = k
            elif k is None or isinstance(k, (bool, int)):
                key = json.dumps(k)  # what json.dumps would have done with the key
            elif isinstance(k, float):
                key = json.dumps(k) if math.isfinite(k) else str(k)
            else:
                try:
                    key = str(k)
                except Exception:  # noqa: BLE001 - a hostile __str__ must not break logging
                    key = f"<{type(k).__name__}>"
            out[key] = jsonable(x, depth + 1)
        return out
    if isinstance(v, (list, tuple)):
        return [jsonable(x, depth + 1) for x in v]
    if isinstance(v, (set, frozenset)):
        try:
            items = sorted(v, key=repr)
        except Exception:  # noqa: BLE001
            items = list(v)
        return [jsonable(x, depth + 1) for x in items]
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", errors="replace")
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return f"<{type(v).__name__}>"


def wire_text(ev: "Event") -> str:
    """The event's strict-JSON text: the copy emit() already serialized, else a fresh dump."""
    w = getattr(ev, "_wire", None)
    return w if isinstance(w, str) else dumps_event(asdict(ev))


def dumps_event(ev: dict[str, Any]) -> str:
    """One event as strict JSON text (no NaN), for the run log and the live stream."""
    try:
        return json.dumps(ev, allow_nan=False)
    except (TypeError, ValueError):
        return json.dumps(jsonable(ev), allow_nan=False)


def log_name(run_id: str) -> str:
    """The run log's file stem. A run id that is not a plain name ("../x", "a/b", a 300-char id, ".")
    used to become the path itself, so emit("../escape") wrote outside runs/. Odd ids map to a
    stable hashed stem, so emit and replay still agree."""
    rid = str(run_id)
    if _SAFE_RUN_ID.fullmatch(rid) and not rid.startswith("."):
        return rid
    return "x-" + hashlib.sha256(rid.encode("utf-8", "surrogatepass")).hexdigest()[:24]


DEFAULT_MAX_SUBSCRIBERS, HARD_MAX_SUBSCRIBERS = 64, 1024


def max_subscribers() -> int:
    """Live event-stream subscribers allowed at once (AVRONE_MAX_WS_SUBSCRIBERS)."""
    import os
    try:
        n = int(os.environ.get("AVRONE_MAX_WS_SUBSCRIBERS", "").strip() or DEFAULT_MAX_SUBSCRIBERS)
    except ValueError:
        n = DEFAULT_MAX_SUBSCRIBERS
    return max(1, min(HARD_MAX_SUBSCRIBERS, n))


DEFAULT_REPLAY_EVENTS, HARD_REPLAY_EVENTS = 2000, 50_000
MAX_LINE_BYTES = 4 * 1024 * 1024  # one event line; anything longer is skipped, never loaded whole


def replay_limit() -> int:
    """Newest events a replay returns (AVRONE_REPLAY_MAX_EVENTS)."""
    import os
    try:
        n = int(os.environ.get("AVRONE_REPLAY_MAX_EVENTS", "").strip() or DEFAULT_REPLAY_EVENTS)
    except ValueError:
        n = DEFAULT_REPLAY_EVENTS
    return max(1, min(HARD_REPLAY_EVENTS, n))


RUNS_PROMPT_CHARS = 2000

# Size-based rotation (round 72). A log never rotated: fleet.jsonl gains an event per heartbeat,
# join, dispatch and refusal for the life of the node, and one long or looping run grew its own log
# until the disk filled. Past AVRONE_CODEX_LOG_MAX_BYTES the active <stem>.jsonl is renamed to
# <stem>.jsonl.1 (older archives shift up to AVRONE_CODEX_LOG_KEEP, the oldest is dropped).
# Archives do not match *.jsonl, so run listings and telemetry still see one file per run;
# replay() reads back into the archives when the active file holds fewer events than asked.
DEFAULT_LOG_MAX_BYTES, HARD_LOG_MAX_BYTES = 64 * 1024 * 1024, 1 << 40
DEFAULT_LOG_KEEP, HARD_LOG_KEEP = 3, 100


def _env_whole(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        n = default
    return max(lo, min(hi, n))


def log_max_bytes() -> int:
    """Rotate a codex log once it would pass this size (AVRONE_CODEX_LOG_MAX_BYTES; 0 = never)."""
    n = _env_whole("AVRONE_CODEX_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES, 0, HARD_LOG_MAX_BYTES)
    return n if n == 0 else max(n, 4096)


DEFAULT_FIELD_MAX_CHARS, HARD_FIELD_MAX_CHARS = 1_000_000, 64_000_000


def field_max_chars() -> int:
    """Longest string an event keeps (AVRONE_CODEX_FIELD_MAX_CHARS; 0 = no cap). Redaction runs
    about ten linear regexes per string on the event loop: a 20 MB write_file content in one
    tool.call event took 10.9 s to emit (round 81)."""
    n = _env_whole("AVRONE_CODEX_FIELD_MAX_CHARS", DEFAULT_FIELD_MAX_CHARS, 0, HARD_FIELD_MAX_CHARS)
    return n if n == 0 else max(n, 1024)


DEFAULT_ARG_CHARS = 65_536


def codex_arg_chars() -> int:
    """Longest string kept from a tool call's args in its tool.call event (AVRONE_CODEX_ARG_CHARS;
    0 = only the field cap). The tool itself always gets the full args; tool.result already kept
    only 4000 chars of output."""
    return _env_whole("AVRONE_CODEX_ARG_CHARS", DEFAULT_ARG_CHARS, 0, HARD_FIELD_MAX_CHARS)


def clip_strings(v: Any, limit: int, depth: int = 0) -> Any:
    """Every string in v cut to `limit` chars with a "...[clipped N chars]" marker (dicts and lists
    walked to depth 8; their keys are cut too). Anything else is returned as is."""
    if limit <= 0:
        return v
    if isinstance(v, str):
        return v if len(v) <= limit else f"{v[:limit]}...[clipped {len(v) - limit} chars]"
    if depth > 8:
        return v
    if isinstance(v, dict):
        return {clip_strings(k, limit, depth + 1) if isinstance(k, str) else k: clip_strings(x, limit, depth + 1)
                for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clip_strings(x, limit, depth + 1) for x in v]
    return v


def log_keep() -> int:
    """Rotated archives kept per log (AVRONE_CODEX_LOG_KEEP; 0 = drop the old log on rotation)."""
    return _env_whole("AVRONE_CODEX_LOG_KEEP", DEFAULT_LOG_KEEP, 0, HARD_LOG_KEEP)


def archive_path(p: Path, k: int) -> Path:
    return p.with_name(f"{p.name}.{k}")


def archives_of(path: Path) -> list[Path]:
    """Rotated archives of a log, newest first (stops at the first gap). A symlink is never an
    archive: following one would read a planted file from outside runs/."""
    out = []
    for k in range(1, HARD_LOG_KEEP + 1):
        a = archive_path(path, k)
        try:
            if a.is_symlink() or not a.is_file():
                break
        except OSError:
            break
        out.append(a)
    return out


def log_chain(path: Path) -> list[Path]:
    """A log and its archives, newest first. The active file is left out when it is missing (just
    rotated away, or never re-created) or is not a plain file, so readers never crash on it."""
    try:
        live = [path] if path.is_file() and not path.is_symlink() else []
    except OSError:
        live = []
    return live + archives_of(path)


def _whole(v: Any, default: int, cap: int) -> int:
    """A count from a caller: junk ("abc", NaN, inf, None, bool) reads as `default`; 1..cap."""
    if isinstance(v, bool):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(cap, n))


def iter_lines(f, max_line: int = MAX_LINE_BYTES):
    """Lines of a text file, each read at most max_line chars at a time; an overlong line yields
    None (and is skipped through in chunks), so a 1 GB line never sits in memory."""
    while True:
        line = f.readline(max_line)
        if not line:
            return
        if line.endswith("\n") or len(line) < max_line:
            yield line
            continue
        while True:  # overlong: discard the rest of this line
            more = f.readline(max_line)
            if not more or more.endswith("\n"):
                break
        yield None


class SubscribersFull(RuntimeError):
    pass


class Codex:
    def __init__(self, data_dir: Path):
        self.log_dir = data_dir / "runs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        # the event loop each subscriber queue belongs to: asyncio.Queue is not thread-safe, and a
        # put from a worker thread never wakes the loop, so a waiting dashboard stalled until some
        # unrelated loop activity (measured: a thread emit arrived only when a 3 s timer fired)
        self._loops: dict[asyncio.Queue[Event], asyncio.AbstractEventLoop] = {}
        # emit() is called from the event loop and from worker threads (runner, fleet);
        # one lock keeps each JSONL record a single whole line.
        self._write_lock = threading.Lock()
        self.dropped = 0  # events a full subscriber queue could not take (slow dashboard)
        self.emitted = 0
        self.refused = 0  # subscribe attempts turned away at AVRONE_MAX_WS_SUBSCRIBERS
        # events each subscriber missed because its queue was full: reported to that subscriber as
        # one "stream.gap" event as soon as it has room, so a slow HUD knows its view has holes
        self._gaps: dict[asyncio.Queue[Event], int] = {}

    def subscribe(self) -> asyncio.Queue[Event]:
        # every emit fans out to every subscriber, and each queue holds up to 1000 events: unbounded
        # subscribers made each run's events cost O(subscribers) and memory grow per connection
        if len(self._subscribers) >= max_subscribers():
            self.refused += 1
            raise SubscribersFull(f"event stream full ({max_subscribers()} subscribers; AVRONE_MAX_WS_SUBSCRIBERS)")
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        try:
            self._loops[q] = asyncio.get_running_loop()
        except RuntimeError:
            pass  # subscribed outside a loop: delivered by direct put, as before
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)
        self._loops.pop(q, None)
        self._gaps.pop(q, None)

    def _offer(self, q: asyncio.Queue[Event], ev: Event) -> None:
        missed = self._gaps.get(q, 0)
        if missed and q.maxsize - q.qsize() >= 2:  # room for the notice AND this event
            q.put_nowait(Event(run_id=ev.run_id, kind="stream.gap", agent="CodeX",
                               data={"missed": missed, "note": "this stream dropped events while the "
                                     "client was slow; replay the run for the full log"}))
            self._gaps.pop(q, None)
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            self.dropped += 1
            if q in self._subscribers:
                self._gaps[q] = self._gaps.get(q, 0) + 1

    def _deliver(self, q: asyncio.Queue[Event], ev: Event) -> None:
        loop = self._loops.get(q)
        if loop is None:
            return self._offer(q, ev)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return self._offer(q, ev)
        if loop.is_closed():
            self.unsubscribe(q)
            return None
        try:
            loop.call_soon_threadsafe(self._offer, q, ev)
        except RuntimeError:  # loop closed between the check and the call
            self.unsubscribe(q)

    def emit(self, run_id: str, kind: str, agent: str, **data: Any) -> Event:
        data = clip_strings(data, field_max_chars())
        if _redact_on():
            data = _scrub(data)
        data = jsonable(data)
        ev = Event(run_id=str(run_id), kind=str(kind), agent=str(agent), data=data)
        text = dumps_event(asdict(ev))
        # serialized once: every WebSocket pump used to re-run asdict()+dumps for its own copy, so a
        # 4 MB event cost ~1.3 s of event-loop time across 32 HUD subscribers
        ev._wire = text  # type: ignore[attr-defined]
        line = (text + "\n").encode("utf-8", errors="backslashreplace")
        path = self.log_dir / f"{log_name(run_id)}.jsonl"
        with self._write_lock:
            cap = log_max_bytes()
            if cap:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                if size and size + len(line) > cap:
                    self._rotate(path)
            self._append(path, line)
        self.emitted += 1
        for q in list(self._subscribers):
            self._deliver(q, ev)
        return ev

    def _rotate(self, path: Path) -> None:
        """<stem>.jsonl -> .jsonl.1 (older archives shift up, the oldest past log_keep() goes).
        Any failure (an archive name taken by a directory, a read-only dir) keeps writing to the
        active log: an audit event is never dropped for a rotation problem."""
        keep = log_keep()
        try:
            if keep == 0:
                path.unlink()
                self.rotations = getattr(self, "rotations", 0) + 1
                return
            for a in [archive_path(path, keep)] + self.archives(path)[keep:]:  # keep may have shrunk
                if a.is_file() or a.is_symlink():
                    a.unlink()
            for k in range(keep - 1, 0, -1):
                a = archive_path(path, k)
                if a.is_file() and not a.is_symlink():
                    os.replace(a, archive_path(path, k + 1))
            os.replace(path, archive_path(path, 1))
            self.rotations = getattr(self, "rotations", 0) + 1
        except OSError as e:
            import logging
            logging.getLogger("avrone.codex").warning("could not rotate %s: %s", path.name, e)

    def archives(self, path: Path) -> list[Path]:
        return archives_of(path)

    @staticmethod
    def _append(path: Path, line: bytes) -> None:
        with path.open("ab+") as f:
            # A crash mid-write leaves a last line with no newline; appending straight after it glued
            # the NEXT event onto the torn bytes, so the first event after every crash was lost too.
            # Start on a fresh line instead (only the torn event itself is lost). One write() call, so
            # O_APPEND keeps concurrent writers' events whole.
            fd = f.fileno()
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            f.write(line)

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any] | None:
        """One JSONL record, or None for a blank, truncated, corrupt or non-object line."""
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(obj, dict):
            return None
        if "NaN" in line or "Infinity" in line:  # logs written before strict JSON: keep replay servable
            obj = jsonable(obj)
        return obj

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        stamped = []
        limit = _whole(limit, 50, 10_000)
        for p in self.log_dir.glob("*.jsonl"):
            try:  # a log can vanish between listing and stat (cleanup, concurrent delete)
                if not p.is_file():  # a FIFO named x.jsonl blocked the listing forever in open()
                    continue
                stamped.append((p.stat().st_mtime, p))
            except OSError:
                continue
        stamped.sort(key=lambda t: t[0], reverse=True)
        out = []
        for _, p in stamped[:limit]:
            arch = self.archives(p)
            src = arch[-1] if arch else p  # after a rotation the run's start event is in the oldest archive
            try:
                with src.open(encoding="utf-8", errors="replace") as f:
                    first = next(iter_lines(f), None)
                start = self._parse_line(first) if first else None
            except OSError:
                continue
            if start is None:
                continue
            data = start.get("data")
            prompt = data.get("prompt", "") if isinstance(data, dict) else ""
            prompt = prompt if isinstance(prompt, str) else ("" if prompt is None else str(prompt))
            # a 4 MB prompt per run made a 50-run listing a 200 MB response
            out.append({"run_id": p.stem, "started": start.get("ts"), "prompt": prompt[:RUNS_PROMPT_CHARS]})
        return out

    def replay(self, run_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """The newest intact events of a run (at most `limit`, default AVRONE_REPLAY_MAX_EVENTS), in
        order. Truncated/corrupt lines (e.g. a crash mid-write) are skipped, so one bad line never
        hides the rest of the audit log. Streams the file: a 200 MB log used to be read whole and
        parsed whole (1.4 GB RSS for one request)."""
        p = self.log_dir / f"{log_name(run_id)}.jsonl"
        if not p.is_file() or not p.resolve().is_relative_to(self.log_dir.resolve()):
            return []  # is_file: a FIFO hung the replay request in open()
        from collections import deque
        n = replay_limit() if limit is None else _whole(limit, replay_limit(), HARD_REPLAY_EVENTS)
        out: list[dict[str, Any]] = []
        for i, src in enumerate([p, *self.archives(p)]):
            # newest file first; an older archive is read only while the newer ones fell short
            keep: deque[dict[str, Any]] = deque(maxlen=n - len(out))
            try:
                with src.open(encoding="utf-8", errors="replace") as f:
                    for line in iter_lines(f):
                        ev = self._parse_line(line) if line is not None else None
                        if ev is not None:
                            keep.append(ev)
            except OSError:
                if i == 0:
                    return []
                break
            out = list(keep) + out
            if len(out) >= n:
                break
        return out
