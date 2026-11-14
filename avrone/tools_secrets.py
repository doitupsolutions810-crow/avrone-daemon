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
                 ".ini", "",
                 # round 55: key files and config formats that hold credentials were never read
                 ".pem", ".key", ".conf", ".properties", ".tfvars", ".xml", ".ps1", ".bash", ".zsh"}
HITS_KEEP = 5000  # hit rows kept in memory per scan; counts and flagged paths stay exact (round 55)


def _wanted(name: str) -> bool:
    """A file secret_scan reads. Round 55: ".env.local", ".env.production" and "prod.env.bak" have
    suffixes ".local" / ".production" / ".bak", so the most common secret files were skipped."""
    low = name.lower()
    if low.startswith(".env") or ".env." in low or low.endswith(".env"):
        return True
    return Path(low).suffix in TEXT_SUFFIXES or is_run_log(name)


def _shapes_rules():
    from .secretshapes import CONTEXT_RULES
    return CONTEXT_RULES


def _kinds(line: str) -> list[str]:
    from . import ctxspawn
    # round 157: redact() skips every rule whose literals are absent (round 82 gates), so a line it
    # leaves unchanged holds nothing to report; the ungated search below cost ~0.4 s per MB, which
    # mattered once whole run logs (up to 64 MiB) are scanned
    try:
        if ctxspawn.redact(line) == line:
            return []
    except Exception:  # noqa: BLE001
        pass
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


