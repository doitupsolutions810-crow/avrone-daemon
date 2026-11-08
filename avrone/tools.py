"""Tool registry. Every tool an agent can call lives here: built-ins, plugins, MCP tools.

Contract:
    async def fn(ctx: AgentContext, args: dict) -> str
    registry.register(Tool(name, description, args_doc, fn, source="builtin"))
"""
from __future__ import annotations

import asyncio
import logging
import difflib
import functools
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

if TYPE_CHECKING:
    from .trinity import AgentContext

ToolFn = Callable[["AgentContext", dict[str, Any]], Awaitable[str]]



def _drive_sync(fn: Any, ctx: Any, args: Any) -> Any:
    """Run a coroutine function that never awaits to completion on this (worker) thread."""
    coro = fn(ctx, args)
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    coro.close()
    raise RuntimeError(f"{getattr(fn, '__name__', fn)} awaited; it cannot run off the event loop")


def off_loop(fn: Any) -> Any:
    """Run an await-free async tool in a worker thread. Tools that parse whole memory/CodeX logs
    (100k+ JSON lines on a busy node) held the event loop for over a second per call, stalling every
    agent, the API, WebSockets and fleet heartbeats; in a thread the loop keeps getting the GIL."""
    @functools.wraps(fn)
    async def wrapper(ctx: Any, args: Any) -> Any:
        return await asyncio.to_thread(_drive_sync, fn, ctx, args)
    wrapper.off_loop = True  # type: ignore[attr-defined]
    return wrapper



def _safe_str(v: Any) -> str:
    try:
        return str(v)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not escape the tool wrapper
        return f"<{type(v).__name__}>"


def _as_text(out: Any) -> str:
    return out if isinstance(out, str) else _safe_str(out)


async def _invoke(fn: Any, ctx: Any, args: Any) -> Any:
    """Call a tool body. A plugin's plain `def` tool used to fail every call with "object str can't
    be used in 'await' expression"; it now runs in a worker thread. A tool calling sys.exit() used to
    raise SystemExit through the agent loop and take the node's event loop down; callers now catch it."""
    if inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(getattr(fn, "__call__", None)):
        return await fn(ctx, args)
    res = await asyncio.to_thread(fn, ctx, args)
    if inspect.isawaitable(res):  # a sync factory returning a coroutine (functools.partial etc.)
        res = await res
    return res


@dataclass
class Tool:
    name: str
    description: str
    args_doc: str  # e.g. '{"cmd": str}'
    fn: ToolFn
    source: str = "builtin"  # builtin | plugin:<name> | mcp:<server>
    # Optional arg schema, checked before the call so a bad call never runs:
    #   {"required": {"path": "str"}, "optional": {"limit": "int"}}
    # Types: str | int | float | bool | list | dict, or alternatives joined by "|"
    # ("str|dict"). Unknown keys are allowed. None = no validation (plugins, MCP...).
    schema: dict[str, dict[str, str]] | None = None


_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,), "int": (int,), "float": (int, float), "bool": (bool,),
    "list": (list,), "dict": (dict,), "any": (object,),
}

# Schemas for the core tools. Attached at register() time to builtin tools that don't
# declare their own, so the tool tables in tools_extra.py etc. stay untouched.
CORE_SCHEMAS: dict[str, dict[str, dict[str, str]]] = {
    "shell": {"required": {"cmd": "str"}},
    "python": {"required": {"code": "str"}},
    "read_file": {"required": {"path": "str"}, "optional": {"offset": "int|str"}},
    "write_file": {"required": {"path": "str", "content": "str|dict|list"}},
    "edit_file": {"required": {"path": "str", "old": "str", "new": "str"},
                  "optional": {"replace_all": "bool|str"}},
    "fetch": {"required": {"url": "str"}},
    "web_search": {"required": {"query": "str"}, "optional": {"max_results": "int"}},
    "spawn": {"required": {"task": "str"}, "optional": {"role": "str", "context": "str", "focus": "str",
                                                       "share_dir": "bool"}},
    "list_files": {"optional": {"path": "str", "depth": "int"}},
    "search_files": {"required": {"pattern": "str"}, "optional": {"path": "str"}},
    "http_request": {"required": {"method": "str", "url": "str"},
                     "optional": {"headers": "dict", "body": "str|dict"}},
    "git": {"required": {"args": "str"}},
    "think": {"required": {"thought": "str"}},
}


