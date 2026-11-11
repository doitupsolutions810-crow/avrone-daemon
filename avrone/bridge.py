"""Learning bridge: a run-scoped blackboard where agents among agents conclude together.

Every agent in one run (squad workers, their sub-agents at any depth, deep reviewers) sees
the same Bridge. A post is::

    {id, run_id, agent, role, kind, text, refs: [post ids], ts, origin_node}

with kind one of finding | question | answer | correction | conclusion.

Bounds (env knobs, clamped to hard ceilings)
  AVRONE_BRIDGE_MAX_POSTS   posts kept per run (default 500, hard cap 5000); the oldest are
                            dropped first
  per-post text             4000 characters (longer text is truncated, marked "...")

Persistence: one JSONL file per run under ``<data_dir>/bridge/<run_id>.jsonl``. Opening a
bridge replays it; truncated or corrupt lines are skipped. Writes hold a per-file lock, so
posting is safe from the event loop and from worker threads.

Fleet relay: ``export(since)`` hands out posts, ``merge_remote(posts, origin_node)`` takes
posts from another node (dedupe by id, origin preserved, local posts never overwritten).
This module adds no fleet endpoints; another layer moves the posts.

Deep mode's first wave runs as ``<run_id>-wave1``; it shares the deep run's bridge, so the
reviewers read what the first wave concluded.
"""
from __future__ import annotations

import json
import math
import contextlib
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from .tools import Tool, off_loop

KINDS = ("finding", "question", "answer", "correction", "conclusion")
MAX_TEXT = 4000
MAX_REFS = 20
DEFAULT_MAX_POSTS, HARD_MAX_POSTS = 500, 5000
DEFAULT_ROUNDS, HARD_MAX_ROUNDS = 1, 3
_RUN_RE = re.compile(r"[^A-Za-z0-9_.-]")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}\Z")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(os.getenv(name, "").strip() or default)
    except ValueError:
        n = default
    return max(lo, min(hi, n))


def max_posts() -> int:
    return _int_env("AVRONE_BRIDGE_MAX_POSTS", DEFAULT_MAX_POSTS, 1, HARD_MAX_POSTS)


def bridge_rounds() -> int:
    """AVRONE_BRIDGE_ROUNDS: cross-examination rounds in a squad (default 1, 0 = off, max 3)."""
    return _int_env("AVRONE_BRIDGE_ROUNDS", DEFAULT_ROUNDS, 0, HARD_MAX_ROUNDS)


def bridge_run_id(run_id: Any) -> str:
    """The bridge a run posts to: deep's '<id>-wave1' first wave shares '<id>'."""
    rid = str(run_id or "run")
    if rid.endswith("-wave1") and len(rid) > len("-wave1"):
        rid = rid[: -len("-wave1")]
    return rid


def safe_run_id(run_id: Any) -> str:
    """The bridge file name for a run. Round 129: ids that differ only in dropped characters
    ("r\ud800" and "r", "运行1" and "运行2", or two ids sharing their first 80 characters) used to
    share one bridge file, so one run read and capped the other's posts. An id that had to be
    changed now carries 12 hex of its SHA-256, as the task board's file name has since round 112.
    An id that is already safe is unchanged."""
    raw = str(run_id or "")
    rid = _RUN_RE.sub("", raw)[:80].lstrip(".")
    if rid == raw and rid:
        return rid
    import hashlib
    return (rid or "run") + "-" + hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _clip(text: Any, redact: bool = True) -> str:
    """Round 154: the text is redacted before it is kept. The bridge journal is relayed to every
    peer (fleet relay, the fusion evidence bus), and only the CodeX copy of a post was redacted, so
    a finding that quoted a config ("token=...", "curl -u admin:pw") was shipped verbatim. Merged
    remote posts go through here too, so a peer that did not redact is not re-exported as is.
    Redacting before the cut keeps a token from being cut into a shape no rule knows. Journal
    replay passes redact=False: those lines were redacted when they were written, and a replay
    runs on every blackboard poll."""
    t = str(text if text is not None else "")
    if redact:
        from .ctxspawn import redact as _redact
        t = _redact(t)
    t = t.strip()
    return t if len(t) <= MAX_TEXT else t[: MAX_TEXT - 3] + "..."


