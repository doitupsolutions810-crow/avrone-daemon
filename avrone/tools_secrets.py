"""secret_scan + mcp_selftest.

    secret_scan {"scope"?: "runs"|"data"|"workspace", "fix"?: bool}
        Find secret-shaped strings (env secret values, bearer/API keys, private keys, URL credentials)
        in this node's own files: run logs (default), the whole data dir, or the workspace. Reports
        path, line and kind only - never the value. fix=true (runs scope only) rewrites affected run
        logs with the same redaction codex now applies on write (atomic, per file). Bounded: at most
        MAX_FILES files and MAX_BYTES per file are read.

    mcp_selftest {}
        Drive this node's MCP server in-process (no subprocess, no network): initialize, ping,
        tools/list, and three malformed requests that must come back as JSON-RPC errors. Reports
        the protocol version, the advertised tools and any check that failed.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

MAX_FILES = 2000
MAX_BYTES = 2 * 1024 * 1024
SCAN_ENTRIES_MAX = 200_000  # directory entries read by one scan
MAX_SHOWN = 30
TEXT_SUFFIXES = {".jsonl", ".json", ".log", ".txt", ".md", ".py", ".sh", ".env", ".yml", ".yaml", ".toml", ".cfg",
                 ".ini", ""}


def _shapes_rules():
    from .secretshapes import CONTEXT_RULES
    return CONTEXT_RULES


def _kinds(line: str) -> list[str]:
    from . import ctxspawn
    found = []
    try:
        for v in ctxspawn._env_secret_values():
            if v in line:
                found.append("env-secret-value")
                break
    except Exception:  # noqa: BLE001
        pass
    names = ["private-key", "auth-header", "bearer", "secret-assignment", "api-key", "github-token", "slack-token",
             "aws-key", "google-key", "jwt", "url-credentials", "vendor-token"] + [n for *_x, n in _shapes_rules()]
    for (pat, _rep), name in zip(ctxspawn._PATTERNS, names):
        if pat.search(line):
            found.append(name)
    return found


def _roots(ctx: "AgentContext", scope: str) -> Path | None:
    core = getattr(ctx, "core", None)
    settings = getattr(core, "settings", None)
    if scope == "workspace":
        ws = getattr(getattr(core, "runner", None), "workspace", None) or getattr(settings, "workspace", None)
        return Path(ws) if ws else None
    dd = getattr(settings, "data_dir", None)
    if dd is None:
        return None
    return Path(dd) / "runs" if scope == "runs" else Path(dd)


def scan(root: Path) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    files = scanned = 0
    from .safeio import NotRegularFile, TreeWalk, open_read
    # round 90: sorted(rglob("*")) listed the whole workspace (node_modules, .git included) before
    # scanning 2000 files; .git is now pruned while walking and at most SCAN_ENTRIES_MAX are read.
    walk = TreeWalk(root, SCAN_ENTRIES_MAX, skip=lambda n: n == ".git") if root.is_dir() else ()
    for rel, de in walk:
        if files >= MAX_FILES:
            break
        p = Path(de.path)
        try:
            if not de.is_file(follow_symlinks=False) or (
                    p.suffix.lower() not in TEXT_SUFFIXES and not is_run_log(p.name)):
                continue
        except OSError:
            continue
        files += 1
        try:  # O_NOFOLLOW|O_NONBLOCK: a FIFO or link swapped in after the check is not read
            with open_read(p) as f:
                data = f.read(MAX_BYTES)
        except OSError:
            continue
        scanned += 1
        for i, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            k = _kinds(line)
            if k:
                hits.append({"path": str(p.relative_to(root)), "line": i, "kinds": sorted(set(k))})
    return {"files": scanned, "hits": hits,
            "truncated": files >= MAX_FILES or bool(getattr(walk, "truncated", False))}


_ARCHIVE_RE = None


def is_run_log(name: str) -> bool:
    """<run>.jsonl or a rotated archive <run>.jsonl.N (round 90: archives were never scanned, so
    a secret that rotated out of the active log was reported CLEAN and never fixed)."""
    global _ARCHIVE_RE
    if _ARCHIVE_RE is None:
        import re
        _ARCHIVE_RE = re.compile(r".+\.jsonl(\.[1-9][0-9]{0,3})?")
    return bool(_ARCHIVE_RE.fullmatch(name))


FIX_MAX_BYTES = 256 * 1024 * 1024  # a run log bigger than this is not rewritten in memory


def _redact_value(v: Any) -> Any:
    from .ctxspawn import redact
    if isinstance(v, str):
        return redact(v) if _kinds(v) else v
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    if isinstance(v, dict):
        return {(_redact_value(k) if isinstance(k, str) else k): _redact_value(x) for k, x in v.items()}
    return v


def _redact_line(ln: str) -> str:
    """Round 151: a flagged JSON event line is redacted value by value and dumped again. Redacting
    the raw line broke it: a private key without its END marker (tool output cut at a cap) is
    masked to the end of the text, which took the line's closing quotes and braces, so every run
    log reader skipped the event. A secret JSON-escaped in the line (an env value holding a quote
    or backslash) was also never matched. Lines that are not JSON are redacted as text."""
    from .ctxspawn import redact
    s = ln.rstrip("\r")
    if s[:1] in ("{", "["):
        from .safeio import json_loads
        try:
            obj = json_loads(s)
        except ValueError:
            obj = None
        if isinstance(obj, (dict, list)):
            new = _redact_value(obj)
            if new != obj:
                return json.dumps(new) + ln[len(s):]
    return redact(ln)


def _redact_text(text: str) -> str:
    return "\n".join(_redact_line(ln) if _kinds(ln) or _line_hides_secret(ln) else ln for ln in text.split("\n"))


def _line_hides_secret(ln: str) -> bool:
    """A JSON line whose decoded strings hold a secret the raw text hides (escaped quotes etc.)."""
    if not ln.startswith("{") or "\\" not in ln:
        return False
    from .safeio import json_loads
    try:
        obj = json_loads(ln.rstrip("\r"))
    except ValueError:
        return False
    return _redact_value(obj) != obj


def fix_runs(root: Path, paths: set[str], lock: Any = None) -> int:
    """Rewrite flagged run logs with secrets redacted. Round 90: the log is read and redacted
    outside `lock` (the Codex write lock), then, holding it, any events appended meanwhile are
    redacted and carried over before the swap. Rewriting without it dropped every event emitted
    while the file was being redacted, and replacing a log that had just rotated resurrected the
    archived content as the active log. Reads go through read_capped (regular files only)."""
    import contextlib
    from .safeio import FileTooBig, read_capped, tmp_prefix
    fixed = 0
    for rel in sorted(paths):
        p = root / rel
        if not is_run_log(p.name) or p.is_symlink() or not p.is_file():
            continue
        try:
            st0 = os.lstat(p)
            data = read_capped(p, FIX_MAX_BYTES)
            if data is None:
                continue
            # round 151: surrogateescape keeps bytes that are not UTF-8 (a torn write) exactly as
            # they were; "replace" turned them into U+FFFD in lines that held no secret at all
            text = data.decode("utf-8", errors="surrogateescape")
            new = _redact_text(text)
            if new == text:
                continue
            from .safeio import mkstemp_beside
            fd, tmp = mkstemp_beside(p)  # round 116: long names; round 121: errors name p
            try:
                with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                    f.write(new)
                    with (lock if lock is not None else contextlib.nullcontext()):
                        try:
                            st1 = os.lstat(p)
                        except FileNotFoundError:
                            st1 = None
                        if st1 is None or st1.st_ino != st0.st_ino or st1.st_size < len(data):
                            raise _Moved()  # rotated, replaced or truncated meanwhile: leave it
                        if st1.st_size > len(data):
                            from .safeio import open_read  # round 106: never a swapped-in FIFO
                            with open_read(p) as src:
                                if os.fstat(src.fileno()).st_ino != st1.st_ino:
                                    raise _Moved()
                                src.seek(len(data))
                                tail = src.read(st1.st_size - len(data))
                            f.write(_redact_text(tail.decode("utf-8", errors="surrogateescape")))
                        f.flush()
                        os.fsync(f.fileno())  # round 151: a crash after the swap left an empty log
                        os.chmod(tmp, st1.st_mode & 0o777)
                        os.replace(tmp, p)
            except _Moved:
                Path(tmp).unlink(missing_ok=True)
                continue
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            fixed += 1
        except (OSError, FileTooBig):
            continue
    return fixed


class _Moved(Exception):
    pass


async def t_secret_scan(ctx: "AgentContext", args: dict[str, Any]) -> str:
    scope = args.get("scope", "runs")
    if scope not in ("runs", "data", "workspace"):
        return "ERROR: scope must be runs, data or workspace"
    if args.get("fix") is True and scope != "runs":
        return "ERROR: fix is only allowed for scope runs (run logs); fix other files by hand"
    root = _roots(ctx, scope)
    if root is None:
        return f"secret_scan: no {scope} directory configured on this node"
    res = await asyncio.to_thread(scan, root)
    hits = res["hits"]
    head = (f"secret_scan [{scope}]: {'CLEAN' if not hits else 'FOUND'} - {len(hits)} line(s) in "
            f"{len({h['path'] for h in hits})} file(s); {res['files']} file(s) read"
            + (" (file cap reached)" if res["truncated"] else ""))
    lines = [head] + [f"- {h['path']}:{h['line']} {', '.join(h['kinds'])}" for h in hits[:MAX_SHOWN]]
    if len(hits) > MAX_SHOWN:
        lines.append(f"... {len(hits) - MAX_SHOWN} more")
    if hits and args.get("fix") is True:
        lock = getattr(getattr(getattr(ctx, "core", None), "codex", None), "_write_lock", None)
        n = await asyncio.to_thread(fix_runs, root, {h["path"] for h in hits}, lock)
        after = await asyncio.to_thread(scan, root)
        lines.append(f"fixed {n} run log(s); {len(after['hits'])} line(s) still flagged")
    return "\n".join(lines)


async def mcp_selftest() -> dict[str, Any]:
    from . import mcp_server
    from .tools import builtin_registry

    async def fac():
        class _Core:
            tools = builtin_registry()
        return _Core(), {}

    class _Out(io.BytesIO):
        def flush(self) -> None:
            pass
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": [1]},
            {"jsonrpc": "2.0", "id": 5, "method": 7},
            {"jsonrpc": "2.0", "id": 6, "method": "no/such"}]
    r = asyncio.StreamReader()
    for q in reqs:
        r.feed_data((json.dumps(q) + "\n").encode())
    r.feed_eof()
    out = _Out()
    await mcp_server.AvroneMCPServer(fac).serve(r, out)
    got = {m.get("id"): m for m in (json.loads(x) for x in out.getvalue().decode().splitlines())}
    checks = {
        "initialize": "result" in got.get(1, {}),
        "ping": got.get(2, {}).get("result") == {},
        "tools/list": bool(got.get(3, {}).get("result", {}).get("tools")),
        "bad params -> -32602": got.get(4, {}).get("error", {}).get("code") == -32602,
        "bad method -> -32600": got.get(5, {}).get("error", {}).get("code") == -32600,
        "unknown method -> -32601": got.get(6, {}).get("error", {}).get("code") == -32601,
    }
    return {"protocol": got.get(1, {}).get("result", {}).get("protocolVersion"),
            "server": got.get(1, {}).get("result", {}).get("serverInfo", {}),
            "tools": [t.get("name") for t in got.get(3, {}).get("result", {}).get("tools", [])],
            "checks": checks}


async def t_mcp_selftest(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        r = await mcp_selftest()
    except Exception as e:  # noqa: BLE001
        return f"mcp_selftest: FAIL - server raised {type(e).__name__}"
    failed = [k for k, ok in r["checks"].items() if not ok]
    srv = r["server"]
    return "\n".join([f"mcp_selftest: {'PASS' if not failed else 'FAIL'} - protocol {r['protocol']}, "
                      f"{srv.get('name', '?')} {srv.get('version', '?')}, {len(r['tools'])} tool(s): "
                      + ", ".join(r["tools"])]
                     + [f"- {k}: {'ok' if ok else 'FAILED'}" for k, ok in r["checks"].items()])


SECRET_TOOLS = [
    Tool("secret_scan", "read-only by default: find secret-shaped strings in this node's run logs, data dir or "
         "workspace (path:line and kind, never the value); fix=true redacts run logs in place.",
         '{"scope"?: "runs"|"data"|"workspace", "fix"?: bool}', t_secret_scan, source="builtin",
         schema={"optional": {"scope": "str", "fix": "bool"}}),
    Tool("mcp_selftest", "read-only: drive this node's MCP server in-process and check the protocol handshake, "
         "tool list and error handling.", "{}", t_mcp_selftest, source="builtin", schema={}),
]


def register_secret_tools(registry) -> None:
    for t in SECRET_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