class _Hits(list):
    """Round 55: every hit was kept, so a 256 MiB run log of short flagged lines built millions of
    dicts (GBs of memory) though only MAX_SHOWN are printed. Rows past HITS_KEEP are counted, and
    the set of flagged paths (what fix rewrites) stays complete."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0
        self.paths: set[str] = set()

    def append(self, h: dict[str, Any]) -> None:  # type: ignore[override]
        self.count += 1
        self.paths.add(h["path"])
        if len(self) < HITS_KEEP:
            super().append(h)


def scan(root: Path) -> dict[str, Any]:
    hits: _Hits = _Hits()
    files = scanned = total = 0
    total_cap = SCAN_TOTAL_MAX
    partial_files: list[str] = []
    from .safeio import NotRegularFile, TreeWalk, open_read
    # round 90: sorted(rglob("*")) listed the whole workspace (node_modules, .git included) before
    # scanning 2000 files; .git is now pruned while walking and at most SCAN_ENTRIES_MAX are read.
    walk = TreeWalk(root, SCAN_ENTRIES_MAX, skip=lambda n: n == ".git") if root.is_dir() else ()
    for rel, de in walk:
        if files >= MAX_FILES:
            break
        p = Path(de.path)
        try:
            if not de.is_file(follow_symlinks=False) or not _wanted(p.name):
                continue
        except OSError:
            continue
        files += 1
        cap = min(_file_cap(p), max(0, total_cap - total))
        try:  # O_NOFOLLOW|O_NONBLOCK: a FIFO or link swapped in after the check is not read
            with open_read(p) as f:
                n, partial = _scan_stream(f, cap, str(p.relative_to(root)), hits)
        except OSError:
            continue
        total += n
        scanned += 1
        if partial:
            partial_files.append(str(p.relative_to(root)))
    return {"files": scanned, "hits": hits, "partial": partial_files,
            "truncated": files >= MAX_FILES or bool(getattr(walk, "truncated", False))}


# Round 157 (v0.150): scan() read the first MAX_BYTES (2 MiB) of each file and reported the rest
# CLEAN. Run logs rotate at 64 MiB, so a secret written after the first 2 MiB of a run was never
# flagged, and `fix` (which only rewrites flagged logs) never removed it. Run logs and archives are
# now read whole up to FIX_MAX_BYTES; other files keep the 2 MiB cap. A file read only in part is
# reported, never silently CLEAN. Lines are split on "\n" only, the same split fix uses: the old
# splitlines() also split on U+2028, \x0b, \x1c ... so reported line numbers were wrong.
SCAN_TOTAL_MAX = 2 * 1024 * 1024 * 1024  # bytes read by one scan, all files together
LINE_CHUNK = 4 * 1024 * 1024  # one line is examined in pieces of this size, overlapping


def _file_cap(p: Path) -> int:
    return FIX_MAX_BYTES if is_run_log(p.name) else MAX_BYTES


def _scan_stream(f: Any, cap: int, rel: str, hits: list[dict[str, Any]]) -> tuple[int, bool]:
    """Scan up to `cap` bytes of an open binary file line by line. Returns (bytes read, partial)."""
    read = 0
    lineno = 1
    tail = b""  # the end of the previous piece of a long line, so a secret across pieces is seen
    flagged = False
    while read < cap:
        chunk = f.readline(min(LINE_CHUNK, cap - read))
        if not chunk:
            return read, False
        read += len(chunk)
        ends = chunk.endswith(b"\n")
        if not flagged:
            text = (tail + chunk).decode("utf-8", errors="replace")
            k = _kinds(text)
            # round 55: a secret JSON-escaped in an event line (an env value holding a quote or a
            # backslash) was invisible to the raw-text search, so the scan said CLEAN while fix
            # (which decodes lines) would have masked it; decode whole escaped JSON lines here too
            if not k and ends and not tail and _line_hides_secret(text.rstrip("\n")):
                k = ["json-escaped-secret"]
            if k:
                hits.append({"path": rel, "line": lineno, "kinds": sorted(set(k))})
                flagged = True
        if ends:
            lineno += 1
            tail, flagged = b"", False
        else:
            tail = chunk[-4096:]
    return read, bool(f.read(1))


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
        # round 55: two secret keys redact to the same "[REDACTED]", and the dict comprehension kept
        # only the last one: fix silently deleted the other entry from the run log
        out: dict[Any, Any] = {}
        for k, x in v.items():
            nk = _redact_value(k) if isinstance(k, str) else k
            if nk in out and nk != k:
                i = 2
                while f"{nk}#{i}" in out or f"{nk}#{i}" in v:
                    i += 1
                nk = f"{nk}#{i}"
            out[nk] = _redact_value(x)
        return out
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
    st = ln.lstrip(" \t")
    if st[:1] not in ("{", "[") or not any(e in ln for e in ('\\"', "\\\\", "\\u", "\\/")):
        # round 55: only escapes that can hide a secret's characters are worth decoding; "\n" alone
        # cannot, and a line opening with "[" or indentation was never checked
        return False
    ln = st
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


def _tsesc(x: Any) -> str:
    from .textsafe import escape
    return escape(str(x))


async def _jobs_db_report(path: Path, fix: bool) -> list[str]:
    import sqlite3
    from .jobqueue import scrub_db
    try:
        r = await asyncio.to_thread(scrub_db, path, fix)
    except sqlite3.Error as e:
        return [f"- jobs.db: NOT scanned ({_tsesc(type(e).__name__)}: {_tsesc(str(e)[:120])})"]
    out = []
    if r["flagged"]:
        out.append(f"- jobs.db: {r['flagged']} job(s) with a secret in the stored result or error"
                   + (f"; fixed {r['fixed']}" if fix else " (fix=true masks them)")
                   + ("" if not fix or r.get("wal_cleared", 1) else
                      "; the old values are still in jobs.db-wal (a reader held it), run fix again"))
    if r["payloads"]:
        out.append(f"- jobs.db: {r['payloads']} job payload(s) hold a secret (task input; never rewritten, fix by hand)")
    return out


async def t_secret_scan(ctx: "AgentContext", args: dict[str, Any]) -> str:
    scope = args.get("scope", "runs")
    scope = scope.strip().lower() if isinstance(scope, str) else scope  # round 55: " Runs" refused
    if scope not in ("runs", "data", "workspace"):
        return "ERROR: scope must be runs, data or workspace"
    from .argnum import tool_bool
    # round 61: entry points that skip check() (MCP, fleet, admin) pass fix="true"; `is True` ignored
    # it, so the scan reported FOUND and fixed nothing, and fix="true" with scope workspace was not refused
    fix = tool_bool(args.get("fix"), False)
    if fix and scope not in ("runs", "data"):
        return "ERROR: fix is only allowed for scope runs or data (run logs, bridge journals, memory, jobs.db); fix other files by hand"
    root = _roots(ctx, scope)
    if root is None:
        return f"secret_scan: no {scope} directory configured on this node"
    res = await asyncio.to_thread(scan, root)
    hits = res["hits"]
    nhits = getattr(hits, "count", len(hits))
    hpaths = set(getattr(hits, "paths", None) or {h["path"] for h in hits})
    head = (f"secret_scan [{scope}]: {'CLEAN' if not hits else 'FOUND'} - {nhits} line(s) in "
            f"{len(hpaths)} file(s); {res['files']} file(s) read"
            + (" (file cap reached)" if res["truncated"] else ""))
    if res.get("partial"):
        part = res["partial"]
        head += (f"; {len(part)} file(s) only partly read (size cap), the rest NOT scanned: "
                 + ", ".join(_tsesc(x) for x in part[:5]) + (" ..." if len(part) > 5 else ""))
    # round 157: hit paths were printed raw; a run log named "x.jsonl\n- CLEAN" forged report lines
    lines = [head] + [f"- {_tsesc(h['path'])}:{h['line']} {', '.join(h['kinds'])}" for h in hits[:MAX_SHOWN]]
    if nhits > MAX_SHOWN:
        lines.append(f"... {nhits - MAX_SHOWN} more")
    if scope == "data":  # round 159: jobs.db rows (results/errors stored raw before v0.148)
        lines += await _jobs_db_report(root / "jobs.db", fix)
        if any(ln.startswith("- jobs.db:") and "NOT scanned" not in ln for ln in lines) and not hits:
            lines[0] = lines[0].replace(": CLEAN -", ": FOUND -", 1)
    if hits and fix:
        lock = getattr(getattr(getattr(ctx, "core", None), "codex", None), "_write_lock", None)
        paths = set(hpaths)
        if scope == "runs":
            n = await asyncio.to_thread(fix_runs, root, paths, lock)
            nb = 0
        else:  # round 158: data scope fixes run logs under runs/ and bridge journals under bridge/
            n = await asyncio.to_thread(fix_runs, root / "runs",
                                        {x[5:] for x in paths if x.startswith("runs/") and "/" not in x[5:]}, lock)
            from .bridge import fix_journal
            core = getattr(ctx, "core", None)
            nb = 0
            if "memory.jsonl" in paths:  # round 163: memory values (the store's own lock and rewrite)
                from .memstore import MemoryStore
                try:
                    mr = await asyncio.to_thread(MemoryStore(root / "memory.jsonl").scrub)
                    lines.append(f"memory: masked {mr['values']} value(s)"
                                 + (f"; {mr['keys']} key(s) hold a secret (keys are not rewritten, "
                                    "fix them by hand)" if mr["keys"] else ""))
                except OSError as e:
                    lines.append(f"memory: NOT fixed ({type(e).__name__})")
            for x in sorted(paths):
                if x.startswith("bridge/") and "/" not in x[7:]:
                    try:
                        nb += await asyncio.to_thread(fix_journal, root / x, core)
                    except OSError:
                        pass
        after = await asyncio.to_thread(scan, root)
        lines.append(f"fixed {n} run log(s)" + (f" and {nb} bridge journal(s)" if scope == "data" else "")
                     + f"; {getattr(after['hits'], 'count', len(after['hits']))} line(s) still flagged")
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
    # round 61: a serverInfo that is not an object raised AttributeError outside the try, and the
    # protocol, server name and tool names were printed raw (a name holding a newline forged a line)
    if not isinstance(srv, dict):
        srv = {}
        failed.append("serverInfo")
    return "\n".join([f"mcp_selftest: {'PASS' if not failed else 'FAIL'} - protocol {_tsesc(r['protocol'])}, "
                      f"{_tsesc(srv.get('name', '?'))} {_tsesc(srv.get('version', '?'))}, {len(r['tools'])} tool(s): "
                      + ", ".join(_tsesc(t) for t in r["tools"])]
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