def _refs(refs: Any) -> list[str]:
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    out: list[str] = []
    for r in refs:
        r = str(r).strip()
        if r and _ID_RE.match(r) and r not in out:
            out.append(r)
        if len(out) >= MAX_REFS:
            break
    return out


def _remote_ts(ts: Any) -> float:
    """A relayed post's ts: finite, non-bool number, else now."""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return time.time()
    try:
        ts = float(ts)
    except OverflowError:  # a peer relayed ts as a 400-digit int
        return time.time()
    return ts if math.isfinite(ts) and ts > 0 else time.time()



JOURNAL_READ_MAX = 64 * 1024 * 1024


def _read_journal(path: Path) -> str:
    """The bridge journal's text (its newest 64 MB at most). read_text() blocked forever on a FIFO
    there (boot hung in Bridge()) and followed a planted symlink; both now read as empty."""
    from .safeio import read_regular
    raw = read_regular(path, JOURNAL_READ_MAX)
    return "" if raw is None else raw.decode("utf-8", errors="replace")

class BridgeError(ValueError):
    """A post refused (bad kind, empty text, unknown refs)."""


class Bridge:
    def __init__(self, run_id: str, data_dir: Path | str | None = None, codex: Any = None,
                 node_id: str = "local", limit: int | None = None):
        self.run_id = bridge_run_id(run_id)
        self.codex = codex
        self.node_id = str(node_id or "local")
        self.limit = int(limit) if limit else max_posts()
        self.path: Path | None = None
        if data_dir is not None:
            d = Path(data_dir) / "bridge"
            d.mkdir(parents=True, exist_ok=True)
            self.path = d / f"{safe_run_id(self.run_id)}.jsonl"
        key = str(self.path.resolve()) if self.path else f"mem:{id(self)}"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())
        self._posts: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._evicted: "OrderedDict[str, None]" = OrderedDict()  # tombstones: ids the ring dropped
        self._lines = 0
        self._load()

    # ------------------------------------------------------------ io
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        with self._lock:
            try:
                text = _read_journal(self.path)
            except OSError:
                return
            self._replay(text)

    def _replay(self, text: str) -> None:
        """Rebuild the ring from file text (call with the lock held)."""
        self._posts.clear()
        self._lines = 0
        for line in text.splitlines():
            tomb = self._parse_tombstones(line)
            if tomb is not None:
                for t in tomb:
                    self._tomb(t)
                continue
            rec = self._parse(line)
            if rec is None:
                continue
            self._lines += 1
            if rec["id"] in self._evicted:
                continue
            self._posts.pop(rec["id"], None)
            self._posts[rec["id"]] = rec
        self._trim()

    @staticmethod
    def _parse_tombstones(line: str) -> list[str] | None:
        if '"tombstones"' not in line:
            return None
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(rec, dict) or set(rec) != {"tombstones"} or not isinstance(rec["tombstones"], list):
            return None
        return [t for t in rec["tombstones"] if isinstance(t, str) and _ID_RE.match(t)]

    def _tomb(self, post_id: str) -> None:
        self._evicted.pop(post_id, None)
        self._evicted[post_id] = None
        cap = max(64, 4 * self.limit)
        while len(self._evicted) > cap:
            self._evicted.popitem(last=False)

    @staticmethod
    def _parse(line: str) -> dict[str, Any] | None:
        line = line.strip()
        if not line:
            return None
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not _ID_RE.match(rec["id"]):
            return None
        if rec.get("kind") not in KINDS or not isinstance(rec.get("text"), str):
            return None
        rec["refs"] = _refs(rec.get("refs"))
        rec["text"] = _clip(rec["text"], redact=False)
        return rec

    @contextlib.contextmanager
    def _file_lock(self):
        """Serialize append/compaction across processes, not just threads.

        The sidecar inode remains stable when the JSONL file is atomically replaced.
        This matters on multi-worker deployments where each process owns a Bridge.
        """
        if self.path is None:
            yield
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        from .safeio import open_lock
        with open_lock(lock_path) as lock_file:
            try:
                import fcntl  # POSIX: Linux and macOS
            except ImportError:  # pragma: no cover - non-POSIX fallback
                yield
                return
            from .safeio import flock_wait
            if not flock_wait(lock_file.fileno()):  # round 104: _write keeps the post in memory
                raise OSError("bridge journal lock is held by another process")
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write(self, recs: list[dict[str, Any]]) -> None:
        if self.path is None or not recs:
            return
        try:
            with self._file_lock():
                from .safeio import open_append
                with open_append(self.path, "a+b") as f:
                    f.seek(0, os.SEEK_END)
                    lead = b""
                    if f.tell() > 0:  # a torn last line (crash mid-write) must not swallow this post
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":
                            lead = b"\n"
                    f.seek(0, os.SEEK_END)
                    f.write(lead + "".join(json.dumps(r, default=str) + "\n" for r in recs).encode("utf-8"))
                self._lines += len(recs)
                if self._lines > 2 * self.limit:  # replay under process lock before atomic compaction
                    self._replay(_read_journal(self.path))
                    from .safeio import atomic_write
                    head = json.dumps({"tombstones": list(self._evicted)}) + "\n" if self._evicted else ""
                    # durable before the rename (fsync): a power cut after os.replace could otherwise
                    # leave a zero-length journal (delayed allocation) and lose every post of the run
                    atomic_write(self.path, head + "".join(json.dumps(r, default=str) + "\n"
                                                           for r in self._posts.values()), fsync=True)
                    self._lines = len(self._posts)
        except OSError:
            pass  # the in-memory bridge still works; persistence is best effort

    def _trim(self) -> None:
        while len(self._posts) > self.limit:
            pid, _ = self._posts.popitem(last=False)
            self._tomb(pid)

    # ------------------------------------------------------------ writes
    def post(self, agent: str, kind: str, text: str, refs: Iterable[str] | None = None,
             role: str = "", require_refs: bool = False) -> dict[str, Any]:
        kind = str(kind or "finding").strip().lower()
        if kind not in KINDS:
            raise BridgeError(f"kind must be one of {', '.join(KINDS)}")
        body = _clip(text)
        if not body:
            raise BridgeError("text is required")
        if refs is not None and not isinstance(refs, (str, list, tuple)):
            # refs=1.5 / true / {"a": 1} raised TypeError ('float' object is not iterable) from list()
            refs = list(refs) if isinstance(refs, (set, frozenset)) else [refs]
        refs_l = _refs(refs)
        with self._lock:
            unknown = [r for r in refs_l if r not in self._posts]
            if unknown and self.path is not None:
                # another PROCESS on this data dir (a second worker, the MCP server) may have posted
                # it: this Bridge's ring only learns of those at compaction, so a reply to a sibling
                # process's post was refused as "unknown post id". Re-read the journal and recheck.
                try:
                    with self._file_lock():
                        self._replay(_read_journal(self.path))
                except OSError:
                    pass
                unknown = [r for r in refs_l if r not in self._posts]
            if unknown:
                raise BridgeError(f"unknown post id(s): {', '.join(unknown[:5])}")
            if require_refs and not refs_l:
                raise BridgeError("refs is required: the id(s) of the post(s) you reply to")
            rec = {"id": uuid.uuid4().hex[:12], "run_id": self.run_id, "agent": str(agent or "agent")[:80],
                   "role": str(role or "")[:60], "kind": kind, "text": body, "refs": refs_l,
                   "ts": time.time(), "origin_node": self.node_id}
            self._posts[rec["id"]] = rec
            self._trim()
            self._write([rec])
        if self.codex is not None:
            try:
                self.codex.emit(self.run_id, "bridge.post", rec["agent"], post_id=rec["id"], post_kind=kind,
                                text=body[:2000], refs=refs_l, role=rec["role"])
            except Exception:  # noqa: BLE001 - the audit log must never sink a post
                pass
        return dict(rec)

    def reply(self, agent: str, refs: Iterable[str], text: str, kind: str = "answer",
              role: str = "") -> dict[str, Any]:
        return self.post(agent, kind, text, refs=refs, role=role, require_refs=True)

    def merge_remote(self, posts: Any, origin_node: str) -> dict[str, int]:
        """Take posts relayed from another node. Dedupe by id; a local post is never overwritten;
        a post's own origin_node is kept (else `origin_node`). Refs may point at posts we lack."""
        stats = {"merged": 0, "skipped": 0, "rejected": 0}
        origin = str(origin_node or "remote")[:80]
        fresh: list[dict[str, Any]] = []
        with self._lock:
            for p in posts if isinstance(posts, list) else []:
                try:
                    rec = self._parse(json.dumps(p, default=str)) if isinstance(p, dict) else None
                except (ValueError, TypeError, RecursionError):  # circular / absurdly nested
                    rec = None
                if rec is None:
                    stats["rejected"] += 1
                    continue
                if rec["id"] in self._posts or rec["id"] in self._evicted:
                    stats["skipped"] += 1
                    continue
                rec = {"id": rec["id"], "run_id": self.run_id, "agent": str(rec.get("agent") or "agent")[:80],
                       "role": str(rec.get("role") or "")[:60], "kind": rec["kind"],
                       "text": _clip(rec["text"]),  # round 154: a peer's post is redacted before we keep it
                       "refs": rec["refs"], "ts": _remote_ts(rec.get("ts")),
                       "origin_node": str(rec.get("origin_node") or origin)[:80]}
                self._posts[rec["id"]] = rec
                fresh.append(rec)
                stats["merged"] += 1
            self._trim()
            self._write(fresh)
        if fresh and self.codex is not None:
            try:
                self.codex.emit(self.run_id, "bridge.merge", "Bridge", origin=origin, merged=len(fresh))
            except Exception:  # noqa: BLE001
                pass
        return stats

    # ------------------------------------------------------------ reads
    def posts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._posts.values()]

    def get(self, post_id: str) -> dict[str, Any] | None:
        with self._lock:
            p = self._posts.get(post_id)
            return dict(p) if p else None

    def __len__(self) -> int:
        return len(self._posts)

    def export(self, since: str | None = None) -> list[dict[str, Any]]:
        """Posts after `since` (a post id), oldest first; all when since is None or unknown."""
        posts = self.posts()
        if since:
            ids = [p["id"] for p in posts]
            if since in ids:
                posts = posts[ids.index(since) + 1:]
        return posts

    def read(self, kind: str | None = None, agent: str | None = None, since: str | None = None,
             query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        out = self.export(since)
        if kind:
            out = [p for p in out if p["kind"] == str(kind).lower()]
        if agent:
            a = str(agent).lower()
            out = [p for p in out if a in p["agent"].lower() or a == p["role"].lower()]
        if query:
            q = str(query).lower()
            out = [p for p in out if q in p["text"].lower()]
        limit = max(1, min(int(limit or 50), 200))
        return out[-limit:]

    def unresolved(self) -> list[dict[str, Any]]:
        """Questions and corrections no later answer/conclusion refers to."""
        posts = self.posts()
        settled = {r for p in posts if p["kind"] in ("answer", "conclusion") for r in p["refs"]}
        return [p for p in posts if p["kind"] in ("question", "correction") and p["id"] not in settled]

    def digest(self, max_chars: int = 6000) -> str:
        """Compact text for a lead: what agents concluded together, then unresolved points."""
        posts = self.posts()
        if not posts:
            return ""
        concl = [p for p in posts if p["kind"] == "conclusion"]
        corr = [p for p in posts if p["kind"] == "correction"]
        open_ = self.unresolved()
        parts: list[str] = []

        def block(title: str, items: list[dict[str, Any]]) -> None:
            if items:
                parts.append(title + "\n" + "\n".join(
                    f"- [{p['id']}] {p['agent']}: {p['text'][:600]}" for p in items))
        block("What agents concluded together:", concl)
        block("Corrections raised:", corr)
        block("Unresolved disagreements / open questions:", open_)
        if not parts:
            parts.append(f"{len(posts)} finding(s) posted; no conclusions or corrections.")
        text = "\n\n".join(parts)
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def compact(posts: list[dict[str, Any]], width: int = 300) -> str:
    """One line per post for agents: [id] kind agent (refs): text."""
    if not posts:
        return "(no posts)"
    lines = []
    for p in posts:
        refs = f" re {','.join(p['refs'])}" if p.get("refs") else ""
        org = "" if p.get("origin_node") in (None, "", "local") else f" @{p['origin_node']}"
        from .tools import one_line  # round 134: \r / U+2028 in a post (or a peer's agent name)
        t = one_line(p["text"])      # printed lines that read as other posts
        lines.append(one_line(f"[{p['id']}] {p['kind']} {p['agent']}{org}{refs}: ") + t[:width]
                     + ("..." if len(t) > width else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- per-core registry

_MAX_OPEN = 64


def for_core(core: Any, run_id: str) -> Bridge:
    """The one Bridge for this run on this core (opened once, replayed from disk)."""
    rid = bridge_run_id(run_id)
    guard = getattr(core, "_bridge_guard", None)
    if guard is None:
        with _LOCKS_GUARD:
            guard = getattr(core, "_bridge_guard", None)
            if guard is None:
                guard = threading.Lock()
                try:
                    core._bridge_guard = guard
                    core._bridges = OrderedDict()
                except AttributeError:  # pragma: no cover - slotted core
                    pass
    with guard:
        cache = getattr(core, "_bridges", None)
        if cache is None:
            cache = OrderedDict()
            core._bridges = cache
        b = cache.get(rid)
        if b is None:
            settings = getattr(core, "settings", None)
            data_dir = getattr(settings, "data_dir", None)
            fleet = getattr(core, "fleet", None)
            node = getattr(fleet, "node_id", None) or "local"
            b = Bridge(rid, data_dir, codex=getattr(core, "codex", None), node_id=str(node))
            cache[rid] = b
            while len(cache) > _MAX_OPEN:
                cache.popitem(last=False)
        else:
            cache.move_to_end(rid)
        return b


def load_posts(data_dir: Path | str, run_id: str) -> list[dict[str, Any]] | None:
    """Posts persisted for a run, or None when that run has no bridge file."""
    rid = safe_run_id(bridge_run_id(run_id))
    p = Path(data_dir) / "bridge" / f"{rid}.jsonl"
    if not p.exists():
        return None
    return Bridge(rid, data_dir).posts()


# --------------------------------------------------------------------------- tools

def _who(ctx: Any) -> tuple[str, str]:
    name = str(getattr(ctx, "name", "") or "agent")
    role = ""
    core = getattr(ctx, "core", None)
    info = (getattr(core, "active", None) or {}).get(name) if core is not None else None
    if isinstance(info, dict):
        role = str(info.get("role") or "")
    return name, role


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_bridge_post(ctx: Any, args: dict[str, Any]) -> str:
    b = for_core(ctx.core, ctx.run_id)
    name, role = _who(ctx)
    try:
        p = b.post(name, args.get("kind") or "finding", args.get("text", ""), refs=args.get("refs"), role=role)
    except BridgeError as e:
        return f"ERROR: {e}"
    return f"posted {p['kind']} [{p['id']}] to the run bridge ({len(b)} posts)"


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_bridge_read(ctx: Any, args: dict[str, Any]) -> str:
    b = for_core(ctx.core, ctx.run_id)
    from .argnum import tool_int
    limit = tool_int(args.get("limit") or 30, 30)
    posts = b.read(kind=args.get("kind"), agent=args.get("agent"), since=args.get("since"),
                   query=args.get("query"), limit=limit)
    return compact(posts)


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_bridge_reply(ctx: Any, args: dict[str, Any]) -> str:
    b = for_core(ctx.core, ctx.run_id)
    name, role = _who(ctx)
    try:
        p = b.reply(name, args.get("refs") or [], args.get("text", ""), kind=args.get("kind") or "answer",
                    role=role)
    except BridgeError as e:
        return f"ERROR: {e}"
    return f"replied {p['kind']} [{p['id']}] re {', '.join(p['refs'])}"


BRIDGE_TOOLS: list[tuple[str, str, str, Any, dict[str, dict[str, str]]]] = [
    ("bridge_post", "post to this run's shared bridge (every agent in the run sees it): kind finding|question|"
     "answer|correction|conclusion (default finding); refs = ids of posts it builds on.",
     '{"text": str, "kind"?: str, "refs"?: [str]}', t_bridge_post,
     {"required": {"text": "str"}, "optional": {"kind": "str", "refs": "list|str"}}),
    ("bridge_read", "read this run's shared bridge, oldest first, compact; filter by kind, agent, since "
     "(a post id) and/or query substring.",
     '{"kind"?: str, "agent"?: str, "since"?: str, "query"?: str, "limit"?: int}', t_bridge_read,
     {"optional": {"kind": "str", "agent": "str", "since": "str", "query": "str", "limit": "int"}}),
    ("bridge_reply", "reply on the run bridge to specific posts (refs required); kind answer (default), "
     "correction or conclusion.",
     '{"refs": [str], "text": str, "kind"?: str}', t_bridge_reply,
     {"required": {"refs": "list|str", "text": "str"}, "optional": {"kind": "str"}}),
]


def register_bridge_tools(registry: Any) -> None:
    for name, desc, args_doc, fn, schema in BRIDGE_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin", schema=schema))


# --------------------------------------------------------------------------- recursive learning
#
# At run end up to AVRONE_LESSONS_K (default 5) conclusions (then corrections) from the bridge
# are distilled into the ``lessons`` memstore namespace (data_dir/memory.jsonl), deduped by a
# hash of their normalized text and capped at AVRONE_LESSONS_MAX (default 200, oldest evicted).
# New runs inject the most relevant lessons (keyword overlap with the task, <= 2000 chars) into
# their worker prompts. AVRONE_LESSONS=off disables both.

LESSONS_NS = "lessons"
LESSONS_CHARS = 2000
LESSON_TEXT = 500
_STOP = frozenset("the and for with that this from what are was were has have had not but you your "
                  "our its their they them into over than then can will should would could about "
                  "which when where who how why all any each more most some such only also very "
                  "use used using does did done just like make made task agent agents answer".split())


def lessons_enabled() -> bool:
    return os.getenv("AVRONE_LESSONS", "on").strip().lower() not in ("off", "0", "false", "no", "disabled")


def lessons_max() -> int:
    return _int_env("AVRONE_LESSONS_MAX", 200, 1, 5000)


def lessons_k() -> int:
    return _int_env("AVRONE_LESSONS_K", 5, 1, 20)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower()).strip(" .;:!\t\n")


def lesson_key(text: str) -> str:
    import hashlib
    # surrogatepass: a lone surrogate (cut emoji) raised UnicodeEncodeError here, AFTER research_note
    # had already posted, so the agent saw ERROR, retried, and the note was posted twice
    return "l-" + hashlib.sha1(_norm(text).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", str(text).lower()) if w not in _STOP}


_LESSON_LOCKS: dict[str, threading.RLock] = {}


def lessons_lock(data_dir: Path | str) -> threading.RLock:
    """One lock per data_dir guarding lesson load-check-save (shared by research_note)."""
    key = str(Path(data_dir).resolve())
    with _LOCKS_GUARD:
        return _LESSON_LOCKS.setdefault(key, threading.RLock())


def _store(data_dir: Path | str):
    from .memstore import MemoryStore
    return MemoryStore(Path(data_dir) / "memory.jsonl")


def distill_lessons(data_dir: Path | str, bridge: "Bridge", task: str = "", k: int | None = None) -> list[str]:
    """Save up to k of the bridge's conclusions (then corrections) as lessons; returns new keys."""
    k = lessons_k() if k is None else max(0, int(k))
    posts = bridge.posts()
    picks: list[str] = []
    seen: set[str] = set()
    for kind in ("conclusion", "correction"):
        for p in reversed([p for p in posts if p["kind"] == kind]):
            t = re.sub(r"\s+", " ", p["text"]).strip()[:LESSON_TEXT]
            key = lesson_key(t)
            if t and key not in seen:
                seen.add(key)
                picks.append(t)
            if len(picks) >= k:
                break
        if len(picks) >= k:
            break
    if not picks:
        return []
    with lessons_lock(data_dir):  # load-check-save is one step, so concurrent runs never both "learn" a key
        store = _store(data_dir)
        have = store.load(LESSONS_NS)
        saved: list[str] = []
        for t in picks:
            key = lesson_key(t)
            if key in have or key in saved:
                continue
            store.save(LESSONS_NS, key, t, run_id=bridge.run_id, task=str(task)[:200])
            saved.append(key)
        if saved:
            store.trim(LESSONS_NS, lessons_max())
    return saved


def relevant_lessons(data_dir: Path | str, task: str, max_chars: int = LESSONS_CHARS) -> list[str]:
    """Lessons sharing keywords with `task`, best overlap first (newest breaks ties), within max_chars."""
    try:
        recs = list(_store(data_dir).load(LESSONS_NS).values())
    except OSError:
        return []
    want = _words(task)
    if not want:
        return []
    scored = []
    for r in recs:
        s = len(want & _words(r.get("value", "")))
        if s:
            scored.append((s, float(r.get("ts", 0)), str(r.get("value", ""))))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out: list[str] = []
    used = 0
    for _, _, v in scored:
        line = f"- {v}"
        if used + len(line) + 1 > max_chars:
            continue
        out.append(v)
        used += len(line) + 1
    return out


def lessons_block(data_dir: Path | str, task: str) -> str:
    """Text appended to a worker's focus ('' when off or nothing relevant)."""
    if not lessons_enabled():
        return ""
    try:
        found = relevant_lessons(data_dir, task)
    except Exception:  # noqa: BLE001 - learning must never sink a run
        return ""
    if not found:
        return ""
    return ("\n\nLESSONS FROM EARLIER RUNS (agents concluded these before; reuse what applies, verify "
            "what matters):\n" + "\n".join(f"- {v}" for v in found))


# --------------------------------------------------------------------------- cross-examination

REVISE_PROMPT = """You are the CROSS-EXAMINATION agent {label} in AVRONE (round {rnd}). You and other
agents worked the same task in parallel from different angles. Read their results and the shared
bridge posts, then cross-check them against your own result:
- post a "correction" only for a real error (yours or another agent's): say who and what is wrong
  and the fix; cite post ids in refs when you can;
- post a "conclusion" for each point you now agree on with the others;
- post a "question" for a disagreement you cannot settle, or an "answer" to an open question.
If your own result changes, give the complete revised result; otherwise leave "revised" empty.
You have no tools and this is your only turn. Reply with JSON only:
{{"posts": [{{"kind": "correction|conclusion|question|answer", "text": "...", "refs": ["<post id>"]}}], "revised": ""}}"""

_REVISE_KINDS = ("correction", "conclusion", "question", "answer", "finding")


def parse_revise(reply: Any) -> tuple[list[dict[str, Any]], str]:
    """(posts, revised) from a cross-exam reply; junk yields ([], '')."""
    text = reply if isinstance(reply, str) else ""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"\{.*\}", text, re.S)
    if not m:
        return [], ""
    try:
        obj = json.loads(m.group(1) if m.re.groups else m.group(0))
    except (json.JSONDecodeError, RecursionError):
        return [], ""
    if not isinstance(obj, dict):
        return [], ""
    posts = []
    for p in obj.get("posts") if isinstance(obj.get("posts"), list) else []:
        if isinstance(p, dict) and str(p.get("kind", "")).lower() in _REVISE_KINDS and str(p.get("text") or "").strip():
            posts.append({"kind": str(p["kind"]).lower(), "text": str(p["text"]), "refs": _refs(p.get("refs"))})
        if len(posts) >= 10:
            break
    revised = obj.get("revised")
    return posts, revised.strip() if isinstance(revised, str) else ""


# --------------------------------------------------------------------------- journal scrub
class _JournalRef:
    """Just enough of a Bridge for _file_lock: the journal path."""
    def __init__(self, path: Path):
        self.path = path


def fix_journal(path: Path | str, core: Any = None) -> bool:
    """Round 158: redact a bridge journal written before v0.147 (posts were stored raw: a finding
    quoting "token=..." went to every peer and the fusion bus). Lines are redacted value by value
    and dumped again, so the journal stays valid JSONL; bytes that are not UTF-8 are kept. Runs
    under the same thread lock and cross-process file lock as append and compaction, then
    reloads any open Bridge for this journal, whose ring still held the raw text in memory.
    True when the journal was rewritten. A journal over JOURNAL_READ_MAX is left alone."""
    from .safeio import atomic_write, read_regular
    from .tools_secrets import _redact_line, _kinds, _line_hides_secret
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.suffix != ".jsonl":
        return False
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(p.resolve()), threading.RLock())
    with lock:
        with Bridge._file_lock(_JournalRef(p)):  # type: ignore[arg-type]
            try:
                if p.stat().st_size > JOURNAL_READ_MAX:
                    return False
            except OSError:
                return False
            raw = read_regular(p, JOURNAL_READ_MAX)
            if raw is None:
                return False
            text = raw.decode("utf-8", errors="surrogateescape")
            out = [(_redact_line(ln) if _kinds(ln) or _line_hides_secret(ln) else ln) for ln in text.split("\n")]
            new = "\n".join(out)
            if new == text:
                return False
            atomic_write(p, new.encode("utf-8", errors="surrogateescape"), fsync=True)
        cache = getattr(core, "_bridges", None) if core is not None else None
        for b in list(cache.values()) if cache else []:
            try:
                if b.path is not None and b.path.resolve() == p.resolve():
                    b._load()
            except OSError:
                pass
    return True
