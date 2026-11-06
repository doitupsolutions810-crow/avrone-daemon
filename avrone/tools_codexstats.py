"""codex_stats: a bounded, redacted aggregate of the calling agent's OWN run in the Eternal CodeX log.

    codex_stats {"scope"?: "self"|"run", "kind"?: str, "last"?: int, "format"?: "text"|"json"}
        scope "self" (default) -> only events this agent emitted in its run
        scope "run"            -> every event of the agent's own run (still never another run)
        kind                   -> optional kind prefix filter, e.g. "tool" or "fleet."
        last                   -> how many most-recent (timestamp, kind) pairs to list (0..50, default 10)

Only shapes are reported: counts by kind, counts of tool names used, error count, first/last
timestamps and the last N (time, kind[, tool]) tuples. Event payloads (prompts, tool args, tool
output, messages) are NEVER echoed, so nothing secret can leak through it. The run id comes from
the agent's context (never from args), the log path is confined to the codex log dir with symlink
and traversal checks, at most CODEX_STATS_MAX_BYTES of the log tail is read, and output is capped.
Nothing here writes, spawns, runs a command or touches the network.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

CODEX_STATS_MAX_BYTES = 4 * 1024 * 1024
CODEX_STATS_MAX_LAST = 50
CODEX_STATS_MAX_KINDS = 60
CODEX_STATS_MAX_CHARS = 8000
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_\-.]{1,128}")
_KIND_RE = re.compile(r"[A-Za-z0-9_.\-]{0,64}")
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.:\-/]")


def _label(v: Any, n: int = 64) -> str:
    """A kind / tool name made safe to print: limited charset, bounded length."""
    s = _SAFE_LABEL_RE.sub("?", str(v))
    return s[:n] or "?"


def _iso(ts: Any) -> str:
    try:
        f = float(ts)
    except (TypeError, ValueError, OverflowError):  # a 400-digit int ts
        return "?"
    if f != f or f in (float("inf"), float("-inf")) or f < 0:
        return "?"
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(f))
    except (OverflowError, ValueError, OSError):  # ts=1e308 / year > 9999 crashed codex_stats
        return "?"


def _log_path(ctx: "AgentContext") -> Path | None:
    codex = getattr(ctx.core, "codex", None)
    log_dir = getattr(codex, "log_dir", None)
    run_id = str(getattr(ctx, "run_id", "") or "")
    if log_dir is None or not _RUN_ID_RE.fullmatch(run_id) or run_id in (".", ".."):
        return None
    base = Path(log_dir)
    p = base / f"{run_id}.jsonl"
    try:
        if p.is_symlink() or not p.resolve().is_relative_to(base.resolve()):
            return None
    except OSError:
        return None
    return p


def _tail_bytes(p: Path, budget: int) -> tuple[bytes, bool]:
    """The last `budget` bytes of one file (no symlinks); (bytes, more_before)."""
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return b"", False
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        more = size > budget
        if more:
            f.seek(size - budget)
        return f.read(budget), more


def _read_tail(p: Path) -> tuple[list[dict[str, Any]], bool]:
    """Parsed events from at most the last CODEX_STATS_MAX_BYTES of the run's log, reading back
    into rotated archives when the active file is short (round 73: right after a rotation the
    active log held a handful of events and codex_stats reported the run as nearly empty)."""
    from .codex import log_chain
    chunks: list[bytes] = []
    budget = CODEX_STATS_MAX_BYTES
    truncated = False
    chain = log_chain(p)
    for i, src in enumerate(chain):
        raw, more = _tail_bytes(src, budget)
        budget -= len(raw)
        chunks.insert(0, raw if raw.endswith(b"\n") or not raw else raw + b"\n")
        if more or (budget <= 0 and i + 1 < len(chain)):
            truncated = True
            break
    lines = b"".join(chunks).decode("utf-8", errors="replace").splitlines()
    if truncated and lines:
        lines = lines[1:]  # first line is almost certainly cut mid-record
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out, truncated


def codex_stats(ctx: "AgentContext", scope: str = "self", kind: str = "", last: int = 10) -> dict[str, Any]:
    p = _log_path(ctx)
    if p is None:
        raise LookupError("the codex log for this run is not available")
    events, truncated = _read_tail(p)
    run_id = str(ctx.run_id)
    me = str(getattr(ctx, "name", ""))
    sel = []
    for ev in events:
        if str(ev.get("run_id", run_id)) != run_id:
            continue
        if scope == "self" and str(ev.get("agent", "")) != me:
            continue
        k = str(ev.get("kind", ""))
        if kind and not k.startswith(kind):
            continue
        sel.append(ev)
    kinds = Counter(_label(ev.get("kind", "")) for ev in sel)
    tools: Counter[str] = Counter()
    for ev in sel:
        if ev.get("kind") == "tool.call" and isinstance(ev.get("data"), dict):
            tools[_label(ev["data"].get("tool", "?"))] += 1
    stamps = [ev.get("ts") for ev in sel]
    recent = []
    for ev in sel[-last:] if last else []:
        row = {"ts": _iso(ev.get("ts")), "kind": _label(ev.get("kind", ""))}
        d = ev.get("data")
        if isinstance(d, dict) and str(ev.get("kind", "")).startswith("tool.") and "tool" in d:
            row["tool"] = _label(d.get("tool"))
        recent.append(row)
    top = kinds.most_common(CODEX_STATS_MAX_KINDS)
    return {"run_id": _label(run_id, 128), "scope": scope, "agent": _label(me) if scope == "self" else None,
            "kind_filter": kind or None, "events": len(sel),
            "by_kind": dict(top), "kinds_hidden": max(0, len(kinds) - len(top)),
            "tools_called": dict(tools.most_common(CODEX_STATS_MAX_KINDS)),
            "errors": sum(n for k, n in kinds.items() if k == "error" or k.endswith("error") or "refused" in k),
            "first": _iso(stamps[0]) if stamps else None, "last": _iso(stamps[-1]) if stamps else None,
            "recent": recent, "log_truncated": truncated}


def _int(v: Any, default: int, lo: int, hi: int) -> int | None:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _cap(text: str) -> str:
    return text if len(text) <= CODEX_STATS_MAX_CHARS else text[:CODEX_STATS_MAX_CHARS] + "\n...[truncated]"


async def t_codex_stats(ctx: "AgentContext", args: dict[str, Any]) -> str:
    scope = str(args.get("scope") or "self").strip().lower()
    if scope not in ("self", "run"):
        return "ERROR: scope must be \"self\" or \"run\""
    kind = str(args.get("kind") or "").strip()
    if not _KIND_RE.fullmatch(kind):
        return "ERROR: kind must be letters, digits, '.', '_' or '-' (max 64), e.g. \"tool\""
    last = _int(args.get("last"), 10, 0, CODEX_STATS_MAX_LAST)
    if last is None:
        return "ERROR: last must be an integer"
    try:
        rep = codex_stats(ctx, scope, kind, last)
    except LookupError as e:
        return f"ERROR: {e}"
    if str(args.get("format", "text")).lower() == "json":
        return _cap(json.dumps(rep, indent=1, sort_keys=True))
    who = f"agent {rep['agent']}" if scope == "self" else "whole run"
    lines = [f"codex_stats run {rep['run_id']} ({who}{', kind ' + kind + '*' if kind else ''}; "
             f"read-only, payloads never shown)",
             f"events: {rep['events']}  errors/refusals: {rep['errors']}  "
             f"first: {rep['first'] or '-'}  last: {rep['last'] or '-'}"]
    if rep["log_truncated"]:
        lines.append(f"(log larger than {CODEX_STATS_MAX_BYTES // (1024 * 1024)} MiB: only its tail was counted)")
    lines.append("by kind:")
    lines += [f"  {k}: {n}" for k, n in rep["by_kind"].items()] or ["  (none)"]
    if rep["kinds_hidden"]:
        lines.append(f"  ...[{rep['kinds_hidden']} more kinds]")
    if rep["tools_called"]:
        lines.append("tools called: " + ", ".join(f"{k} x{n}" for k, n in rep["tools_called"].items()))
    if rep["recent"]:
        lines.append(f"last {len(rep['recent'])}:")
        lines += [f"  {r['ts']} {r['kind']}" + (f" ({r['tool']})" if "tool" in r else "") for r in rep["recent"]]
    return _cap("\n".join(lines))


CODEX_STATS_TOOLS: list[Tool] = [
    Tool("codex_stats",
         "read-only: aggregate of YOUR OWN run's Eternal CodeX events (scope self = your events, run = "
         "your whole run): counts by kind, tools called, errors, first/last time and the last N "
         "(time, kind) pairs. Payloads are never shown; optional kind prefix filter.",
         '{"scope"?: "self"|"run", "kind"?: str, "last"?: int, "format"?: "text"|"json"}',
         t_codex_stats, source="builtin",
         schema={"optional": {"scope": "str", "kind": "str", "last": "int", "format": "str"}}),
]


def register_codex_stats_tools(registry) -> None:
    for t in CODEX_STATS_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
