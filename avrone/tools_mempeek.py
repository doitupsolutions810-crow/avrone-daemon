"""memory_peek: a read-only, bounded, redacted view of the calling agent's OWN memory namespace.

    memory_peek {"limit"?: int, "prefix"?: str, "value_chars"?: int, "format"?: "text"|"json"}
        limit        newest entries to show (1..50, default 10)
        prefix       only keys starting with this (max 200 chars)
        value_chars  value preview length (0..500, default 120; 0 = keys only)

Reports the namespace, entry count against the per-namespace cap, total value bytes, the
oldest/newest save time and the newest entries with truncated value previews. The namespace is
always the agent's own (memstore.agent_namespace / ctx.memory_namespace, never a 'fleet:...' or
another agent's): no argument can choose it. Values under secret-shaped keys (TOKEN/KEY/SECRET/...)
show only that they are set, token-shaped values (incl. a token prefix glued to other text and
long mixed-case+digit blobs) are hidden whatever their key, URL credentials are
masked, control characters are replaced. Redaction runs on the FULL value before truncation so a
cut can never expose part of a secret. Output is capped. Nothing here writes, spawns or networks.
"""
from __future__ import annotations

import json
import re

from . import textsafe as _textsafe
import time
from typing import TYPE_CHECKING, Any

from .tools import Tool
from .tools import off_loop
from .tools_introspect import redact_value

if TYPE_CHECKING:
    from .trinity import AgentContext

PEEK_MAX_LIMIT = 50
PEEK_MAX_VALUE_CHARS = 500
PEEK_MAX_KEY_CHARS = 120
PEEK_MAX_PREFIX = 200
PEEK_MAX_CHARS = 8000
# Extra memory-value checks on top of redact_value: a known token prefix glued to other text (no word
# boundary, e.g. "xxsk-..."), and long mixed-case+digit blobs that look like keys/hashes/JWT parts.
_EMBEDDED_TOKEN_RE = re.compile(
    r"(?i)(?:sk|pk|rk|xai|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs])[-_][A-Za-z0-9_\-]{12,}")
_BLOB_RE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")


def _secretish(v: str) -> bool:
    from .tools_introspect import _detect_form
    v = _detect_form(v)  # round 52: a zero-width char inside a token hid it from both checks
    if _EMBEDDED_TOKEN_RE.search(v):
        return True
    for m in _BLOB_RE.finditer(v):
        b = m.group(0)
        if any(c.isdigit() for c in b) and any(c.islower() for c in b) and any(c.isupper() for c in b):
            return True
    return False


def _iso(ts: Any) -> str:
    try:
        f = float(ts)
    except (TypeError, ValueError, OverflowError):  # a 400-digit int ts raised OverflowError
        return "?"
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return "?"
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(f))
    except (OverflowError, ValueError, OSError):  # ts=1e308 / year > 9999 crashed memory_peek
        return "?"


def _clean(s: str, n: int) -> str:
    s = _textsafe.UNSAFE_ONE.sub("?", s)
    s = re.sub("[\ud800-\udfff]", "\ufffd", s)  # round 52: a lone surrogate broke UTF-8 transports
    return s if len(s) <= n else s[:n] + "..."


def _nbytes(v: Any) -> int:
    """UTF-8 size of a stored value. Round 52: a lone surrogate (a hand-edited or peer-synced
    memory.jsonl holding "\\ud800") raised UnicodeEncodeError and failed the whole peek."""
    return len(str(v).encode("utf-8", "surrogatepass"))


def _preview(key: str, value: Any, n: int) -> str:
    v = redact_value(key, value if value not in (None,) else "")
    if v.startswith("***") or v == "unset":
        return v
    if _secretish(str(value)):
        return "***redacted (token-shaped value)***"
    return _clean(v, n)


def _int(v: Any, default: int, lo: int, hi: int) -> int | None:
    from .argnum import tool_int  # round 45: one rule for inf, NaN, "1_000", Unicode digits
    return tool_int(v, default, lo, hi, bad=None)