def _type_ok(value: Any, spec: str) -> bool:
    for name in str(spec).split("|"):
        types = _TYPES.get(name.strip())
        if types is None:
            return True  # unknown type word in a schema: don't block the call on it
        if isinstance(value, bool) and name.strip() in ("int", "float"):
            continue  # True is an int in Python, never in a tool schema
        if isinstance(value, types):
            return True
    return False


_DOC_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"(\?)?\s*:\s*(str|int|float|bool)\b(?!\s*\|)')


def infer_schema(args_doc: str) -> dict[str, dict[str, str]] | None:
    """A conservative schema from a builtin tool's args_doc: only top-level REQUIRED keys of a plain
    scalar type become required (so a missing "host" is an ARG ERROR before the tool runs). Optional
    keys are left to the tool body (models often send "5" for an int). Nothing to require gives {}
    (an object is still required); docs with alternatives ("... or {...}") give None (no pre-check)."""
    doc = str(args_doc or "").strip()
    if not doc.startswith("{") or " or {" in doc:
        return None
    depth, top = 0, []
    for ch in doc:  # keep only depth-1 text so nested {"path": str} inside lists is ignored
        if ch in "{[":
            depth += 1
            top.append(" " if depth > 1 else ch)
            continue
        if ch in "}]":
            depth -= 1
            top.append(" ")
            continue
        top.append(ch if depth == 1 else " ")
    req = {k: t for k, opt, t in _DOC_KEY_RE.findall("".join(top)) if not opt}
    return {"required": req} if req else {}


def validate_args(schema: dict[str, dict[str, str]] | None, args: Any) -> list[str]:
    """Problems with `args` against `schema` (empty list = fine)."""
    if schema is None:
        return []
    if not isinstance(args, dict):  # {} still means "an object, any keys"
        return [f"args must be a JSON object, got {type(args).__name__}"]
    if not schema:
        return []
    problems: list[str] = []
    for key, spec in (schema.get("required") or {}).items():
        if key not in args or args[key] is None:
            problems.append(f"missing '{key}'")
        elif not _type_ok(args[key], spec):
            problems.append(f"'{key}' must be {spec}, got {type(args[key]).__name__}")
    for key, spec in (schema.get("optional") or {}).items():
        if args.get(key) is not None and not _type_ok(args[key], spec):
            problems.append(f"'{key}' must be {spec}, got {type(args[key]).__name__}")
    return problems


UNKNOWN_NAME_MAX = 120


