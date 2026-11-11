"""Context-rich sub-agent spawning (ctxspawn).

``spawn`` used to hand a child only ``role`` + ``task``. Now the child also gets a
*ContextPack* built from the parent, chosen by the spawn arg ``context``:

    "full"     (default) the run's original task, a lineage chain (root -> ... -> parent),
               the parent's role/focus, the squad's sibling roles/focuses (inside a squad),
               the parent's last tool calls/results (bounded ring buffer on AgentContext),
               a read-only snapshot of the parent's memory namespace, and the names of the
               files in the parent's agent dir.
    "summary"  task + lineage + a compact digest (counts and tool names, no payloads).
    "none"     the old behaviour: role + task only.

Optional spawn args: ``focus`` (the child's focus line) and ``share_dir`` (bool, default
false): the child may READ the parent's agent dir through a ``parent/`` virtual prefix in
the file tools (see :mod:`avrone.fsunify`); writes there are refused.

Caps
  AVRONE_SPAWN_CONTEXT_CHARS   total pack size (default 12000, clamped to 200..64000).
                               Truncation is deterministic: task + lineage always come
                               first; later sections are cut (then dropped) in a fixed order.
  AVRONE_MAX_AGENTS_PER_RUN    agents one run may start in total (default 64, 1..4096);
                               a spawn past it returns an ERROR string.
Every pack is passed through :func:`redact` (token shapes, keys, Authorization headers,
``*_TOKEN/*_KEY/*_SECRET``-style assignments and the literal values of such env vars).
"""
from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import secretshapes as _shapes

if TYPE_CHECKING:
    from .trinity import AgentContext

MODES = ("full", "summary", "none")
DEFAULT_CONTEXT_CHARS, MIN_CONTEXT_CHARS, HARD_CONTEXT_CHARS = 12000, 200, 64000
DEFAULT_MAX_AGENTS, HARD_MAX_AGENTS = 64, 4096
RECENT_CALLS = 8          # ring buffer length on AgentContext
CALL_ARGS_CHARS = 300
CALL_OUT_CHARS = 600
MEMORY_ENTRIES = 20
MEMORY_VALUE_CHARS = 300
DIR_FILES = 50
DIR_SCAN_MAX = 20_000  # entries visited by _dir_files before it gives up
SUMMARY_CHARS = 2000
REDACTED = "[REDACTED]"
TRUNC = "...[truncated]"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def context_chars() -> int:
    return _env_int("AVRONE_SPAWN_CONTEXT_CHARS", DEFAULT_CONTEXT_CHARS, MIN_CONTEXT_CHARS, HARD_CONTEXT_CHARS)


def max_agents_per_run() -> int:
    return _env_int("AVRONE_MAX_AGENTS_PER_RUN", DEFAULT_MAX_AGENTS, 1, HARD_MAX_AGENTS)


def new_ring() -> deque:
    return deque(maxlen=RECENT_CALLS)


# --------------------------------------------------------------------------- redaction
_SECRET_WORD = r"(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|private[_-]?key|access[_-]?key|[a-z0-9]_key|credential)"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
     REDACTED),
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|x-admin-token)(\"?\s*[:=]\s*\"?)"
                r"(?:(bearer|basic|token)\s+)?[^\s,;\"']+"),
     lambda m: f"{m.group(1)}{m.group(2)}{(m.group(3) + ' ') if m.group(3) else ''}{REDACTED}"),  # type: ignore[list-item]
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {REDACTED}"),
    (None, None),  # key = value with a secret-ish key: _KVRule, filled in below (linear, no regex)
    (re.compile(r"\b(?:sk|pk|rk|xai)-[A-Za-z0-9_-]{16,}"), REDACTED),  # round 151: + xAI keys
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]{0,31}://)[^/@\s:]*:[^/@\s]+@"), r"\1***@"),  # round 153: redis://:pw@
    (_shapes.TOKEN_PREFIX_RE, REDACTED),  # round 152: vendor tokens (secretshapes)
] + [(rx, rep) for rx, rep, _g, _n in _shapes.CONTEXT_RULES]  # round 153: -u user:pass, --password x, ...


# Scheme regexes above are bounded ({0,31}) and unanchored: `\b[a-z][a-z0-9+.\-]*://` rescanned the
# rest of every "a.a.a..." run from each of its word boundaries (quadratic: 16 KB of minified JS in
# one tool result held the event loop for seconds, since every CodeX event is redacted on emit).

_IDENT_RE = re.compile(r"[\w.-]+")
_SECRET_RE = re.compile(_SECRET_WORD, re.I)
# round 153: "=>" and ":=" are separators too ("password => 'x'" masked only the ">")
_KV_TAIL_RE = re.compile(r"[\"']?\s*(?:=>|:=|[:=])\s*(?!\[REDACTED\])(\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)")


