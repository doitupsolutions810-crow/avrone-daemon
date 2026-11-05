"""Extra agent tooling: search, python, file navigation/editing, HTTP, git, memory,
artifacts, docker and a think scratchpad.

Usage:
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(registry)   # registry: avrone.tools.ToolRegistry

New options (all optional, read via env / getattr so config.py needs no change):
    AVRONE_SEARCH_PROVIDER  auto | duckduckgo | brave | tavily   (default auto)
    BRAVE_API_KEY           enables Brave Search API (auto picks it when set)
    TAVILY_API_KEY          enables Tavily Search API (auto picks it when Brave is not set)
    AVRONE_MEMORY_MAX_ENTRIES / AVRONE_MEMORY_MAX_VALUE / AVRONE_MEMORY_MAX_NAMESPACES
                            per-namespace memory caps (see avrone.memstore; hard ceilings apply)
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import html as _html
import json
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import fsunify, memstore
from .tools import Tool, _safe_path, runner_accepts_cwd

if TYPE_CHECKING:
    from .trinity import AgentContext

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
USER_AGENT = "Mozilla/5.0 (compatible; AVRONE/0.2)"

# Tests (or embedders) may set this to an httpx transport, e.g. httpx.MockTransport.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _client(timeout: float = 30) -> httpx.AsyncClient:
    kw: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if HTTP_TRANSPORT is not None:
        kw["transport"] = HTTP_TRANSPORT
    return httpx.AsyncClient(**kw)


def _setting(ctx: "AgentContext", name: str, default: Any) -> Any:
    return getattr(ctx.core.settings, name, default)


def _ws(ctx: "AgentContext") -> Path:
    return Path(ctx.core.runner.workspace).resolve()


def _rel(ctx: "AgentContext", p: Path) -> str:
    try:
        return str(p.resolve().relative_to(_ws(ctx))) or "."
    except ValueError:
        return str(p)


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    codex = getattr(ctx.core, "codex", None)
    if codex is not None:
        try:
            codex.emit(ctx.run_id, kind, ctx.name, **data)
        except Exception:
            pass


# --------------------------------------------------------------------------- web_search

def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _ddg_url(href: str) -> str:
    href = _html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    u = urlparse(href)
    if "duckduckgo.com" in u.netloc and u.path.startswith("/l/"):
        q = parse_qs(u.query).get("uddg")
        if q:
            return unquote(q[0])
    return href


def parse_ddg_html(text: str, max_results: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    links = list(re.finditer(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', text, re.S))
    for i, m in enumerate(links):
        tag = m.group(0)
        href_m = re.search(r'href="([^"]+)"', tag)
        if not href_m:
            continue
        url = _ddg_url(href_m.group(1))
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        end = links[i + 1].start() if i + 1 < len(links) else len(text)
        seg = text[m.end():end]
        sn = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>', seg, re.S)
        results.append({"title": _strip_tags(m.group(1)), "url": url,
                        "snippet": _strip_tags(sn.group(1)) if sn else ""})
        if len(results) >= max_results:
            break
    return results


async def _search_ddg(q: str, n: int) -> list[dict[str, str]]:
    async with _client() as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": q},
                         headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return parse_ddg_html(r.text, n)


async def _search_brave(q: str, n: int, key: str) -> list[dict[str, str]]:
    async with _client() as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q": q, "count": n},
                        headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    items = (r.json().get("web") or {}).get("results") or []
    return [{"title": _strip_tags(i.get("title", "")), "url": i.get("url", ""),
             "snippet": _strip_tags(i.get("description", ""))} for i in items[:n]]


async def _search_tavily(q: str, n: int, key: str) -> list[dict[str, str]]:
    async with _client() as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": q, "max_results": n},
                         headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    items = r.json().get("results") or []
    return [{"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": (i.get("content") or "")[:400]} for i in items[:n]]


async def t_web_search(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "allow_network", True):
        return "ERROR: network access is disabled."
    q = str(args.get("query", "")).strip()
    if not q:
        return "ERROR: query is required"
    n = _int(args.get("max_results"), 5, 1, 20)
    provider = str(_setting(ctx, "search_provider", "") or os.getenv("AVRONE_SEARCH_PROVIDER", "auto")).lower()
    brave = str(_setting(ctx, "brave_api_key", "") or os.getenv("BRAVE_API_KEY", ""))
    tavily = str(_setting(ctx, "tavily_api_key", "") or os.getenv("TAVILY_API_KEY", ""))
    if provider == "auto":
        provider = "brave" if brave else "tavily" if tavily else "duckduckgo"
    if provider == "brave":
        if not brave:
            return "ERROR: AVRONE_SEARCH_PROVIDER=brave but BRAVE_API_KEY is not set"
        results = await _search_brave(q, n, brave)
    elif provider == "tavily":
        if not tavily:
            return "ERROR: AVRONE_SEARCH_PROVIDER=tavily but TAVILY_API_KEY is not set"
        results = await _search_tavily(q, n, tavily)
    else:
        provider = "duckduckgo"
        results = await _search_ddg(q, n)
    if not results:
        return f"No results for {q!r} ({provider})."
    lines = [f"Search results for {q!r} ({provider}):"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}" + (f"\n   {r['snippet']}" if r["snippet"] else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- python

async def t_python(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "agents_can_shell", True):
        return "ERROR: code execution is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    code = str(args.get("code", ""))
    if not code.strip():
        return "ERROR: code is required"
    runner = ctx.core.runner
    root = fsunify.agent_root(ctx)
    if root is not None and runner_accepts_cwd(runner):
        # run with cwd = own agent dir; the snippet lives there too so it is reachable from
        # the local runner, `docker exec -w /workspace/agents/<id>` and the isolated /agent mount
        f = root / f".snippet_{uuid.uuid4().hex[:10]}.py"
        f.write_text(code)
        try:
            res = await runner.run(f"python3 {shlex.quote(f.name)}", cwd=root)
        finally:
            f.unlink(missing_ok=True)
        return res.text()
    tmp = _ws(ctx) / ".avrone" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / f"snippet_{uuid.uuid4().hex[:10]}.py"
    f.write_text(code)
    try:
        res = await ctx.core.runner.run(f"python3 {shlex.quote(_rel(ctx, f))}")
    finally:
        f.unlink(missing_ok=True)
    return res.text()


# --------------------------------------------------------------------------- list_files / search_files

async def t_list_files(ctx: "AgentContext", args: dict[str, Any]) -> str:
    r = fsunify.resolve(ctx, str(args.get("path", "") or "."), "list_files")
    root, skip = r.path, fsunify.skip_dirs(ctx, r)
    if not root.exists():
        return f"ERROR: {args.get('path')} not found"
    if root.is_file():
        return f"{fsunify.display(ctx, root)} ({root.stat().st_size} bytes)"
    depth = _int(args.get("depth"), 3, 1, 10)
    cap = 400
    head = fsunify.display(ctx, root)
    lines = [head if head.endswith("/") else head + "/"]
    count = 0
    truncated = False

    def walk(d: Path, level: int, prefix: str) -> None:
        nonlocal count, truncated
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP_DIRS or e in skip:
                continue
            if count >= cap:
                truncated = True
                return
            count += 1
            if e.is_dir() and not e.is_symlink():
                lines.append(f"{prefix}{e.name}/")
                if level < depth:
                    walk(e, level + 1, prefix + "  ")
            else:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{e.name} ({size}b)")

    walk(root, 1, "  ")
    if truncated:
        lines.append(f"...[truncated at {cap} entries]")
    return "\n".join(lines)


async def t_search_files(ctx: "AgentContext", args: dict[str, Any]) -> str:
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return "ERROR: pattern is required"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex: {e}"
    r = fsunify.resolve(ctx, str(args.get("path", "") or "."), "search_files")
    root = r.path
    if not root.exists():
        return f"ERROR: {args.get('path')} not found"
    cap, hits, scanned = 200, [], 0
    files = [root] if root.is_file() else _iter_files(root, fsunify.skip_dirs(ctx, r))
    for f in files:
        if scanned > 5000 or len(hits) >= cap:
            break
        scanned += 1
        try:
            if f.stat().st_size > 1_000_000:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for no, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{fsunify.display(ctx, f)}:{no}: {line.strip()[:300]}")
                if len(hits) >= cap:
                    break
    if not hits:
        return f"No matches for /{pattern}/."
    out = "\n".join(hits)
    if len(hits) >= cap:
        out += f"\n...[capped at {cap} matches]"
    return out


def _iter_files(root: Path, skip: set[Path] | None = None):
    skip = skip or set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and Path(dirpath) / d not in skip)
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


# --------------------------------------------------------------------------- edit_file

def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on", "all")
    return bool(v)


def _rstrip_lines(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.split("\n"))


def _loose_span(text: str, old: str) -> list[tuple[int, int]]:
    """Spans of `text` whose lines equal `old`'s lines up to trailing whitespace. Line-aligned
    only (old must cover whole lines), so it can never splice into the middle of a line."""
    olines = [ln.rstrip() for ln in old.strip("\n").split("\n")]
    if not any(olines):
        return []
    lines = text.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    k, spans = len(olines), []
    for i in range(len(lines) - k + 1):
        if all(lines[i + j].rstrip() == olines[j] for j in range(k)):
            end = starts[i + k - 1] + len(lines[i + k - 1])
            spans.append((starts[i], end))
    return spans


def _nearest_hint(text: str, old: str) -> str:
    import difflib
    first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
    if not first:
        return ""
    lines = text.split("\n")
    best = difflib.get_close_matches(first, [ln.strip() for ln in lines], n=1, cutoff=0.6)
    if not best:
        return ""
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == best[0])
    return f"; closest line is {idx + 1}: {lines[idx].strip()[:120]!r} (read_file the file and copy the exact text)"


async def t_edit_file(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Exact, unique replace, hardened (round 25): bytes outside the edit survive untouched
    (a latin-1 or mixed file was silently rewritten with U+FFFD everywhere before), CRLF files
    match an LF `old`, trailing-whitespace drift matches line-aligned, a miss names the closest
    line, binary files and directories are refused, replace_all is opt-in, writes are atomic
    and keep the file's mode."""
    from .tools import atomic_write_bytes
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "ERROR: edit_file needs a file path"
    r = fsunify.resolve(ctx, raw_path, "edit_file")
    p = r.path
    if p.is_dir():
        return f"ERROR: {raw_path} is a directory, not a file"
    if not p.is_file():
        return f"ERROR: {raw_path} not found"
    old = args.get("old")
    new = args.get("new")
    old = "" if old is None else str(old)
    new = "" if new is None else str(new)
    if not old:
        return "ERROR: 'old' must be a non-empty string"
    if old == new:
        return "ERROR: 'old' and 'new' are identical; nothing to change"
    data = p.read_bytes()
    name = fsunify.display(ctx, p)
    if b"\x00" in data[:8192]:
        return f"ERROR: {name} is a binary file; edit it with shell instead"
    # surrogateescape: undecodable bytes round-trip exactly instead of becoming U+FFFD
    text = data.decode("utf-8", "surrogateescape")
    new = re.sub(r"[\ud800-\udfff]", "\ufffd", new)
    old = re.sub(r"[\ud800-\udfff]", "\ufffd", old)
    replace_all = _truthy(args.get("replace_all"))
    crlf = "\r\n" in text and "\r\n" not in old
    if crlf:
        old_c, new_c = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        old_c, new_c = old, new
    how = ""
    n = text.count(old_c)
    if n == 0 and crlf and old in text:  # an LF fragment that happens to sit on one line
        old_c, new_c, n = old, new, text.count(old)
    if n:
        if n > 1 and not replace_all:
            return (f"ERROR: 'old' text matches {n} times in {name}; include more context to make it "
                    "unique, or pass replace_all=true")
        out = text.replace(old_c, new_c) if replace_all else text.replace(old_c, new_c, 1)
        count = n if replace_all else 1
    else:
        work = text.replace("\r\n", "\n") if crlf else text
        spans = _loose_span(work, old)
        if not spans:
            return f"ERROR: 'old' text not found in {name}{_nearest_hint(work, old)}"
        if len(spans) > 1 and not replace_all:
            return (f"ERROR: 'old' text matches {len(spans)} times in {name} (ignoring trailing "
                    "whitespace); include more context, or pass replace_all=true")
        repl = new.replace("\r\n", "\n").strip("\n") if old.strip("\n") != old else new.replace("\r\n", "\n")
        for s0, e0 in reversed(spans if replace_all else spans[:1]):
            work = work[:s0] + repl + work[e0:]
        out = work.replace("\n", "\r\n") if crlf else work
        count = len(spans) if replace_all else 1
        how = ", matched ignoring trailing whitespace"
    blob = out.encode("utf-8", "surrogateescape")
    if r.in_agent:
        from .agentfs_tools import quota_write
        quota_write(ctx, r.root, p, blob, tool="edit_file")
    else:
        atomic_write_bytes(p, blob)
    plural = "replacement" if count == 1 else "replacements"
    return f"edited {name} ({count} {plural}{how})"