def memory_peek(ctx: "AgentContext", limit: int = 10, prefix: str = "", value_chars: int = 120) -> dict[str, Any]:
    from . import memstore
    from .tools_extra import _mem_file, _mem_ns
    ns = _mem_ns(ctx)
    if not memstore.is_local(ns):  # defence in depth: _mem_ns only ever returns a local namespace
        raise PermissionError("not a local namespace")
    mem = memstore.MemoryStore(_mem_file(ctx)).load(ns)
    if not isinstance(mem, dict):
        mem = {}
    # round 52: a non-dict record raised AttributeError out of the tool
    recs = [r for r in mem.values() if isinstance(r, dict) and str(r.get("key", "")).startswith(prefix)]
    recs.sort(key=lambda r: memstore._ts(r.get("ts")), reverse=True)
    stamps = [memstore._ts(r.get("ts")) for r in recs]
    total_bytes = sum(_nbytes(r.get("value", "")) for r in recs)
    entries = []
    for r in recs[:limit]:
        k = str(r.get("key", ""))
        e = {"key": _clean(k, PEEK_MAX_KEY_CHARS), "saved": _iso(r.get("ts")),
             "bytes": _nbytes(r.get("value", ""))}
        if value_chars:
            e["value"] = _preview(k, r.get("value", ""), value_chars)
        entries.append(e)
    return {"namespace": ns, "entries_total": len(mem), "cap": memstore.max_entries(),
            "prefix": prefix or None, "matching": len(recs), "value_bytes": total_bytes,
            "newest": _iso(stamps[0]) if stamps else None, "oldest": _iso(stamps[-1]) if stamps else None,
            "shown": entries, "more": max(0, len(recs) - limit)}


def _cap(text: str) -> str:
    return text if len(text) <= PEEK_MAX_CHARS else text[:PEEK_MAX_CHARS] + "\n...[truncated]"


@off_loop
async def t_memory_peek(ctx: "AgentContext", args: dict[str, Any]) -> str:
    limit = _int(args.get("limit"), 10, 1, PEEK_MAX_LIMIT)
    value_chars = _int(args.get("value_chars"), 120, 0, PEEK_MAX_VALUE_CHARS)
    if limit is None or value_chars is None:
        return "ERROR: limit and value_chars must be integers"
    raw_prefix = args.get("prefix")
    if raw_prefix is not None and not isinstance(raw_prefix, str):
        return "ERROR: prefix must be text"  # round 52: {"a": 1} was str()-ed into a prefix
    prefix = raw_prefix or ""
    if len(prefix) > PEEK_MAX_PREFIX:
        return f"ERROR: prefix is longer than {PEEK_MAX_PREFIX} chars"
    rep = memory_peek(ctx, limit, prefix, value_chars)
    fmt = args.get("format", "text")
    if isinstance(fmt, str) and fmt.strip().lower() == "json":
        out = json.dumps(rep, indent=1, sort_keys=True)
        if len(out) > PEEK_MAX_CHARS:  # round 52: a cut indented dump was not parseable JSON
            out = json.dumps(rep, sort_keys=True, separators=(",", ":"))
        while len(out) > PEEK_MAX_CHARS and rep["shown"]:
            rep["shown"] = rep["shown"][:-1]
            rep["more"] += 1
            out = json.dumps(rep, sort_keys=True, separators=(",", ":"))
        return out
    head = (f"memory_peek namespace {rep['namespace']} (your own; read-only, secrets redacted): "
            f"{rep['entries_total']}/{rep['cap']} entries")
    if not rep["entries_total"]:
        return head + "\nno memories saved yet."
    lines = [head]
    if prefix:
        lines.append(f"prefix {_clean(prefix, 60)!r}: {rep['matching']} matching")
    lines.append(f"value bytes: {rep['value_bytes']}  newest: {rep['newest'] or '-'}  oldest: {rep['oldest'] or '-'}")
    for e in rep["shown"]:
        row = f"- {e['key']} ({e['bytes']} B, {e['saved']})"
        if "value" in e:
            row += f": {e['value']}"
        lines.append(row)
    if rep["more"]:
        lines.append(f"...[{rep['more']} more; raise limit or narrow with prefix]")
    return _cap("\n".join(lines))


MEMPEEK_TOOLS: list[Tool] = [
    Tool("memory_peek",
         "read-only: bounded view of YOUR OWN memory namespace: entry count vs cap, total bytes, "
         "newest/oldest save time and the newest entries with truncated, redacted value previews "
         "(secret-shaped keys and token-shaped values are masked). Optional key prefix filter.",
         '{"limit"?: int, "prefix"?: str, "value_chars"?: int, "format"?: "text"|"json"}',
         t_memory_peek, source="builtin",
         schema={"optional": {"limit": "int", "prefix": "str", "value_chars": "int", "format": "str"}}),
]


def register_mempeek_tools(registry) -> None:
    for t in MEMPEEK_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