def _redact_kv(s: str) -> str:
    """Mask the value of `key = value` / `"key": "value"` when the key holds a secret word.

    Was one regex, `[\\w.-]*SECRET[\\w.-]*\\s*[:=]...`: on a long run of word chars and dots
    ("a.a.a...", minified JS, dotted versions) it backtracked from every boundary: 4 KB took
    0.7 s, 16 KB 11 s, 50 KB 99 s. Identifiers are now tokenised once (linear), and only tokens
    that contain a secret word and are followed by := are looked at."""
    out: list[str] = []
    last = 0
    for m in _IDENT_RE.finditer(s):
        if m.start() < last or not _SECRET_RE.search(m.group()):
            continue
        tail = _KV_TAIL_RE.match(s, m.end())
        if tail is None:
            continue
        out.append(s[last:tail.start(1)])
        out.append(REDACTED)
        last = tail.end(1)
    if not out:
        return s
    out.append(s[last:])
    return "".join(out)



class _KVRule:
    """Stands in _PATTERNS for the old key/value regex (same .search / .sub surface, so
    secret_scan's per-rule names still line up)."""
    pattern = "key=value with a secret word in the key"

    def search(self, s: str) -> bool:
        return _redact_kv(s) != s

    def sub(self, _rep: Any, s: str) -> str:
        return _redact_kv(s)


_PATTERNS[3] = (_KVRule(), None)  # type: ignore[assignment]


_ENV_CACHE: tuple[dict, list[str]] | None = None


def _env_secret_values() -> list[str]:
    """Secret-named env values, longest first. redact() runs this for every string of every CodeX
    event; it rebuilt the list from os.environ each time (decoding every variable and testing it
    against every marker): 751 us per call with 500 variables, round 83. The result is now cached
    and reused while the environment is unchanged (a C-level dict comparison against a snapshot),
    so a variable set or removed at runtime still takes effect on the next call."""
    global _ENV_CACHE
    raw = getattr(os.environ, "_data", None)
    src = raw if isinstance(raw, dict) else dict(os.environ)
    cache = _ENV_CACHE
    if cache is not None and len(cache[0]) == len(src) and cache[0] == src:
        return cache[1]
    from .tools_extra import is_secret_name
    vals = {v for k, v in os.environ.items() if is_secret_name(k) and v and len(v) >= 6}
    out = sorted(vals, key=len, reverse=True)
    _ENV_CACHE = (dict(src), out)
    return out


def redact(text: Any) -> str:
    """Mask secret-shaped values in `text`. Never raises."""
    s = "" if text is None else str(text)
    try:
        for v in _env_secret_values():
            s = s.replace(v, REDACTED)
    except Exception:  # noqa: BLE001
        pass
    low: str | None = None
    for i, (pat, rep) in enumerate(_PATTERNS):
        anyof = _ANY_GATES.get(i)
        if anyof is not None and not any(w in s for w in anyof):
            continue
        gate = _GATES[i] if i < len(_GATES) else None
        if gate is not None:
            need, words = gate
            if need and not all(w in s for w in need):
                continue
            if words:
                if low is None:
                    low = _fold(s)
                if not any(w in low for w in words):
                    continue
        t = pat.sub(rep, s)  # type: ignore[arg-type]
        if t is not s and t != s:
            s, low = t, None
    return s


# Literal gates (round 82). Every CodeX event string runs every rule on the event loop; ten full
# regex passes cost ~0.55 s per MB. Each rule can only match text that holds certain literals, and
# `in` is a C memchr-speed scan, so a rule whose literals are absent is skipped. `need`: all of these
# must be present (case-sensitive); `words`: at least one must be present in the case-folded text.
# Folding matches re.IGNORECASE: besides str.lower(), U+0130/U+0131 fold to i, U+017F to s and
# U+212A to k (the only non-ASCII code points re's (?i) matches against ASCII letters).
_FOLD_MAP = str.maketrans({"\u0130": "i", "\u0131": "i", "\u017f": "s", "\u212a": "k"})
_FOLD_CHARS = ("\u0130", "\u0131", "\u017f", "\u212a")


def _fold(s: str) -> str:
    if any(c in s for c in _FOLD_CHARS):
        s = s.translate(_FOLD_MAP)
    return s.lower()