def clip_tool_name(name: Any) -> str:
    """An unknown tool name as it may be echoed back: at most UNKNOWN_NAME_MAX chars plus a count.
    A model that put a 1 MB string in "tool" got the whole string back in the error, in the
    codex tool.call/tool.arg_error events and in its next prompt as "TOOL RESULT (<1 MB>)",
    every turn it repeated it (round 80)."""
    s = str(name)
    return s if len(s) <= UNKNOWN_NAME_MAX else f"{s[:UNKNOWN_NAME_MAX]}...(+{len(s) - UNKNOWN_NAME_MAX} chars)"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+", tool.name):
            raise ValueError(f"bad tool name: {tool.name!r}")
        if tool.schema is None and tool.source == "builtin" and tool.name in CORE_SCHEMAS:
            tool.schema = CORE_SCHEMAS[tool.name]
        if tool.schema is None and tool.source == "builtin":
            tool.schema = infer_schema(tool.args_doc)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list(self) -> list[dict[str, str]]:
        return [{"name": t.name, "description": t.description, "args": t.args_doc, "source": t.source}
                for t in sorted(self._tools.values(), key=lambda t: t.name)]

    def docs(self) -> str:
        lines = ["Available tools (one call per reply, or a small batch of independent calls):"]
        for t in sorted(self._tools.values(), key=lambda t: t.name):
            lines.append(f"- {t.name} {t.args_doc}: {t.description}")
        return "\n".join(lines) + "\n"

    def suggest(self, name: str, n: int = 3) -> list[str]:
        """Registered tool names closest to `name` (for typos like 'shel' or 'readfile')."""
        if len(str(name)) > UNKNOWN_NAME_MAX:  # no registered name is this long; skip difflib
            return []
        names = self.names()
        lowered = {t.lower(): t for t in names}
        hits = difflib.get_close_matches(str(name).lower(), list(lowered), n=n, cutoff=0.6)
        return [lowered[h] for h in hits]

    def unknown_message(self, name: str) -> str:
        close = self.suggest(name)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"ERROR: unknown tool '{clip_tool_name(name)}'.{hint} Known: {', '.join(self.names())}"

    def check(self, name: str, args: Any) -> tuple[str, str] | None:
        """Pre-call check: None if the call may run, else (reason, message for the model).

        reason is "unknown_tool" or "bad_args"; the tool is NOT run in either case."""
        tool = self.get(name)
        if tool is None:
            return "unknown_tool", self.unknown_message(name)
        problems = validate_args(tool.schema, args)
        if problems:
            return "bad_args", (f"ARG ERROR ({name}): {'; '.join(problems)}; expected {tool.args_doc}. "
                                "The tool did not run; resend the call with fixed args.")
        return None

    async def call(self, ctx: "AgentContext", name: str, args: dict[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            return self.unknown_message(name)
        if args is None:
            args = {}
        # Defense in depth for entry points that skip check() (MCP, fleet, admin, direct calls):
        # a tool body never sees a list/str where it expects an object. (Typed per-field checks stay
        # in check() and in each tool, which give the model their own, more specific messages.)
        if not isinstance(args, dict):
            return (f"ARG ERROR ({name}): args must be a JSON object, got {type(args).__name__}; "
                    f"expected {tool.args_doc}. The tool did not run.")
        from . import capgate  # opt-in capability gate (AVRONE_CAPGATE); a no-op when off
        if capgate.mode() == "off":
            try:
                out = await _invoke(tool.fn, ctx, args)
            except (Exception, SystemExit) as e:  # errors go back to the agent
                return f"ERROR: {type(e).__name__}: {_safe_str(e)}"
            return _as_text(out)
        allowed, why, cap_id = capgate.gate(ctx, name)
        ddir = capgate.data_dir_of(ctx)
        caller = getattr(ctx, "name", "") or "agent"
        run_id = getattr(ctx, "run_id", "") or ""
        if not allowed:
            msg = f"CAP DENIED ({name}): {why}. The tool did not run."
            if ddir is not None:
                await _receipt(ddir, caller=caller, tool=name, args=args, outcome="denied",
                               output=msg, cap_id=cap_id, run_id=run_id)
            return msg
        outcome = "ok"
        try:
            out = await _invoke(tool.fn, ctx, args)
        except (Exception, SystemExit) as e:  # errors go back to the agent
            out, outcome = f"ERROR: {type(e).__name__}: {_safe_str(e)}", "error"
        out = _as_text(out)
        if ddir is not None:
            await _receipt(ddir, caller=caller, tool=name, args=args,
                           outcome=outcome if not why else f"{outcome}+audit", output=out,
                           cap_id=cap_id, run_id=run_id)
        return out


async def _receipt(ddir: Any, **kw: Any) -> None:
    """capgate.record off the event loop, never raising into the tool call (round 79).

    record() takes a cross-process flock, hashes the args and output (an 8 MB read_file result
    hashed on the loop) and fsyncs twice. It ran inline on every gated call, so another process
    holding the chain lock froze the whole node, and an unwritable chain (disk full, a dir where
    receipts.jsonl should be) raised out of a tool that had already run, losing its output. The
    failure is logged; the receipt chain's own verify_chain shows any gap."""
    from . import capgate
    try:
        await asyncio.to_thread(capgate.record, ddir, **kw)
    except Exception:  # noqa: BLE001
        logging.getLogger("avrone.capgate").warning("capgate receipt not recorded for %s",
                                                    kw.get("tool"), exc_info=True)


MAX_NAME_BYTES = 255  # one path component (NAME_MAX on Linux/macOS)


def check_path_text(rel: Any) -> str:
    """A path an agent gave, refused with a plain message when the OS could never hold it.

    A lone surrogate (cut emoji) or a 300-char file name used to surface as raw
    'UnicodeEncodeError: utf-8 codec ...' / 'OSError: [Errno 36] File name too long' from
    read_file, write_file, edit_file and artifact_save."""
    s = str(rel)
    if re.search("[\ud800-\udfff]", s):
        raise ValueError("path contains an invalid character (a lone UTF-16 surrogate)")
    if "\x00" in s:
        raise ValueError("path contains a NUL byte")
    for part in s.replace("\\", "/").split("/"):
        if len(part.encode("utf-8")) > MAX_NAME_BYTES:
            raise ValueError(f"path component is {len(part.encode('utf-8'))} bytes; the limit is {MAX_NAME_BYTES}")
    return s


def _safe_path(root: Path, rel: str) -> Path:
    try:
        p = (root / check_path_text(rel).lstrip("/")).resolve()
        r = root.resolve()
    except RuntimeError:  # 3.12 symlink loop; its message names the host's absolute path
        raise ValueError("path has a symlink loop") from None
    except OSError as e:
        raise ValueError("path has a symlink loop" if e.errno == __import__("errno").ELOOP else "path cannot be resolved") from None
    if not p.is_relative_to(r):
        raise ValueError("path escapes the workspace")
    return p


async def run_for_agent(ctx: "AgentContext", cmd: str, timeout: int | None = None):
    """Run `cmd` for this agent: in its own persistent dir when it has one, else as before.

    Runners that predate per-agent cwd (no `cwd` parameter) keep their old behavior."""
    runner = ctx.core.runner
    agent_dir = getattr(ctx, "agent_dir", None)
    if agent_dir is not None and runner_accepts_cwd(runner):
        return await runner.run(cmd, timeout=timeout, cwd=agent_dir)
    return await runner.run(cmd, timeout=timeout) if timeout else await runner.run(cmd)


def runner_accepts_cwd(runner: Any) -> bool:
    """True if `runner.run` takes a per-agent `cwd` (runners predating v0.16 do not)."""
    try:
        return "cwd" in inspect.signature(runner.run).parameters
    except (TypeError, ValueError):
        return False


async def t_shell(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not ctx.core.settings.agents_can_shell:
        return "ERROR: shell is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    cmd = args.get("cmd")
    cmd = "" if cmd is None else str(cmd)
    if not cmd.strip():
        return "ERROR: shell needs a non-empty 'cmd'"
    res = await run_for_agent(ctx, cmd)
    if _INSTALL_RE.search(cmd):  # self-change: record it in the resource ledger
        try:
            from .agentkit import record_change
            await asyncio.to_thread(record_change, ctx, "shell install", cmd[:200], res.exit_code == 0,
                                    (res.stdout or res.stderr)[-200:])
        except Exception:  # noqa: BLE001
            pass
    return res.text()


_INSTALL_RE = __import__("re").compile(
    r"\b(apt(-get)?\s+install|pip3?\s+install|python3?\s+-m\s+pip\s+install|npm\s+(i|install)|apk\s+add|yum\s+install|dnf\s+install|brew\s+install|cargo\s+install|go\s+install)\b")


READ_MAX_CHARS = 12000
FETCH_MAX_BYTES = 2 * 1024 * 1024
FETCH_TRANSPORT: Any = None  # tests inject an httpx.MockTransport here
_TEXTY = ("text/", "json", "xml", "javascript", "yaml", "csv", "x-sh", "markdown", "html")


def _offset(raw: Any) -> int:
    try:
        return max(0, int(str(raw).strip() or 0))
    except (TypeError, ValueError):
        return 0


DIR_LISTING_SCAN_MAX = 20_000

def _dir_listing(p: Path, limit: int = 30) -> str:
    import os as _os
    names, extra = [], False
    try:  # round 89: bounded scandir (iterdir + is_dir stat'ed every entry of a 1M-entry dir)
        with _os.scandir(p) as it:
            for de in it:
                if len(names) >= DIR_LISTING_SCAN_MAX:
                    extra = True
                    break
                try:
                    d = de.is_dir()
                except OSError:
                    d = False
                names.append(de.name + ("/" if d else ""))
    except OSError:
        return ""
    names.sort()
    if extra:
        return ", ".join(names[:limit]) + f", ... (over {DIR_LISTING_SCAN_MAX} entries)"
    more = f", ... (+{len(names) - limit} more)" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more if names else "(empty)"


def write_content(raw: Any) -> str:
    """write_file content as text. Before round 24 a dict/list (common when a model writes a
    .json file) became a Python repr ("{'a': 1}"), and a lone surrogate aborted the write."""
    import json as _json
    if raw is None:
        text = ""
    elif isinstance(raw, str):
        text = raw
    elif isinstance(raw, (dict, list)):
        text = _json.dumps(raw, indent=2, ensure_ascii=False, default=str) + "\n"
    elif isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = str(raw)
    return re.sub(r"[\ud800-\udfff]", "\ufffd", text)


def atomic_write_bytes(p: Path, data: bytes) -> None:
    """Write `data` to `p` atomically (tmp + os.replace). Keeps an existing file's permission
    bits (mkstemp creates 0600, which would strip a script's exec bit and hide it from other
    users); a new file gets the normal 0666 & ~umask."""
    import os as _os
    import stat as _stat
    import tempfile as _tempfile
    try:
        # round 109: lstat, and drop setuid/setgid. p.stat() followed a symlink swapped in after
        # the path was resolved and copied a host file's mode (e.g. a setuid binary's 04755) onto
        # agent-written content; and a rewrite kept S_ISUID/S_ISGID, which the kernel itself drops
        # when a file's content is changed by write()
        st = p.lstat()
        if _stat.S_ISLNK(st.st_mode):
            raise OSError("symlink: use the default mode")
        mode = _stat.S_IMODE(st.st_mode) & ~(_stat.S_ISUID | _stat.S_ISGID)
    except OSError:
        um = _os.umask(0)
        _os.umask(um)
        mode = 0o666 & ~um
    from .safeio import tmp_prefix as _tmp_prefix
    fd, tmp = _tempfile.mkstemp(dir=p.parent, prefix=_tmp_prefix(p.name), suffix=".tmp")
    try:
        with _os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _os.chmod(tmp, mode)
        _os.replace(tmp, p)
    except BaseException:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


async def t_read(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Bounded, honest read: never loads a huge file whole, says when it truncated and how to
    page on (offset), refuses binary instead of returning NUL soup, and lists a directory
    instead of raising IsADirectoryError."""
    from . import fsunify
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "ERROR: read_file needs a file path (use list_files to see what is there)"
    p = fsunify.resolve(ctx, raw_path, "read_file").path
    if not p.exists():
        return f"ERROR: {raw_path} not found"
    if p.is_dir():
        return f"ERROR: {raw_path} is a directory, not a file. Entries: {_dir_listing(p)}"
    if not p.is_file():
        return f"ERROR: {raw_path} is not a regular file"
    off = _offset(args.get("offset"))
    from .safeio import NotRegularFile, open_read
    try:  # round 87: a FIFO swapped in after is_file() parked a pool thread forever in open()
        fh = open_read(p, follow=True)
    except NotRegularFile:
        return f"ERROR: {raw_path} is not a regular file"
    import os as _os
    size = _os.fstat(fh.fileno()).st_size
    with fh:
        head = fh.read(8192)
        if b"\x00" in head:
            return (f"ERROR: {raw_path} is a binary file ({size} bytes); inspect it with shell "
                    f"(e.g. `file {raw_path}` or `xxd {raw_path} | head`)")
        fh.seek(0)
        data = fh.read((off + READ_MAX_CHARS) * 4 + 4)
    text = data.decode("utf-8", "replace")
    if data and len(data) < size:  # a cut multi-byte char at the end of the window
        text = text.rstrip("\ufffd")
    chunk = text[off:off + READ_MAX_CHARS]
    end = off + len(chunk)
    complete = len(data) >= size and end >= len(text)
    if complete and off == 0:
        return chunk
    if not chunk and off:
        return f"ERROR: offset {off} is past the end of {raw_path} ({size} bytes)"
    note = (f"\n[truncated: showing chars {off}-{end} of a {size}-byte file; "
            f"call read_file with offset={end} for more]") if not complete else \
        f"\n[end of file: chars {off}-{end}]"
    return chunk + note


async def t_write(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from . import fsunify
    raw_path = str(args.get("path") or "").strip()
    if not raw_path or raw_path.endswith(("/", "\\")):
        return "ERROR: write_file needs a file path (got a directory or nothing)"
    r = fsunify.resolve(ctx, raw_path, "write_file")
    p = r.path
    content = write_content(args.get("content"))
    if not r.in_agent and p.is_dir():  # (agent dirs: quota_write refuses and audits it)
        return f"ERROR: {raw_path} is a directory; give a file path inside it"
    if r.in_agent:
        from .agentfs_tools import quota_write
        # quota_write walks the whole agent dir under a thread lock: never on the event loop
        size, used, quota = await asyncio.to_thread(quota_write, ctx, r.root, p, content.encode("utf-8"),
                                                    False, "write_file")
        return f"wrote {fsunify.display(ctx, p)} ({size} bytes; agent dir {used}/{quota} bytes)"
    p.parent.mkdir(parents=True, exist_ok=True)
    # atomic: a crash or full disk mid-write never leaves a half-written file behind
    atomic_write_bytes(p, content.encode("utf-8"))
    if r.unified:
        return f"wrote {fsunify.display(ctx, p)} ({p.stat().st_size} bytes)"
    return f"wrote {p.relative_to(ctx.core.runner.workspace.resolve())} ({p.stat().st_size} bytes)"


def clean_url(raw: Any) -> str:
    u = str(raw or "").strip().strip("<>\"'` ").strip()
    return u


async def t_fetch(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Bounded GET: the body is streamed and capped (a model fetching a 4 GB ISO no longer
    loads it into memory), binary bodies are summarised, entities are unescaped."""
    import html as _html
    if not ctx.core.settings.allow_network:
        return "ERROR: network access is disabled."
    url = clean_url(args.get("url"))
    if not re.match(r"^https?://", url, re.I):
        return "ERROR: url must start with http(s)://"
    kw: dict[str, Any] = {"timeout": 30, "follow_redirects": True}
    if FETCH_TRANSPORT is not None:
        kw["transport"] = FETCH_TRANSPORT
    try:
        async with httpx.AsyncClient(**kw) as c:
            async with c.stream("GET", url, headers={"User-Agent": "AVRONE/0.2"}) as r:
                ctype = r.headers.get("content-type", "").lower()
                buf = bytearray()
                cut = False
                async for part in r.aiter_bytes():
                    buf += part
                    if len(buf) > FETCH_MAX_BYTES:
                        cut = True
                        del buf[FETCH_MAX_BYTES:]
                        break
                status, encoding = r.status_code, r.encoding or "utf-8"
    except httpx.TimeoutException:
        return f"ERROR: fetch timed out after 30s: {url}"
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
        return f"ERROR: bad url {url!r}: {e}"
    except httpx.HTTPError as e:
        return f"ERROR: fetch failed ({type(e).__name__}): {e}"
    is_text = (not ctype) or any(t in ctype for t in _TEXTY)
    if not is_text or b"\x00" in bytes(buf[:4096]):
        more = "+" if cut else ""
        return (f"[HTTP {status}] binary content ({ctype or 'unknown type'}, {len(buf)}{more} bytes); "
                "not shown. Download it with shell (curl -o) if you need it.")
    try:
        text = bytes(buf).decode(encoding, "replace")
    except LookupError:
        text = bytes(buf).decode("utf-8", "replace")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    tail = " [body cut at 2 MB]" if cut else ""
    return f"[HTTP {status}] {text[:8000]}{tail}"


async def t_spawn(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if ctx.depth >= ctx.core.settings.max_spawn_depth:
        return "ERROR: max sub-agent depth reached; do this part yourself."
    from .ctxspawn import spawn  # ctxspawn: context pack, lineage, agent cap, share_dir
    return await spawn(ctx, args)


def builtin_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool("shell", "run a real terminal command in the environment (install packages, run code, docker, git...). Runs in your own persistent agent dir ($AVRONE_AGENT_DIR) when you have one; the shared workspace is $AVRONE_SHARED_WORKSPACE.", '{"cmd": str}', t_shell))
    r.register(Tool("read_file", "read a file. With your own agent dir, paths are relative to it; prefix `shared/` (e.g. shared/notes.md) to reach the shared team workspace. No absolute paths or '..'.", '{"path": str}', t_read))
    r.register(Tool("write_file", "create or overwrite a file. Relative paths land in your own persistent agent dir (quota applies); prefix `shared/` to write to the shared team workspace.", '{"path": str, "content": str}', t_write))
    r.register(Tool("fetch", "HTTP GET a URL and return its text (first 8000 chars).", '{"url": str}', t_fetch))
    r.register(Tool("spawn", "spawn a sub-agent to handle a piece of your task; returns its result. context: full (default: it sees the run task, lineage, your recent tool calls, memory and file names) | summary | none; share_dir=true lets it read (not write) your agent dir as parent/.", '{"role": str, "task": str, "context"?: str, "focus"?: str, "share_dir"?: bool}', t_spawn))
    return r