# --------------------------------------------------------------------------- http_request

_KEY_HEADERS = ("content-type", "content-length", "location", "etag", "last-modified",
                "cache-control", "retry-after", "x-ratelimit-remaining")


async def t_http_request(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "allow_network", True):
        return "ERROR: network access is disabled."
    method = str(args.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        return f"ERROR: unsupported method {method}"
    url = str(args.get("url", ""))
    if not re.match(r"^https?://", url):
        return "ERROR: url must start with http(s)://"
    headers = args.get("headers") or {}
    if not isinstance(headers, dict):
        return "ERROR: headers must be an object"
    headers = {str(k): str(v) for k, v in headers.items()}
    headers.setdefault("User-Agent", "AVRONE/0.2")
    body = args.get("body")
    kw: dict[str, Any] = {"headers": headers}
    if isinstance(body, (dict, list)):
        kw["json"] = body
    elif body is not None:
        kw["content"] = str(body).encode()
    async with _client() as c:
        r = await c.request(method, url, **kw)
    lines = [f"HTTP {r.status_code} {r.reason_phrase}"]
    for h in _KEY_HEADERS:
        if h in r.headers:
            lines.append(f"{h}: {r.headers[h]}")
    text = r.text
    limit = 8000
    lines.append("")
    lines.append(text[:limit] + (f"\n...[truncated, {len(text)} chars total]" if len(text) > limit else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- git

def _is_force_push(argv: list[str]) -> bool:
    if "push" not in argv:
        return False
    after = argv[argv.index("push") + 1:]
    for a in after:
        if a in ("-f", "--force") or a.startswith("--force") or a == "--mirror":
            return True
        if a.startswith("-") and not a.startswith("--") and "f" in a[1:]:
            return True
        if a.startswith("+"):
            return True
    return False


async def t_git(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "agents_can_shell", True):
        return "ERROR: git is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    raw = str(args.get("args", "")).strip()
    if raw.startswith("git "):
        raw = raw[4:]
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        return f"ERROR: could not parse args: {e}"
    if not argv:
        return "ERROR: args is required, e.g. \"status\""
    if _is_force_push(argv):
        return "ERROR: force push is not allowed."
    return (await ctx.core.runner.run("git " + shlex.join(argv))).text()


# --------------------------------------------------------------------------- memory (Eternal CodeX)

def _mem_file(ctx: "AgentContext") -> Path:
    d = Path(_setting(ctx, "data_dir", "./data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "memory.jsonl"


def _mem_ns(ctx: "AgentContext") -> str:
    """This agent's own namespace (see avrone.memstore): pinned via ctx.memory_namespace, else
    the agent id derived from its name. Always a local namespace (never 'fleet:...')."""
    pinned = getattr(ctx, "memory_namespace", None)
    if isinstance(pinned, str) and memstore.is_local(pinned):
        return pinned
    return memstore.agent_namespace(getattr(ctx, "name", ""))


def load_memory(path: Path, namespace: str | None = None) -> dict[str, dict[str, Any]]:
    """Latest record per key: within `namespace` when given, else across every namespace."""
    store = memstore.MemoryStore(path)
    if namespace is not None:
        return store.load(namespace)
    latest: dict[str, dict[str, Any]] = {}
    for space in store.namespaces().values():
        for k, rec in space.items():
            if k not in latest or rec.get("ts", 0) >= latest[k].get("ts", 0):
                latest[k] = rec
    return latest


async def t_memory_save(ctx: "AgentContext", args: dict[str, Any]) -> str:
    key = str(args.get("key", "")).strip()
    if not key:
        return "ERROR: key is required"
    value = str(args.get("value", ""))
    ns = _mem_ns(ctx)
    try:
        memstore.MemoryStore(_mem_file(ctx)).save(ns, key, value, run_id=ctx.run_id, agent=ctx.name)
    except memstore.MemoryRefused as e:
        return f"ERROR: {e}"
    _emit(ctx, "memory.save", key=key, ns=ns, bytes=len(value.encode()))
    return f"saved memory '{key}'"


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t}


async def t_memory_recall(ctx: "AgentContext", args: dict[str, Any]) -> str:
    mem = load_memory(_mem_file(ctx), _mem_ns(ctx))
    if not mem:
        return "No memories saved yet."
    query = str(args.get("query", "") or "").strip()
    recs = list(mem.values())
    if query:
        qt = _tokens(query)
        ql = query.lower()
        scored = []
        for r in recs:
            k, v = str(r["key"]).lower(), str(r.get("value", "")).lower()
            score = 3 * len(qt & _tokens(k)) + len(qt & _tokens(v))
            if ql in k:
                score += 5
            elif ql in v:
                score += 2
            if score > 0:
                scored.append((score, r.get("ts", 0), r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        recs = [r for _, _, r in scored]
        if not recs:
            return f"No memories match {query!r}."
    else:
        recs.sort(key=lambda r: r.get("ts", 0), reverse=True)
    out = [f"- {r['key']}: {str(r.get('value', ''))[:1000]}" for r in recs[:20]]
    return "\n".join(out)


async def t_memory_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """List the keys in this agent's own namespace, newest first (keys only, no values)."""
    ns = _mem_ns(ctx)
    mem = load_memory(_mem_file(ctx), ns)
    limit = _int(args.get("limit"), 50, 1, 500)
    if not mem:
        return f"namespace {ns}: no memories saved yet."
    recs = sorted(mem.values(), key=lambda r: r.get("ts", 0), reverse=True)
    rows = [f"- {r['key']} ({len(str(r.get('value', '')).encode())} bytes)" for r in recs[:limit]]
    head = f"namespace {ns}: {len(mem)}/{memstore.max_entries()} entries"
    return head + "\n" + "\n".join(rows)


async def t_memory_sync(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Push memory up to the parent node over the signed fleet gateway (/api/fleet/memory).
    scope "agent" (default) = this agent's namespace; "node" = every local namespace on this node
    (memory merged in from other nodes is never re-pushed)."""
    from .fleet import MEMORY_SYNC_MAX_ENTRIES
    f = getattr(ctx.core, "fleet", None)
    if f is None or not getattr(f, "enabled", False):
        return "ERROR: fleet disabled: memory_sync needs AVRONE_FLEET_TOKEN (and AVRONE_FLEET_PARENT)"
    if not getattr(f, "parent", ""):
        return "ERROR: no parent node: set AVRONE_FLEET_PARENT to sync memory upward"
    scope = str(args.get("scope", "agent") or "agent")
    if scope not in ("agent", "node"):
        return "ERROR: scope must be \"agent\" or \"node\""
    store = memstore.MemoryStore(_mem_file(ctx))
    if scope == "agent":
        spaces = {(ns := _mem_ns(ctx)): store.load(ns)}
    else:
        spaces = {ns: v for ns, v in store.namespaces().items() if memstore.is_local(ns)}
    raw_keys = args.get("keys")
    if raw_keys in (None, "", []):
        want: set[str] = set()
    elif isinstance(raw_keys, (str, int)) and not isinstance(raw_keys, bool):
        want = {str(raw_keys).strip()}  # a bare "db" used to be iterated into {"d", "b"}
    elif isinstance(raw_keys, list):
        want = {str(k).strip() for k in raw_keys if isinstance(k, (str, int)) and not isinstance(k, bool)}
        if not want:
            return "ERROR: keys must be a list of key names"
    else:
        return "ERROR: keys must be a key name or a list of key names"
    recs = [{"ns": ns, "key": r["key"], "value": r.get("value", ""), "ts": r.get("ts", 0),
             "agent": r.get("agent", "")}
            for ns, space in spaces.items() for r in space.values() if not want or r["key"] in want]
    if not recs:
        return "nothing to sync: no matching memories"
    recs.sort(key=lambda r: r["ts"], reverse=True)
    dropped = max(0, len(recs) - MEMORY_SYNC_MAX_ENTRIES)
    recs = recs[:MEMORY_SYNC_MAX_ENTRIES]
    try:
        res = await f.push_memory(recs, f"{f.node_id}/{ctx.name}")
    except Exception as e:  # noqa: BLE001
        return f"ERROR: memory sync refused or failed: {e}"
    _emit(ctx, "memory.sync", scope=scope, entries=len(recs), parent=f.parent)
    return (f"synced {len(recs)} entries to parent {res.get('node', f.parent)}: merged={res.get('merged', 0)} "
            f"skipped={res.get('skipped', 0)} rejected={res.get('rejected', 0)}"
            + (f" (oldest {dropped} not sent: {MEMORY_SYNC_MAX_ENTRIES}-entry push cap)" if dropped else ""))


# --------------------------------------------------------------------------- artifact_save

async def t_artifact_save(ctx: "AgentContext", args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "ERROR: name is required"
    run = re.sub(r"[^A-Za-z0-9_\-.]", "_", str(ctx.run_id or "norun")) or "norun"
    base = _safe_path(_ws(ctx), f"artifacts/{run}")
    p = _safe_path(base, name)
    if p == base:
        return "ERROR: bad artifact name"
    # write_content: lone surrogates -> U+FFFD, dict/list -> JSON. A surrogate used to raise
    # UnicodeEncodeError AFTER open(), leaving an empty artifact behind; write_text also used the
    # locale encoding. Written atomically, as UTF-8, like write_file.
    from .tools import atomic_write_bytes, write_content
    content = write_content(args.get("content", ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(p, content.encode("utf-8"))
    rel = _rel(ctx, p)
    size = p.stat().st_size
    _emit(ctx, "artifact", path=rel, bytes=size)
    return rel


# --------------------------------------------------------------------------- docker_run

async def t_docker_run(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "agents_can_shell", True):
        return "ERROR: docker is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    if not shutil.which("docker"):
        return "ERROR: docker is not available on this host (no `docker` binary found)."
    image = str(args.get("image", "")).strip()
    cmd = str(args.get("cmd", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-/:@]*", image):
        return "ERROR: invalid image name"
    if not cmd.strip():
        return "ERROR: cmd is required"
    ws = _ws(ctx)
    run_name = f"avrone-dr-{uuid.uuid4().hex[:12]}"
    argv = ["docker", "run", "--rm", "--name", run_name, "-v", f"{ws}:/workspace", "-w", "/workspace"]
    if not _setting(ctx, "allow_network", True):
        argv += ["--network", "none"]
    argv += [image, "sh", "-c", cmd]
    timeout = int(_setting(ctx, "command_timeout", 120)) + 180  # allow for image pull
    try:
        res = await ctx.core.runner._exec(argv, f"docker run --rm {image} {cmd}", timeout)
    except asyncio.CancelledError:
        await asyncio.shield(_docker_rm(ctx, run_name))
        raise
    if getattr(res, "timed_out", False):  # the killed client leaves the --rm container running
        await _docker_rm(ctx, run_name)
    return res.text()


async def _docker_rm(ctx: "AgentContext", name: str) -> None:
    try:
        await ctx.core.runner._exec(["docker", "rm", "-f", name], "docker rm", 30)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- think

async def t_think(ctx: "AgentContext", args: dict[str, Any]) -> str:
    _emit(ctx, "agent.think", thought=str(args.get("thought", ""))[:4000], tool="think")
    return "noted"


# --------------------------------------------------------------------------- diff_files

DIFF_MAX_FILE_BYTES = 1_000_000
DIFF_MAX_TEXT_CHARS = 1_000_000
DIFF_MAX_OUTPUT_CHARS = 12000


def _read_text_for_diff(ctx: "AgentContext", raw: Any, label: str) -> tuple[str | None, str]:
    """Return (text, display_name) or (None, error message)."""
    rel = str(raw or "").strip()
    if not rel:
        return None, f"ERROR: {label} is required"
    p = fsunify.resolve(ctx, rel, "diff_files").path
    if not p.exists():
        return None, f"ERROR: {rel} not found"
    if not p.is_file():
        return None, f"ERROR: {rel} is not a file"
    if p.stat().st_size > DIFF_MAX_FILE_BYTES:
        return None, f"ERROR: {rel} is larger than {DIFF_MAX_FILE_BYTES} bytes"
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        return None, f"ERROR: {rel} looks like a binary file"
    return data.decode(errors="replace"), fsunify.display(ctx, p)


async def t_diff_files(ctx: "AgentContext", args: dict[str, Any]) -> str:
    import difflib

    has_b, has_text = args.get("path_b") not in (None, ""), "text_b" in args and args.get("text_b") is not None
    if has_b == has_text:
        return "ERROR: give exactly one of path_b or text_b"
    a_text, a_name = _read_text_for_diff(ctx, args.get("path_a"), "path_a")
    if a_text is None:
        return a_name
    if has_b:
        b_text, b_name = _read_text_for_diff(ctx, args.get("path_b"), "path_b")
        if b_text is None:
            return b_name
    else:
        if not isinstance(args.get("text_b"), str):
            return "ERROR: text_b must be a string"
        b_text, b_name = args["text_b"], "(text_b)"
        if len(b_text) > DIFF_MAX_TEXT_CHARS:
            return f"ERROR: text_b is longer than {DIFF_MAX_TEXT_CHARS} chars"
    context = _int(args.get("context"), 3, 0, 20)
    lines = list(difflib.unified_diff(a_text.splitlines(keepends=True), b_text.splitlines(keepends=True),
                                      fromfile=a_name, tofile=b_name, n=context))
    if not lines:
        return f"No differences between {a_name} and {b_name}."
    out = "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in lines)
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    summary = f"[{added} added, {removed} removed]"
    if len(out) > DIFF_MAX_OUTPUT_CHARS:
        out = out[:DIFF_MAX_OUTPUT_CHARS] + f"\n...[diff truncated, {len(out)} chars total]\n"
    return out.rstrip("\n") + "\n" + summary


# --------------------------------------------------------------------------- codex_search

CODEX_MAX_RUN_FILES = 200
CODEX_MAX_EVENTS_SCANNED = 50_000
CODEX_MAX_OUTPUT_CHARS = 12000
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_\-.]{1,128}")


def _codex_match(ev: dict[str, Any], kind: str, agent: str, tool: str, query: str) -> bool:
    if kind:
        ek = str(ev.get("kind", ""))
        if kind.endswith("*") or kind.endswith("."):
            if not ek.startswith(kind.rstrip("*")):
                return False
        elif ek != kind:
            return False
    if agent and str(ev.get("agent", "")).lower() != agent:
        return False
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    if tool and str(data.get("tool", "")) != tool:
        return False
    if query:
        hay = (str(ev.get("kind", "")) + " " + str(ev.get("agent", "")) + " "
               + json.dumps(data, default=str)).lower()
        if query not in hay:
            return False
    return True


def _codex_line(ev: dict[str, Any], run_id: str) -> str:
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(ev.get("ts", 0))))
    except (TypeError, ValueError, OverflowError, OSError):
        ts = "?"
    tool = f" tool={_line1(data['tool'], 80)}" if data.get("tool") else ""
    rest = {k: v for k, v in data.items() if k != "tool"}
    snippet = re.sub(r"\s+", " ", json.dumps(rest, default=str))
    if len(snippet) > 240:
        snippet = snippet[:240] + "..."
    # kind/agent/tool are whatever the log holds: a 4 MB tool name or a newline in an agent name used
    # to come out whole (or as a forged extra result line)
    return f"[{run_id}] {ts}Z {_line1(ev.get('kind', '?'), 80)} {_line1(ev.get('agent', '?'), 120)}{tool} {snippet}"


def _line1(v: Any, n: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", str(v))[:n]


async def t_codex_search(ctx: "AgentContext", args: dict[str, Any]) -> str:
    codex = getattr(ctx.core, "codex", None)
    log_dir = getattr(codex, "log_dir", None)
    if log_dir is None:
        return "ERROR: the Eternal CodeX log is not available"
    log_dir = Path(log_dir)
    kind = str(args.get("kind") or "").strip()
    agent = str(args.get("agent") or "").strip().lower()
    tool = str(args.get("tool") or "").strip()
    query = str(args.get("query") or "").strip().lower()
    run_id = str(args.get("run_id") or "").strip()
    if not (kind or agent or tool or query or run_id):
        return "ERROR: give at least one filter: kind, agent, tool, query or run_id"
    if len(query) > 500:
        return "ERROR: query is longer than 500 chars"
    limit = _int(args.get("limit"), 20, 1, 200)
    max_runs = _int(args.get("runs"), 50, 1, CODEX_MAX_RUN_FILES)
    if not log_dir.is_dir():
        return "No CodeX runs logged yet."
    if run_id:
        if not _RUN_ID_RE.fullmatch(run_id) or run_id in (".", ".."):
            return "ERROR: invalid run_id"
        f = log_dir / f"{run_id}.jsonl"
        if not f.resolve().is_relative_to(log_dir.resolve()):
            return "ERROR: invalid run_id"
        if not f.is_file():
            return f"ERROR: run {run_id} not found"
        files = [f]
    else:
        files = sorted(log_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_runs]
        if not files:
            return "No CodeX runs logged yet."
    hits: list[tuple[float, str]] = []
    scanned, capped = 0, False
    for f in files:
        try:
            fh = f.open(errors="replace")
        except OSError:
            continue
        from .codex import iter_lines
        with fh:
            for line in iter_lines(fh):  # a giant/newline-less line is skipped, never loaded whole
                if scanned >= CODEX_MAX_EVENTS_SCANNED:
                    capped = True
                    break
                scanned += 1
                if line is None:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, RecursionError):
                    continue
                if not isinstance(ev, dict) or not _codex_match(ev, kind, agent, tool, query):
                    continue
                try:
                    ts = float(ev.get("ts", 0))
                except (TypeError, ValueError):
                    ts = 0.0
                hits.append((ts, _codex_line(ev, f.stem)))
        if capped:
            break
    if not hits:
        return f"No CodeX events match (scanned {scanned} events in {len(files)} run(s))."
    hits.sort(key=lambda h: h[0], reverse=True)
    total = len(hits)
    lines = [f"{total} matching event(s) in {len(files)} run(s), newest first; showing {min(total, limit)}:"]
    size = len(lines[0])
    for _, ln in hits[:limit]:
        if size + len(ln) + 1 > CODEX_MAX_OUTPUT_CHARS:
            lines.append("...[output capped]")
            break
        lines.append(ln)
        size += len(ln) + 1
    if capped:
        lines.append(f"...[scan stopped at {CODEX_MAX_EVENTS_SCANNED} events; narrow with run_id or runs]")
    return "\n".join(lines)


# --------------------------------------------------------------------------- self_scan
# Read-only self-inventory of the agent's OWN environment. Never probes other hosts and
# never reveals a secret value: secret-shaped env names only report "***set***" / "unset".

SELF_SCAN_MAX_CHARS = 8000
SELF_SCAN_MAX_IFACES = 16
SELF_SCAN_MAX_ADDRS = 8
SELF_SCAN_MAX_SECRET_NAMES = 40
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
# Secret-shaped names always listed (as set/unset) so the report is stable across hosts.
KNOWN_SECRET_ENV = ("AVRONE_ADMIN_TOKEN", "AVRONE_LLM_API_KEY", "OPENCLAW_GATEWAY_TOKEN", "XAI_API_KEY",
                    "OPENAI_API_KEY", "OPENROUTER_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/@\s]*@")


def is_secret_name(name: str) -> bool:
    up = name.upper()
    return any(m in up for m in SECRET_MARKERS)


def redact_env(name: str, value: str | None) -> str:
    """How an env var may appear in a report: secret-shaped -> presence only; URLs lose credentials."""
    if is_secret_name(name):
        return "***set***" if value else "unset"
    if value is None:
        return "unset"
    v = _URL_USERINFO_RE.sub(r"\1***@", str(value))
    return v[:200] + ("..." if len(v) > 200 else "")


def _writable(p: Path) -> bool:
    try:
        return p.exists() and os.access(p, os.W_OK)
    except OSError:
        return False


def _disk(p: Path) -> dict[str, Any] | None:
    try:
        u = shutil.disk_usage(p)
    except OSError:
        return None
    gib = 1024 ** 3
    return {"total_gib": round(u.total / gib, 1), "free_gib": round(u.free / gib, 1)}


def _own_interfaces() -> list[dict[str, Any]]:
    """This host's own interfaces (presence helper: local `ip addr` / own sockets, no probing)."""
    from .presence import _host_interfaces
    try:
        ifs = _host_interfaces()
    except Exception:  # noqa: BLE001
        return []
    return [{"name": str(i.get("name")), "state": str(i.get("state")),
             "addrs": [str(a) for a in (i.get("addrs") or [])][:SELF_SCAN_MAX_ADDRS]}
            for i in ifs[:SELF_SCAN_MAX_IFACES]]


def self_inventory(ctx: "AgentContext") -> dict[str, Any]:
    """Structured, secret-free inventory of the environment this agent runs in."""
    import platform
    import socket

    core = ctx.core
    s = core.settings
    runner = getattr(core, "runner", None)
    ws = Path(getattr(runner, "workspace", None) or _setting(ctx, "workspace", ".")).resolve()
    cwd = Path(os.getcwd())
    tools = getattr(core, "tools", None)
    try:
        tool_count: int | None = len(tools.names()) if tools is not None else None
    except Exception:  # noqa: BLE001
        tool_count = None
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "unknown"
    allowlist = [h.strip() for h in os.getenv("AVRONE_RECON_ALLOWLIST", "").split(",") if h.strip()]
    secret_names = set(KNOWN_SECRET_ENV) | {k for k in os.environ if is_secret_name(k)}
    secrets = {k: redact_env(k, os.environ.get(k)) for k in sorted(secret_names)[:SELF_SCAN_MAX_SECRET_NAMES]}
    return {
        "system": {"os": f"{platform.system()} {platform.release()}".strip(), "arch": platform.machine(),
                   "python": platform.python_version(), "hostname": hostname, "pid": os.getpid()},
        "paths": {"cwd": str(cwd), "cwd_writable": _writable(cwd), "workspace": str(ws),
                  "workspace_writable": _writable(ws), "workspace_disk": _disk(ws if ws.exists() else cwd)},
        "runner": {"kind": getattr(runner, "kind", "unknown"), "configured": str(_setting(ctx, "runner", "auto")),
                   "docker_available": bool(shutil.which("docker"))},
        "toggles": {
            "AVRONE_RECON": os.getenv("AVRONE_RECON", "allow").lower(),
            "AVRONE_RECON_ALLOWLIST": ",".join(allowlist)[:300] or "(none: default-deny)",
            "AVRONE_ALLOW_NETWORK": bool(_setting(ctx, "allow_network", True)),
            "AVRONE_AGENTS_CAN_SHELL": bool(_setting(ctx, "agents_can_shell", True)),
            "AVRONE_SELF_EVOLVE": os.getenv("AVRONE_SELF_EVOLVE", "allow").lower(),
            "AVRONE_SELF_CHANGES": os.getenv("AVRONE_SELF_CHANGES", "allow").lower(),
            "admin_token": "on" if _setting(ctx, "admin_token", "") else "off",
        },
        "tools": {"registered": tool_count},
        "network_interfaces": _own_interfaces(),
        "secrets": secrets,
    }


def _cap(text: str, limit: int = SELF_SCAN_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


async def t_self_scan(ctx: "AgentContext", args: dict[str, Any]) -> str:
    inv = self_inventory(ctx)
    if str(args.get("format", "text")).lower() == "json":
        return _cap(json.dumps(inv, indent=1, sort_keys=True))
    sy, pa, ru, tg = inv["system"], inv["paths"], inv["runner"], inv["toggles"]
    disk = pa["workspace_disk"]
    lines = [
        "self_scan (own environment only; read-only; secret values never shown)",
        f"system: {sy['os']} {sy['arch']} python {sy['python']} host={sy['hostname']} pid={sy['pid']}",
        f"cwd: {pa['cwd']} (writable={pa['cwd_writable']})",
        f"workspace: {pa['workspace']} (writable={pa['workspace_writable']})"
        + (f" disk free {disk['free_gib']}/{disk['total_gib']} GiB" if disk else ""),
        f"runner: {ru['kind']} (configured={ru['configured']}) docker_available={ru['docker_available']}",
        "toggles: " + " ".join(f"{k}={v}" for k, v in tg.items()),
        f"tools registered: {inv['tools']['registered'] if inv['tools']['registered'] is not None else 'unknown'}",
        "network interfaces:",
    ]
    for i in inv["network_interfaces"] or [{"name": "(none found)", "state": "", "addrs": []}]:
        lines.append(f"  {i['name']} {i['state']} {', '.join(i['addrs'])}".rstrip())
    lines.append("secret-shaped env: " + (", ".join(f"{k}={v}" for k, v in inv["secrets"].items()) or "(none)"))
    return _cap("\n".join(lines))


# --------------------------------------------------------------------------- capabilities_report
# Plain-English capability ledger derived from self_inventory(): what this agent CAN and
# CANNOT do under current settings. Read-only; secret values never appear.

def capability_ledger(inv: dict[str, Any]) -> list[dict[str, Any]]:
    tg, ru, pa = inv["toggles"], inv["runner"], inv["paths"]
    shell = bool(tg["AVRONE_AGENTS_CAN_SHELL"])
    net = bool(tg["AVRONE_ALLOW_NETWORK"])
    recon = tg["AVRONE_RECON"] != "deny"
    evolve = tg["AVRONE_SELF_EVOLVE"] != "deny"
    changes = tg["AVRONE_SELF_CHANGES"]
    docker_ok = shell and ru["docker_available"]
    if not shell:
        docker_detail = "AVRONE_AGENTS_CAN_SHELL=false"
    elif not ru["docker_available"]:
        docker_detail = "no docker binary on this host"
    else:
        docker_detail = "docker_run" + ("" if net else " (containers get --network none)")
    secrets_set = sum(1 for v in inv["secrets"].values() if v == "***set***")

    def row(cap: str, can: bool, status: str, detail: str) -> dict[str, Any]:
        return {"capability": cap, "can": can, "status": status, "detail": detail}

    return [
        row("shell", shell, "enabled" if shell else "disabled",
            "shell, python, git" + ("" if shell else " refused (AVRONE_AGENTS_CAN_SHELL=false)")),
        row("docker", docker_ok, "enabled" if docker_ok else "unavailable", docker_detail),
        row("outbound network", net, "enabled" if net else "disabled",
            "fetch, web_search, http_request" + ("" if net else " refused (AVRONE_ALLOW_NETWORK=false)")),
        row("recon", recon, "allow" if recon else "deny",
            f"allowlist={tg['AVRONE_RECON_ALLOWLIST']}" + ("" if net or not recon else "; outbound network is off")),
        row("self-evolve", evolve, "allow" if evolve else "deny", "AVRONE_SELF_EVOLVE=" + tg["AVRONE_SELF_EVOLVE"]),
        row("self-changes", changes != "deny", changes,
            "env_install " + {"deny": "refused", "log": "runs and is logged"}.get(changes, "runs and is recorded")),
        row("workspace write", bool(pa["workspace_writable"]),
            "enabled" if pa["workspace_writable"] else "read-only", pa["workspace"]),
        row("admin API", tg["admin_token"] == "on", tg["admin_token"],
            "AVRONE_ADMIN_TOKEN " + ("set (value hidden)" if tg["admin_token"] == "on" else "unset; admin endpoints refuse")),
        row("runner", True, str(ru["kind"]), f"configured={ru['configured']}"),
        row("credentials", secrets_set > 0, f"{secrets_set} set",
            "secret-shaped env vars present; values never shown"),
    ]


async def t_capabilities_report(ctx: "AgentContext", args: dict[str, Any]) -> str:
    inv = self_inventory(ctx)
    ledger = capability_ledger(inv)
    if str(args.get("format", "text")).lower() == "json":
        return _cap(json.dumps({"host": inv["system"]["hostname"], "tools_registered": inv["tools"]["registered"],
                                "ledger": ledger}, indent=1))
    lines = [f"capabilities_report for {inv['system']['hostname']} (own settings only; read-only)"]
    for r in ledger:
        lines.append(f"- {r['capability']}: {r['status']} ({r['detail']})")
    lines.append("CAN: " + (", ".join(r["capability"] for r in ledger if r["can"]) or "(nothing)"))
    lines.append("CANNOT: " + (", ".join(r["capability"] for r in ledger if not r["can"]) or "(nothing)"))
    return _cap("\n".join(lines))


# --------------------------------------------------------------------------- registration

# --------------------------------------------------------------------------- privileged tier (priv_shell)
# Deny by default. A tool here runs ONLY when the run was granted the matching elevated capability
# through the signed fleet gateway, and even then only inside a hard allowlist / fixed action set.
# fleet.handle_run marks the grant TWICE and both must agree: ctx.core.fleet_privileged (core-wide)
# and the PRIV_GRANT context variable, which is scoped to that run's task tree -- so an unrelated run
# executing concurrently on the same core never inherits the elevated capability.
# Every attempt is audited to the Eternal CodeX.
#   AVRONE_PRIV_SHELL_ALLOW    comma list of allowed argv[0] names (bare names, e.g. "ls,cat,git");
#                              default empty = refuse every command
#   AVRONE_PRIV_SHELL_TIMEOUT  per-command timeout ceiling in seconds (default 30, clamped 1..300)
PRIV_SHELL_CAP = "priv:shell"
PRIV_SHELL_METACHARS = set(";|&`$><") | {"\n", "\r", "\x00"}
PRIV_SHELL_MAX_CMD = 2000
PRIV_SHELL_MAX_ARGS = 64
PRIV_SHELL_TIMEOUT_DEFAULT = 30
PRIV_SHELL_TIMEOUT_MAX = 300
_PRIV_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]*")


PRIV_GRANT: contextvars.ContextVar[str] = contextvars.ContextVar("avrone_priv_grant", default="")


@contextlib.contextmanager
def priv_grant(cap: str):
    """Scope a granted capability to the current task tree. Only fleet.handle_run should call this,
    after the signed privileged gate has accepted the request."""
    token = PRIV_GRANT.set(cap or "")
    try:
        yield
    finally:
        PRIV_GRANT.reset(token)


def granted_capability(ctx: "AgentContext") -> str:
    """The elevated capability the signed gateway granted THIS run ("" = none): the core flag and
    the run-scoped grant must both be set and identical."""
    cap = getattr(ctx.core, "fleet_privileged", "")
    if not isinstance(cap, str) or not cap or PRIV_GRANT.get() != cap:
        return ""
    return cap


def priv_shell_allowlist() -> set[str]:
    """Allowed argv[0] names. Entries that are not bare names (paths, globs, blanks) are ignored."""
    raw = os.environ.get("AVRONE_PRIV_SHELL_ALLOW", "")
    return {a.strip() for a in raw.split(",") if _PRIV_NAME_RE.fullmatch(a.strip())}


def priv_shell_timeout(requested: Any = None) -> int:
    try:
        ceiling = int(os.environ.get("AVRONE_PRIV_SHELL_TIMEOUT", "") or PRIV_SHELL_TIMEOUT_DEFAULT)
    except ValueError:
        ceiling = PRIV_SHELL_TIMEOUT_DEFAULT
    ceiling = max(1, min(PRIV_SHELL_TIMEOUT_MAX, ceiling))
    return _int(requested, ceiling, 1, ceiling) if requested is not None else ceiling


def check_priv_shell_command(cmd: str, allow: set[str]) -> tuple[list[str] | None, str, str]:
    """Validate one bounded command. Returns (argv, "", "") or (None, code, reason)."""
    if not cmd.strip():
        return None, "empty", "cmd is required"
    if len(cmd) > PRIV_SHELL_MAX_CMD:
        return None, "too_long", f"cmd longer than {PRIV_SHELL_MAX_CMD} chars"
    bad = sorted({ch for ch in cmd if ch in PRIV_SHELL_METACHARS})
    if bad:
        return None, "metachar", ("shell metacharacters / pipelines are not allowed (single bounded command only): "
                                  + " ".join(repr(b) for b in bad))
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return None, "parse", f"cannot parse cmd: {e}"
    if not argv:
        return None, "empty", "cmd is required"
    if len(argv) > PRIV_SHELL_MAX_ARGS:
        return None, "too_many_args", f"more than {PRIV_SHELL_MAX_ARGS} arguments"
    exe = argv[0]
    if not _PRIV_NAME_RE.fullmatch(exe):  # bare name only: no paths ("/tmp/x/ls"), no VAR=x prefixes
        return None, "not_allowlisted", f"{exe!r} is not a bare command name"
    if not allow:
        return None, "allowlist_empty", "no commands allowed (AVRONE_PRIV_SHELL_ALLOW is empty)"
    if exe not in allow:
        return None, "not_allowlisted", f"{exe!r} is not in AVRONE_PRIV_SHELL_ALLOW"
    return argv, "", ""


def _priv_refuse(ctx: "AgentContext", tool: str, code: str, reason: str, **data: Any) -> str:
    _emit(ctx, "priv.refused", tool=tool, code=code, reason=reason,
          capability=granted_capability(ctx), **data)
    return f"ERROR: {reason}"


async def t_priv_shell(ctx: "AgentContext", args: dict[str, Any]) -> str:
    cmd = str(args.get("cmd", ""))
    shown = cmd[:300]
    if granted_capability(ctx) != PRIV_SHELL_CAP:
        return _priv_refuse(ctx, "priv_shell", "no_capability",
                            "requires a granted priv:shell capability (signed fleet privileged run)", cmd=shown)
    argv, code, reason = check_priv_shell_command(cmd, priv_shell_allowlist())
    if argv is None:
        return _priv_refuse(ctx, "priv_shell", code, reason, cmd=shown)
    safe = shlex.join(argv)  # every token re-quoted: no globbing/expansion survives into bash -lc
    timeout = priv_shell_timeout(args.get("timeout"))
    try:
        res = await ctx.core.runner.run(safe, timeout=timeout)
    except Exception as e:  # noqa: BLE001 -- audited, then reported to the agent
        _emit(ctx, "priv.shell", tool="priv_shell", cmd=safe, argv0=argv[0], timeout=timeout,
              error=f"{type(e).__name__}: {e}"[:300])
        return f"ERROR: {type(e).__name__}: {e}"
    _emit(ctx, "priv.shell", tool="priv_shell", cmd=safe, argv0=argv[0], timeout=timeout,
          exit_code=res.exit_code, timed_out=res.timed_out)
    return res.text()



# --------------------------------------------------------------------------- privileged tier (priv_admin)
# A SMALL, explicit set of read-only admin views. Never arbitrary code, never a write.
PRIV_ADMIN_CAP = "priv:admin"
PRIV_ADMIN_MAX_CHARS = 12000


def _admin_fleet(ctx: "AgentContext") -> Any:
    f = getattr(ctx.core, "fleet", None)
    if f is None:
        raise LookupError("fleet is not available on this node")
    return f


async def _admin_queue_stats(ctx: "AgentContext", args: dict[str, Any]) -> Any:
    q = getattr(_admin_fleet(ctx), "queue", None)
    if q is None:
        raise LookupError("queue not running")
    return await q.stats()


async def _admin_node_list(ctx: "AgentContext", args: dict[str, Any]) -> Any:
    keep = ("node_id", "status", "load", "children", "last_seen")
    return [{**{k: n.get(k) for k in keep}, "runner": (n.get("capabilities") or {}).get("runner")}
            for n in _admin_fleet(ctx).list_nodes()]


async def _admin_fleet_info(ctx: "AgentContext", args: dict[str, Any]) -> Any:
    return _admin_fleet(ctx).info()


def _admin_codex(ctx: "AgentContext") -> Any:
    codex = getattr(ctx.core, "codex", None)
    if codex is None or not callable(getattr(codex, "replay", None)):
        raise LookupError("the Eternal CodeX log is not available")
    return codex


async def _admin_codex_runs(ctx: "AgentContext", args: dict[str, Any]) -> Any:
    return _admin_codex(ctx).runs(_int(args.get("limit"), 20, 1, 50))


async def _admin_codex_tail(ctx: "AgentContext", args: dict[str, Any]) -> Any:
    run_id = str(args.get("run_id") or ctx.run_id or "").strip()
    if not _RUN_ID_RE.fullmatch(run_id) or run_id in (".", ".."):
        raise ValueError("invalid run_id")
    n = _int(args.get("limit"), 20, 1, 100)
    out = []
    cx = _admin_codex(ctx)
    try:
        evs = cx.replay(run_id, limit=n)  # only the tail is kept while streaming
    except TypeError:  # an older codex without the limit argument
        evs = cx.replay(run_id)[-n:]
    for e in evs:
        data = e.get("data")
        out.append({"ts": e.get("ts"), "kind": e.get("kind"), "agent": e.get("agent"),
                    "data": json.dumps(data, default=str)[:300] if data is not None else None})
    return out


PRIV_ADMIN_ACTIONS: dict[str, tuple[str, Any]] = {
    "queue_stats": ("job counts by status", _admin_queue_stats),
    "node_list": ("linked fleet nodes with status/load/runner", _admin_node_list),
    "fleet_info": ("this node's fleet summary", _admin_fleet_info),
    "codex_runs": ("recent CodeX runs (limit<=50)", _admin_codex_runs),
    "codex_tail": ("last events of a CodeX run (run_id, default this run; limit<=100)", _admin_codex_tail),
}


async def t_priv_admin(ctx: "AgentContext", args: dict[str, Any]) -> str:
    action = str(args.get("action") or "").strip()
    shown = action[:80]
    if granted_capability(ctx) != PRIV_ADMIN_CAP:
        _emit(ctx, "admin.refused", tool="priv_admin", code="no_capability", action=shown,
              capability=granted_capability(ctx))
        return "ERROR: requires a granted priv:admin capability (signed fleet privileged run)"
    entry = PRIV_ADMIN_ACTIONS.get(action)
    if entry is None:
        _emit(ctx, "admin.refused", tool="priv_admin", code="unknown_action", action=shown,
              capability=PRIV_ADMIN_CAP)
        return f"ERROR: unknown admin action {shown!r}; allowed: {', '.join(sorted(PRIV_ADMIN_ACTIONS))}"
    try:
        result = await entry[1](ctx, args)
    except Exception as e:  # noqa: BLE001 -- audited, then reported to the agent
        _emit(ctx, "admin.action", tool="priv_admin", action=action, ok=False, error=f"{type(e).__name__}: {e}"[:300])
        return f"ERROR: {e}"
    _emit(ctx, "admin.action", tool="priv_admin", action=action, ok=True)
    text = json.dumps(result, default=str, indent=1)
    return text[:PRIV_ADMIN_MAX_CHARS] + ("\n...[truncated]" if len(text) > PRIV_ADMIN_MAX_CHARS else "")


EXTRA_TOOLS: list[tuple[str, str, str, Any]] = [
    ("web_search", "search the web (DuckDuckGo by default; Brave/Tavily if API keys set); returns numbered title/url/snippet results.",
     '{"query": str, "max_results"?: int}', t_web_search),
    ("python", "run a Python 3 snippet and return its stdout/stderr. Runs with cwd = your own agent dir when you have one (shared workspace via $AVRONE_SHARED_WORKSPACE), else in the workspace.",
     '{"code": str}', t_python),
    ("list_files", "show a directory tree (skips .git, node_modules, __pycache__). Defaults to your own agent dir when you have one; path `shared/` lists the shared team workspace.",
     '{"path"?: str, "depth"?: int}', t_list_files),
    ("search_files", "regex search across text files; returns file:line: match. Searches your own agent dir by default; path `shared/` searches the shared team workspace.",
     '{"pattern": str, "path"?: str}', t_search_files),
    ("edit_file", "replace one exact, unique occurrence of `old` with `new` in a file (own agent dir by default, `shared/...` for the shared workspace; quota applies in your dir).",
     '{"path": str, "old": str, "new": str}', t_edit_file),
    ("http_request", "make an HTTP request (any method) and return status, key headers and body.",
     '{"method": str, "url": str, "headers"?: dict, "body"?: str|dict}', t_http_request),
    ("git", "run a git command in the workspace, e.g. {\"args\": \"status\"} (force push refused).",
     '{"args": str}', t_git),
    ("memory_save", "save a fact to persistent agent memory (Eternal CodeX); latest value per key wins.",
     '{"key": str, "value": str}', t_memory_save),
    ("memory_recall", "recall persistent memories, ranked by keyword match (all recent if no query).",
     '{"query"?: str}', t_memory_recall),
    ("memory_list", "list the keys in your own persistent memory namespace, newest first (survives restarts).",
     '{"limit"?: int}', t_memory_list),
    ("memory_sync", "push your memory (scope agent) or this node's (scope node) to the PARENT fleet node over the signed gateway; it lands under fleet:<this-node>/<namespace> there.",
     '{"scope"?: "agent"|"node", "keys"?: [str]}', t_memory_sync),
    ("artifact_save", "save a deliverable file under artifacts/<run_id>/ and announce it; returns its path.",
     '{"name": str, "content": str}', t_artifact_save),
    ("docker_run", "run a command in a fresh container of the given image with the workspace mounted at /workspace.",
     '{"image": str, "cmd": str}', t_docker_run),
    ("think", "private scratchpad: record your reasoning/plan; no side effects.",
     '{"thought": str}', t_think),
    ("diff_files", "unified diff between two text files, or a file and given text (own agent dir by default, `shared/...` for the shared workspace; path-confined, output capped at 12000 chars).",
     '{"path_a": str, "path_b"?: str, "text_b"?: str, "context"?: int}', t_diff_files),
    ("codex_search", "read-only search of the Eternal CodeX run logs by event kind (exact, or prefix ending in '.'/'*'), agent, tool name, substring and/or run_id; newest first, bounded.",
     '{"kind"?: str, "agent"?: str, "tool"?: str, "query"?: str, "run_id"?: str, "limit"?: int, "runs"?: int}', t_codex_search),
    ("self_scan", "read-only inventory of YOUR OWN environment: OS/arch/python, host, cwd/workspace writability, runner, docker, AVRONE toggles, tool count, own network interfaces; secret env values redacted.",
     '{"format"?: "text"|"json"}', t_self_scan),
    ("capabilities_report", "read-only plain-English ledger of what YOU can and cannot do under current settings (shell, docker, network, recon+allowlist, self-evolve, self-changes, workspace write, admin); for squad reviews.",
     '{"format"?: "text"|"json"}', t_capabilities_report),
    ("priv_shell", "PRIVILEGED: run ONE allowlisted command (AVRONE_PRIV_SHELL_ALLOW) on the host runner; only in a run granted priv:shell via the signed fleet gateway; no pipes/metacharacters; audited.",
     '{"cmd": str, "timeout"?: int}', t_priv_shell),
    ("priv_admin", "PRIVILEGED: read-only admin views (queue_stats, node_list, fleet_info, codex_runs, codex_tail); only in a run granted priv:admin via the signed fleet gateway; audited.",
     '{"action": "queue_stats"|"node_list"|"fleet_info"|"codex_runs"|"codex_tail", "run_id"?: str, "limit"?: int}', t_priv_admin),
]


def register_extra_tools(registry) -> None:
    """Add every extra tool to `registry` (an avrone.tools.ToolRegistry)."""
    for name, desc, args_doc, fn in EXTRA_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
    from .netdiag import register_net_tools
    register_net_tools(registry)
    from .agentkit import register_kit_tools
    register_kit_tools(registry)
    from .evolve import register_evolve_tools
    register_evolve_tools(registry)
    from .recon import register_recon_tools
    register_recon_tools(registry)
    from .osint import register_osint_tools
    register_osint_tools(registry)
    from .agentfs_tools import register_agent_fs_tools
    register_agent_fs_tools(registry)
    from .agentpack_tools import register_agentpack_tools
    register_agentpack_tools(registry)
    from .bridge import register_bridge_tools
    register_bridge_tools(registry)
    from .tools_introspect import register_introspect_tools
    register_introspect_tools(registry)
    from .tools_codexstats import register_codex_stats_tools
    register_codex_stats_tools(registry)
    from .tools_mempeek import register_mempeek_tools
    register_mempeek_tools(registry)
    from .tools_agenthome import register_agenthome_tools
    register_agenthome_tools(registry)
    from .greatscale import register_greatscale_tools
    register_greatscale_tools(registry)
    from .tools_fleetstatus import register_fleetstatus_tools
    register_fleetstatus_tools(registry)
    from .tools_flakeprobe import register_flakeprobe_tools
    register_flakeprobe_tools(registry)
    from .tools_routerstatus import register_routerstatus_tools
    register_routerstatus_tools(registry)
    from .tools_researchnote import register_researchnote_tools
    register_researchnote_tools(registry)
    from .tools_lessonrecall import register_lessonrecall_tools
    register_lessonrecall_tools(registry)
    from .tools_lessonforget import register_lessonforget_tools
    register_lessonforget_tools(registry)
    from .tools_statehealth import register_statehealth_tools
    register_statehealth_tools(registry)
    from .tools_hostview import register_hostview_tools
    register_hostview_tools(registry)
    from .tools_secrets import register_secret_tools
    register_secret_tools(registry)
    from .tools_limits import register_limits_tools
    register_limits_tools(registry)
    from .tools_gatewayview import register_gatewayview_tools
    register_gatewayview_tools(registry)
    from .tools_packpeer import register_packpeer_tools
    register_packpeer_tools(registry)