_ANY_GATES: dict[int, tuple[str, ...]] = {
    4: ("sk-", "pk-", "rk-", "xai-"),
    5: ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    11: _shapes.PREFIX_GATE,
    **{12 + i: g for i, (_r, _p, g, _n) in enumerate(_shapes.CONTEXT_RULES)},
}
_KV_WORDS = ("token", "secret", "passw", "api", "private", "access", "_key", "credential")
_GATES: list[tuple[tuple[str, ...], tuple[str, ...]] | None] = [
    (("PRIVATE KEY-----",), ()),
    ((), ("authorization", "x-api-key", "x-admin-token")),
    ((), ("bearer",)),
    ((), _KV_WORDS),  # _redact_kv: the key must hold a secret word (and is followed by : or =)
    None,  # sk-/pk-/rk-/xai-: any-of, case-sensitive: checked in _any_gate
    None,  # gh?_ / github_pat_: same
    (("xox",), ()),
    (("AKIA",), ()),
    (("AIza",), ()),
    (("eyJ", "."), ()),
    (("://", "@"), ()),
]


# --------------------------------------------------------------------------- pack
@dataclass
class ContextPack:
    mode: str
    task: str
    lineage: list[str]
    sections: list[tuple[str, str]] = field(default_factory=list)  # (title, body), priority order

    def render(self, cap: int | None = None) -> str:
        """Deterministic, capped text: task + lineage first, then sections until the budget ends."""
        if self.mode == "none":
            return ""
        cap = context_chars() if cap is None else max(1, int(cap))
        head = (f"=== CONTEXT FROM YOUR PARENT (spawn context: {self.mode}) ===\n"
                f"## Original run task\n{redact(self.task)}\n"
                f"## Lineage (root -> parent)\n{redact(' -> '.join(self.lineage) or '(none)')}\n")
        if len(head) >= cap:
            return head[:max(0, cap - len(TRUNC))] + TRUNC
        out = head
        titles = [t for t, _ in self.sections]
        dropped: list[str] = []
        for i, (title, body) in enumerate(self.sections):
            block = f"## {title}\n{redact(body).rstrip()}\n"
            room = cap - len(out)
            if len(block) <= room:
                out += block
                continue
            # This section overflows: cut it if a useful piece fits next to the drop note,
            # else drop it too. Everything after it is dropped either way.
            rest = titles[i + 1:]
            note = _drop_note(rest)
            keep = room - len(note) - len(TRUNC) - 1
            if rest and keep < len(title) + 8:  # not even the note fits beside a cut: drop the note
                note, keep = "", room - len(TRUNC) - 1
            if keep >= len(title) + 8:
                out += block[:keep] + TRUNC + "\n"
                dropped = rest
            else:
                dropped = titles[i:]
            break
        note = _drop_note(dropped)
        if note and len(out) + len(note) <= cap:
            out += note
        return out[:cap]


def _drop_note(titles: list[str]) -> str:
    return f"[context truncated; dropped: {', '.join(titles)}]\n" if titles else ""


def lineage_of(ctx: Any) -> list[str]:
    """The child's lineage: the parent's own lineage plus the parent."""
    return list(getattr(ctx, "lineage", None) or []) + [str(getattr(ctx, "name", "?"))]


def _recent(ctx: Any) -> list[tuple[str, Any, str]]:
    return list(getattr(ctx, "recent_calls", None) or [])


def _short(v: Any, n: int) -> str:
    s = v if isinstance(v, str) else repr(v)
    return s if len(s) <= n else s[:n] + TRUNC


def _memory(ctx: Any) -> dict[str, dict[str, Any]]:
    try:
        from . import memstore
        from .tools_extra import _mem_file, _mem_ns
        space = memstore.MemoryStore(_mem_file(ctx)).load(_mem_ns(ctx))
    except Exception:  # noqa: BLE001 - context is best effort
        return {}
    items = sorted(space.items(), key=lambda kv: float(kv[1].get("ts", 0) or 0), reverse=True)
    return dict(items[:MEMORY_ENTRIES])


def _dir_files(ctx: Any) -> list[str]:
    d = getattr(ctx, "agent_dir", None)
    if d is None:
        return []
    root = Path(d)
    names: list[str] = []
    # Depth-first over each directory's entries sorted by name: the same order sorted(rglob("*"))
    # gave, but it stops at DIR_FILES instead of listing and sorting the whole tree first. A
    # 100,000-file agent dir took ~6 s per spawn (round 79). Symlinked dirs are not entered, and
    # DIR_SCAN_MAX entries bound a tree of empty dirs.
    stack: list[tuple[str, list[os.DirEntry]]] = []
    seen = 0

    def listing(path: str) -> list[os.DirEntry]:
        try:
            with os.scandir(path) as it:
                return sorted(it, key=lambda e: e.name, reverse=True)
        except OSError:
            return []
    stack.append(("", listing(str(root))))
    while stack and seen < DIR_SCAN_MAX:
        prefix, entries = stack[-1]
        if not entries:
            stack.pop()
            continue
        e = entries.pop()
        seen += 1
        rel = f"{prefix}{e.name}"
        try:
            if e.is_dir(follow_symlinks=False):
                stack.append((rel + "/", listing(e.path)))
            elif e.is_file():
                names.append(rel)
                if len(names) >= DIR_FILES:
                    names.append("...")
                    break
        except OSError:
            continue
    return names


