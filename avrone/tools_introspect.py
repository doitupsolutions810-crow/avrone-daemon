"""Read-only introspection tools: let an agent look at its own tooling and settings.

    inspect_tool {"name"?: str, "query"?: str}
        name  -> full description, args_doc, source, arg schema and defining module of ONE tool
        query -> bounded list of tools whose name/description contains the substring
    env_report {"prefix"?: str, "format"?: "text"|"json"}
        effective Settings fields + AVRONE_* environment variables, REDACTED: secret-shaped
        names (TOKEN/KEY/SECRET/PASSWORD/...) report presence only, URL credentials and
        secret query params are masked, and token-shaped values are hidden whatever their name.

Nothing here writes, spawns, runs a command or touches the network. Output is capped.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re

from . import textsafe as _textsafe
from typing import TYPE_CHECKING, Any

from . import secretshapes as _shapes
from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

INSPECT_MAX_MATCHES = 50
INSPECT_MAX_QUERY = 200
INSPECT_MAX_DESC = 4000
INSPECT_MAX_CHARS = 8000
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_\-.]{1,128}")


def _registry(ctx: "AgentContext") -> Any:
    reg = getattr(ctx.core, "tools", None)
    if reg is None or not callable(getattr(reg, "get", None)) or not callable(getattr(reg, "names", None)):
        return None
    return reg


def _cap(text: str, limit: int = INSPECT_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _schema_lines(schema: Any) -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return ["schema: (none; args are not pre-validated)"]
    out = []
    for part in ("required", "optional"):
        spec = schema.get(part) or {}
        if isinstance(spec, dict) and spec:
            out.append(f"{part}: " + ", ".join(f"{k}: {v}" for k, v in spec.items()))
    return out or ["schema: (empty)"]


async def t_inspect_tool(ctx: "AgentContext", args: dict[str, Any]) -> str:
    reg = _registry(ctx)
    if reg is None:
        return "ERROR: the tool registry is not available"
    name = str(args.get("name") or "").strip()
    query = str(args.get("query") or "").strip()
    if bool(name) == bool(query):
        return "ERROR: give exactly one of name or query"
    if name:
        if not _TOOL_NAME_RE.fullmatch(name):
            return "ERROR: invalid tool name"
        tool = reg.get(name)
        if tool is None:
            close = reg.suggest(name) if callable(getattr(reg, "suggest", None)) else []
            return f"ERROR: unknown tool {name!r}." + (f" Did you mean: {', '.join(close)}?" if close else "")
        desc = str(tool.description)
        if len(desc) > INSPECT_MAX_DESC:
            desc = desc[:INSPECT_MAX_DESC] + "..."
        module = getattr(getattr(tool, "fn", None), "__module__", None) or "unknown"
        lines = [f"tool: {tool.name}", f"source: {tool.source}", f"module: {module}",
                 f"args: {tool.args_doc}", *_schema_lines(getattr(tool, "schema", None)),
                 f"description: {desc}"]
        return _cap("\n".join(lines))
    if len(query) > INSPECT_MAX_QUERY:
        return f"ERROR: query is longer than {INSPECT_MAX_QUERY} chars"
    q = query.lower()
    hits = []
    for n in reg.names():
        t = reg.get(n)
        if t is None:
            continue
        if q in t.name.lower() or q in str(t.description).lower():
            hits.append(t)
    if not hits:
        return f"No tools match {query!r} ({len(reg.names())} registered)."
    lines = [f"{len(hits)} tool(s) match {query!r}; showing {min(len(hits), INSPECT_MAX_MATCHES)}:"]
    for t in hits[:INSPECT_MAX_MATCHES]:
        d = re.sub(r"\s+", " ", str(t.description))
        lines.append(f"- {t.name} [{t.source}] {t.args_doc}: {d[:120]}{'...' if len(d) > 120 else ''}")
    if len(hits) > INSPECT_MAX_MATCHES:
        lines.append(f"...[{len(hits) - INSPECT_MAX_MATCHES} more; narrow the query]")
    return _cap("\n".join(lines))


# --------------------------------------------------------------------------- env_report

ENV_REPORT_MAX_VARS = 200
ENV_REPORT_MAX_VALUE = 200
ENV_REPORT_MAX_CHARS = 8000
ENV_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "CREDENTIAL",
                      "AUTH", "PRIVATE", "COOKIE", "SESSION", "SIGNING", "CERT")
_PREFIX_RE = re.compile(r"[A-Z0-9_]{0,64}")
# round 52: up to the LAST @ before the host; "u:p@ss@host" showed "ss@host"
_URL_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]{0,31}://)[^/\s]*@")
# zero-width / joiner / soft hyphen / BOM: invisible, so one inside a token or a secret NAME
# ("TOK\u200bEN", "ghp_A1\u200bb2...") hid it from every check while reading the same (round 52)
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e]")


def _detect_form(s: str) -> str:
    """The form secret checks run on: invisibles dropped, NFKC (fullwidth 'ＴＯＫＥＮ' -> 'TOKEN')."""
    import unicodedata
    return unicodedata.normalize("NFKC", _INVISIBLE_RE.sub("", s))
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:[a-z0-9_\-]*(?:token|key|secret|password|passwd|auth|sig|signature|session|code)"
    r"[a-z0-9_\-]*)=)[^&#\s]*")
# Values that look like credentials no matter what the variable is called.
_TOKENISH_RE = re.compile(
    r"(?i)(?:\bbearer\s+\S+|(?:sk|pk|rk|xai|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs])"
    r"[-_][A-Za-z0-9_\-]{12,}|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")


def env_secret_name(name: str) -> bool:
    up = _detect_form(str(name)).upper()
    return any(m in up for m in ENV_SECRET_MARKERS)


def redact_value(name: str, value: Any) -> str:
    """How one setting may appear in env_report. Never returns a secret value."""
    if env_secret_name(name):
        return "***set***" if value not in (None, "") else "unset"
    if value is None:
        return "unset"
    v = str(value)  # displayed as stored: dropping U+200D broke emoji ZWJ sequences (round 136 test)
    d = _detect_form(v)
    if (_TOKENISH_RE.search(v) or _shapes.TOKEN_PREFIX_RE.search(v)  # round 152
            or _TOKENISH_RE.search(d) or _shapes.TOKEN_PREFIX_RE.search(d)):
        return "***redacted (token-shaped value)***"
    v = _URL_USERINFO_RE.sub(r"\1***@", v)
    v = _SECRET_QUERY_RE.sub(r"\1***", v)
    v = _textsafe.UNSAFE_ONE.sub("?", v)
    return v[:ENV_REPORT_MAX_VALUE] + ("..." if len(v) > ENV_REPORT_MAX_VALUE else "")


def env_report(ctx: "AgentContext", prefix: str = "") -> dict[str, Any]:
    """Structured, redacted view of the settings in effect for this agent."""
    settings: dict[str, str] = {}
    s = getattr(ctx.core, "settings", None)
    if s is not None and dataclasses.is_dataclass(s):
        for f in dataclasses.fields(s):
            settings[f.name] = redact_value(f.name, getattr(s, f.name, None))
    full = "AVRONE_" + prefix if prefix and not prefix.startswith("AVRONE_") else (prefix or "AVRONE_")
    names = sorted(k for k in os.environ if k.startswith(full))
    env = {k: redact_value(k, os.environ.get(k)) for k in names[:ENV_REPORT_MAX_VARS]}
    if prefix:
        want = full[len("AVRONE_"):].lower()
        settings = {k: v for k, v in settings.items() if k.startswith(want)}
    return {"prefix": full, "settings": settings, "env": env, "env_total": len(names),
            "env_truncated": max(0, len(names) - ENV_REPORT_MAX_VARS),
            "redacted": sum(1 for v in [*settings.values(), *env.values()] if v.startswith("***"))}


async def t_env_report(ctx: "AgentContext", args: dict[str, Any]) -> str:
    prefix = str(args.get("prefix") or "").strip().upper()
    if not _PREFIX_RE.fullmatch(prefix):
        return "ERROR: prefix must be letters, digits or '_' (e.g. \"AVRONE_LLM\" or \"LLM\")"
    rep = env_report(ctx, prefix)
    if str(args.get("format", "text")).lower() == "json":
        return _cap(json.dumps(rep, indent=1, sort_keys=True), ENV_REPORT_MAX_CHARS)
    lines = [f"env_report ({rep['prefix']}*; read-only; secret values never shown, {rep['redacted']} redacted)",
             "settings in effect:"]
    lines += [f"  {k} = {v}" for k, v in rep["settings"].items()] or ["  (none)"]
    lines.append(f"environment ({rep['env_total']} {rep['prefix']}* vars):")
    lines += [f"  {k} = {v}" for k, v in rep["env"].items()] or ["  (none set)"]
    if rep["env_truncated"]:
        lines.append(f"  ...[{rep['env_truncated']} more not shown; narrow with prefix]")
    return _cap("\n".join(lines), ENV_REPORT_MAX_CHARS)


INTROSPECT_TOOLS: list[Tool] = [
    Tool("inspect_tool",
         "read-only: show ONE registered tool's full description, args, arg schema, source and module "
         "(name), or list tools whose name/description contains a substring (query, max 50).",
         '{"name"?: str, "query"?: str}', t_inspect_tool, source="builtin",
         schema={"optional": {"name": "str", "query": "str"}}),
    Tool("env_report",
         "read-only: the AVRONE settings in effect for you plus AVRONE_* env vars, redacted (secret-shaped "
         "names show set/unset only; URL credentials and token-shaped values masked); optional prefix filter.",
         '{"prefix"?: str, "format"?: "text"|"json"}', t_env_report, source="builtin",
         schema={"optional": {"prefix": "str", "format": "str"}}),
]


def register_introspect_tools(registry) -> None:
    for t in INTROSPECT_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