def _siblings(ctx: Any) -> list[dict[str, str]]:
    squads = getattr(getattr(ctx, "core", None), "squad_roles", None) or {}
    return list(squads.get(getattr(ctx, "run_id", None)) or [])


def build_pack(ctx: Any, mode: str) -> ContextPack:
    task = str(getattr(ctx, "run_task", None) or "")
    lineage = lineage_of(ctx)
    pack = ContextPack(mode, task, lineage)
    if mode == "none":
        return pack
    role, focus = getattr(ctx, "role", None) or "?", getattr(ctx, "focus", None) or ""
    recent, mem, files, sibs = _recent(ctx), _memory(ctx), _dir_files(ctx), _siblings(ctx)
    if mode == "summary":
        tools = ", ".join(t for t, _, _ in recent) or "none"
        digest = (f"parent {getattr(ctx, 'name', '?')} (role {role}); focus: {_short(focus, 300)}\n"
                  f"parent's recent tool calls ({len(recent)}): {tools}\n"
                  f"parent memory keys: {len(mem)}; files in parent agent dir: {len([f for f in files if f != '...'])}"
                  f"{'+' if '...' in files else ''}; squad siblings: {len(sibs)}")
        pack.sections.append(("Digest", digest[:SUMMARY_CHARS]))
        return pack
    pack.sections.append(("Parent", f"name: {getattr(ctx, 'name', '?')}\nrole: {role}\nfocus: {focus or '(same as task)'}"))
    if sibs:
        pack.sections.append(("Squad siblings", "\n".join(
            f"- {s.get('role')}: {_short(s.get('focus', ''), 300)}" for s in sibs)))
    if recent:
        pack.sections.append(("Parent's recent tool calls (oldest first)", "\n".join(
            f"- {t} {_short(a, CALL_ARGS_CHARS)}\n  -> {_short(o, CALL_OUT_CHARS)}" for t, a, o in recent)))
    if mem:
        pack.sections.append(("Parent memory (read-only snapshot)", "\n".join(
            f"- {k}: {_short(str(r.get('value', '')), MEMORY_VALUE_CHARS)}" for k, r in mem.items())))
    if files:
        pack.sections.append(("Files in parent agent dir", "\n".join(files)))
    return pack


def child_agent_id(ctx: Any, role: str) -> str:
    from . import agentfs
    base = getattr(ctx, "agent_id", None) or getattr(ctx, "name", "") or "agent"
    return agentfs.sanitize_agent_id(f"{base}-{role}")


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


async def spawn(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Body of the ``spawn`` tool (depth already checked by the caller)."""
    core = ctx.core
    cap = max_agents_per_run()
    started = int((getattr(core, "run_agents", None) or {}).get(ctx.run_id, 0))
    if started >= cap:
        return (f"ERROR: agent cap reached for this run ({started}/{cap}, AVRONE_MAX_AGENTS_PER_RUN); "
                "do this part yourself.")
    mode = str(args.get("context") or "full").strip().lower()
    if mode not in MODES:
        return f"ERROR: context must be one of {', '.join(MODES)}; got {mode!r}"
    role = str(args.get("role") or "Helper")
    task = str(args.get("task", ""))
    focus = args.get("focus")
    focus = str(focus) if focus not in (None, "") else None
    pack_text = build_pack(ctx, mode).render() if mode != "none" else ""
    parent_dir = getattr(ctx, "agent_dir", None) if _bool(args.get("share_dir", False)) else None
    return await core.run_agent(
        run_id=ctx.run_id, role=role, task=task, focus=focus, parent=ctx.name, depth=ctx.depth + 1,
        agent_id=child_agent_id(ctx, role), lineage=lineage_of(ctx),
        run_task=getattr(ctx, "run_task", None) or task, context_pack=pack_text,
        context_mode=mode, parent_dir=parent_dir, **_cap_kw(ctx))


def _cap_kw(ctx: Any) -> dict[str, Any]:
    """capgate: hand the child an attenuation of this agent's capability (nothing when off), so
    run_agent stubs that predate the gate keep working."""
    from . import capgate
    if capgate.mode() == "off":
        return {}
    return {"parent_capability": capgate.capability_for(ctx)}
