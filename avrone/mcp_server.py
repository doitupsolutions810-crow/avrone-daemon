"""Expose AVRONE itself as an MCP server over stdio.

    python -m avrone.mcp_server

Add to any MCP client (Claude Desktop, Cursor, another AVRONE...)::

    {"mcpServers": {"avrone": {"command": "python", "args": ["-m", "avrone.mcp_server"],
                               "env": {"AVRONE_LLM_BASE_URL": "...", "AVRONE_WORKSPACE": "..."}}}}

Tools: avrone_squad, avrone_chat, avrone_deep, avrone_tools.
stdout carries JSON-RPC only; all logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger("avrone.mcp_server")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
from . import __version__ as _AVRONE_VERSION  # noqa: E402

SERVER_INFO = {"name": "avrone", "title": "AVRONE multi-agent squad", "version": _AVRONE_VERSION}
_CHILD_ENV = "AVRONE_MCP_SERVER_CHILD"  # stops an AVRONE MCP server from loading itself recursively

DEEP_PROMPT = """Deep analysis requested. Work the task below thoroughly: research it, test claims
with tools where possible, look for errors and missing angles, and argue the strongest objections.
{prior}
TASK:
{task}"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "avrone_squad",
        "title": "AVRONE squad (Grok Heavy style)",
        "description": "Run a task with a parallel squad of AVRONE agents (planner splits it into angles, "
                       "agents work them with real tools, a lead cross-checks and merges one answer).",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "What the squad should do or answer."},
            "squad_size": {"type": "integer", "minimum": 1, "maximum": 8,
                           "description": "Number of parallel agents (default from AVRONE_SQUAD_SIZE)."},
            "checkpoint_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$",
                              "description": "Server-managed checkpoint; call again with the same id and "
                                             "task to resume an interrupted run."},
        }, "required": ["task"]},
    },
    {
        "name": "avrone_chat",
        "title": "AVRONE single agent",
        "description": "Run a quick task with one tool-using AVRONE agent.",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "The request."},
        }, "required": ["task"]},
    },
    {
        "name": "avrone_deep",
        "title": "AVRONE deep analysis",
        "description": "Deep, adversarial analysis of a task; optionally critiques and improves a first answer.",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "The question or task."},
            "first_answer": {"type": "string", "description": "Optional draft answer to verify and improve."},
            "rounds": {"type": "integer", "minimum": 1, "maximum": 5,
                       "description": "Max deep waves (1-5, default AVRONE_DEEP_ROUNDS or 1); "
                                      "stops early once a wave finds nothing new."},
            "checkpoint_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$",
                              "description": "Server-managed checkpoint; call again with the same id and "
                                             "task to resume an interrupted run."},
        }, "required": ["task"]},
    },
    {
        "name": "avrone_tools",
        "title": "List AVRONE tools",
        "description": "List the tools AVRONE agents can use (built-ins, plugins, MCP).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


async def build_core():
    """Build a TrinityCore the same way the web server does (plus plugins and MCP tools)."""
    from .codex import Codex
    from .config import settings
    from .llm import LLM
    from .sovereign import establish
    from .trinity import TrinityCore

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    runner = await establish(settings)
    core = TrinityCore(settings, LLM(settings), runner, Codex(settings.data_dir))
    extras: dict[str, Any] = {"runner": runner, "mcp": None}
    try:
        from .plugins import load_plugins
        load_plugins(core.tools, settings)
    except Exception as e:  # never fatal
        log.warning("plugins not loaded: %s", e)
    if not os.getenv(_CHILD_ENV):
        os.environ[_CHILD_ENV] = "1"  # inherited by MCP children, so a nested AVRONE skips this step
        try:
            from .mcp_client import MCPManager
            mgr = MCPManager()
            await mgr.start(core.tools)
            extras["mcp"] = mgr
        except Exception as e:
            log.warning("MCP servers not loaded: %s", e)
    return core, extras


class AvroneMCPServer:
    def __init__(self, core_factory=build_core):
        self._factory = core_factory
        self._core_task: asyncio.Task | None = None
        self._extras: dict[str, Any] = {}
        self._out = None
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._inflight = 0  # agent-running tool calls in progress (avrone_squad/chat/deep)
        self._by_id: dict[Any, asyncio.Task] = {}  # in-flight requests by id, for notifications/cancelled
        self._closed = False  # stdout gone (client exited): stop writing and stop serving

    # ---------------------------------------------------------------- core
    async def core(self):
        if self._core_task is None:
            self._core_task = asyncio.create_task(self._factory())
        core, self._extras = await self._core_task
        return core

    def warm(self) -> None:
        if self._core_task is None:
            self._core_task = asyncio.create_task(self._factory())

    # ---------------------------------------------------------------- io
    async def send(self, obj: dict) -> None:
        """Write one JSON-RPC message. A result JSON cannot carry (NaN, a cycle, an object) used to
        go out as invalid JSON (NaN) or raise mid-handler; it now becomes a -32603 error for that id.
        A closed stdout (the client exited) stops all further writes instead of raising in every task."""
        if self._closed:
            return
        try:
            text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as e:
            text = json.dumps({"jsonrpc": "2.0", "id": _wire_id(obj.get("id")),
                               "error": {"code": -32603,
                                         "message": f"Internal error: result is not valid JSON ({type(e).__name__})"}},
                              separators=(",", ":"))
        # a lone surrogate (from a tool result) cannot be encoded as UTF-8; escape it instead of raising
        data = (text + "\n").encode("utf-8", errors="backslashreplace")
        async with self._write_lock:
            try:
                self._out.write(data)
                self._out.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                self._closed = True

    async def serve(self, reader: asyncio.StreamReader, out) -> None:
        self._out = out
        self.warm()
        while True:
            try:
                raw = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # one oversized request must not kill the server. StreamReader.readline already dropped
                # the oversized bytes (the whole line when its newline had arrived), so just say so
                await self.send({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "Invalid Request: line exceeds the size limit"}})
                continue
            if not raw or self._closed:
                break
            line = raw.decode(errors="replace").strip().lstrip("\ufeff").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):
                # ValueError covers JSONDecodeError and also an integer over Python's 4300-digit limit;
                # RecursionError is very deep nesting. Both used to escape and kill the whole server.
                await self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if msg == []:
                await self.send({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "Invalid Request: empty batch"}})
                continue
            items = msg if isinstance(msg, list) else [msg]
            cap = max_batch()
            if len(items) > cap:
                await self.send({"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600, "message": f"Invalid Request: batch of {len(items)} exceeds {cap} (AVRONE_MCP_MAX_BATCH)"}})
                continue
            for m in items:
                self._dispatch(m)
        if self._tasks:
            await self._drain()
        await self.shutdown()

    def _dispatch(self, m: Any) -> None:
        if isinstance(m, dict) and m.get("method") == "notifications/cancelled":
            self._cancel(m.get("params"))
            return
        if isinstance(m, dict) and isinstance(m.get("method"), str) and m.get("id") is not None and _valid_id(m["id"]):
            key = _id_key(m["id"])
            if key in self._by_id and not self._by_id[key].done():
                t = asyncio.create_task(self.send({"jsonrpc": "2.0", "id": m["id"], "error": {
                    "code": -32600, "message": "Invalid Request: a request with this id is still in progress"}}))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
                return
            t = asyncio.create_task(self._handle_safe(m))
            self._by_id[key] = t
            t.add_done_callback(lambda _t, k=key: self._by_id.pop(k, None) if self._by_id.get(k) is _t else None)
        else:
            t = asyncio.create_task(self._handle_safe(m))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _cancel(self, params: Any) -> None:
        """notifications/cancelled: stop the named in-flight request; per the spec no reply is sent.
        Before this a cancelled squad or deep run kept running and kept its in-flight slot."""
        if not isinstance(params, dict) or not _valid_id(params.get("requestId")) or params.get("requestId") is None:
            return
        t = self._by_id.get(_id_key(params["requestId"]))
        if t is not None and not t.done():
            t._avrone_cancelled = True  # type: ignore[attr-defined]
            t.cancel()

    async def _drain(self) -> None:
        """stdin closed: the client is shutting down (MCP stdio lifecycle). Give in-flight work
        AVRONE_MCP_SHUTDOWN_GRACE seconds, then cancel it and answer each request with an error,
        instead of keeping an hours-long deep run alive for a client that is gone."""
        grace = shutdown_grace()
        pending = {t for t in self._tasks if not t.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=grace)
        if pending:
            ids = [k for k, t in list(self._by_id.items()) if t in pending]
            for t in pending:
                t._avrone_cancelled = True  # type: ignore[attr-defined]
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for k in ids:
                await self.send({"jsonrpc": "2.0", "id": _wire_id(k), "error": {
                    "code": -32603, "message": "server shutting down: input closed before the request finished"}})
        rest = [t for t in self._tasks if not t.done()]
        if rest:
            await asyncio.gather(*rest, return_exceptions=True)

    async def shutdown(self) -> None:
        # The core may still be building at EOF (establish/MCP startup). It used to be left running:
        # it finished after this check, so its runner and MCP children were never stopped and kept the
        # client's stdout pipe open, and the client waited on a server that never exited. Give it the
        # shutdown grace (at least 1 s), then cancel it.
        t = self._core_task
        if t is not None and not t.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(t), timeout=max(1.0, shutdown_grace()))
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        if self._core_task and self._core_task.done() and not self._core_task.cancelled() \
                and not self._core_task.exception():
            res = self._core_task.result()
            if not self._extras and isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], dict):
                self._extras = res[1]  # finished during shutdown: core() never stored its extras
            mgr = self._extras.get("mcp")
            if mgr:
                with contextlib.suppress(Exception):
                    await mgr.stop()
            runner = self._extras.get("runner")
            if runner:
                with contextlib.suppress(Exception):
                    await runner.stop()

    async def _handle_safe(self, m: Any) -> None:
        if not isinstance(m, dict):
            # JSON-RPC invalid requests must receive a response. A non-empty nested
            # batch item (or scalar top-level value) is not a response message.
            await self.send({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": "Invalid Request"}})
            return
        if "method" not in m:
            # We never issue outbound requests, so valid-looking responses are ignored;
            # objects that are neither requests nor responses are invalid requests.
            if "result" in m or "error" in m:
                return
            await self.send({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": "Invalid Request"}})
            return
        rid = m.get("id")
        is_request = "id" in m
        try:
            if is_request and not _valid_id(rid):
                rid = None
                raise _RPCError(-32600, "Invalid Request: id must be a string, an integer or null")
            if "jsonrpc" in m and m["jsonrpc"] != "2.0":
                raise _RPCError(-32600, 'Invalid Request: jsonrpc must be "2.0"')
            if not isinstance(m["method"], str):
                raise _RPCError(-32600, "Invalid Request: method must be a string")
            params = m.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise _RPCError(-32602, "Invalid params: params must be an object")
            result = await self.handle(m["method"], params)
            if is_request:
                await self.send({"jsonrpc": "2.0", "id": rid, "result": result})
        except _RPCError as e:
            if is_request:
                await self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}})
        except Exception as e:
            log.exception("handler failed")
            if is_request:
                await self.send({"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})

    # ---------------------------------------------------------------- methods
    async def handle(self, method: str, params: dict) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
            return {"protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": "AVRONE runs tasks with a parallel squad of tool-using agents. "
                                    "Use avrone_squad for substantive work, avrone_chat for quick asks."}
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            args = params.get("arguments")
            return await self.call_tool(str(params.get("name", "")), {} if args is None else args)
        raise _RPCError(-32601, f"Method not found: {method}")

    async def call_tool(self, name: str, args: dict) -> dict:
        if name not in {t["name"] for t in TOOLS}:
            raise _RPCError(-32602, f"Unknown tool: {name[:100]}")
        if not isinstance(args, dict):
            raise _RPCError(-32602, "Invalid params: arguments must be an object")
        raw_task = args.get("task", "")
        if not isinstance(raw_task, str):
            return _err("'task' must be a string")
        task = raw_task.strip()
        try:
            core = await self.core()
            if name == "avrone_tools":
                return _ok(json.dumps(core.tools.list(), indent=2), {"tools": core.tools.list()})
            if not task:
                return _err("'task' is required")
            from .server import max_message_chars
            limit = max_message_chars()
            first = args.get("first_answer")
            if first is not None and not isinstance(first, str):
                return _err("'first_answer' must be a string")
            if len(task) > limit or (isinstance(first, str) and len(first) > limit):
                return _err(f"task too long (max {limit} characters; AVRONE_MAX_MESSAGE_CHARS)")
            cap = max_inflight()
            if self._inflight >= cap:
                return _err(f"busy: {cap} AVRONE run(s) already in progress (AVRONE_MCP_MAX_INFLIGHT); "
                            "retry when one finishes")
            self._inflight += 1
            try:
                ck, release = _checkpoint(core, name, task, args.get("checkpoint_id"))
                try:
                    res = await self._run(core, name, task, args, ck)
                finally:
                    if release:
                        release()
            finally:
                self._inflight -= 1
        except Exception as e:
            log.exception("tool %s failed", name)
            return _err(f"{type(e).__name__}: {e}")
        if isinstance(res, dict):
            answer = str(res.get("answer", ""))
            meta = {"run_id": res.get("run_id"), "agents": len(res.get("agents") or [])}
            if ck:
                meta["checkpoint_id"] = str(args.get("checkpoint_id"))
            return _ok(answer, meta)
        return _ok(str(res))

    async def _run(self, core: Any, name: str, task: str, args: dict, ck: dict) -> Any:
        if name == "avrone_squad":
            res = await core.squad(task, _clamp_size(args.get("squad_size")), **ck)
        elif name == "avrone_chat":
            res = await core.chat(task)
        else:  # avrone_deep
            first = args.get("first_answer")
            if hasattr(core, "deep"):
                rounds = _clamp_rounds(args.get("rounds"))
                kw = {"rounds": rounds} if rounds is not None else {}
                res = await core.deep(task, first, **kw, **ck)
            else:
                prior = (f"A first answer was given; verify it, fix its mistakes and improve it:\n{first}\n"
                         if first else "")
                res = await core.squad(DEEP_PROMPT.format(prior=prior, task=task))
        return res


def _checkpoint(core: Any, name: str, task: str, ck_id: Any) -> tuple[dict, Any]:
    """({"checkpoint": path} or {}, release or None). Raises CheckpointError/CheckpointConflict."""
    if ck_id in (None, "") or name not in ("avrone_squad", "avrone_deep"):
        return {}, None
    from .checkpoint import check_compatible, claim, managed_path, sanitize_client_id
    path = managed_path(core.settings.data_dir, sanitize_client_id(ck_id))
    check_compatible(path, task, "squad" if name == "avrone_squad" else "deep")
    return {"checkpoint": path}, claim(path)


DEFAULT_MCP_INFLIGHT = 4
HARD_MCP_INFLIGHT = 64


def max_inflight() -> int:
    """Concurrent agent-running tool calls one MCP client may hold (AVRONE_MCP_MAX_INFLIGHT, 1..64)."""
    try:
        n = int(str(os.environ.get("AVRONE_MCP_MAX_INFLIGHT", "")).strip() or DEFAULT_MCP_INFLIGHT)
    except (TypeError, ValueError):
        n = DEFAULT_MCP_INFLIGHT
    return max(1, min(HARD_MCP_INFLIGHT, n))


DEFAULT_MCP_BATCH = 64
DEFAULT_SHUTDOWN_GRACE = 30.0


def max_batch() -> int:
    """Largest JSON-RPC batch accepted on one line (AVRONE_MCP_MAX_BATCH, 1..1024, default 64).
    A 100k-element batch used to spawn 100k tasks at once."""
    try:
        n = int(str(os.environ.get("AVRONE_MCP_MAX_BATCH", "")).strip() or DEFAULT_MCP_BATCH)
    except (TypeError, ValueError):
        n = DEFAULT_MCP_BATCH
    return max(1, min(1024, n))


def shutdown_grace() -> float:
    """Seconds in-flight work may finish after stdin closes (AVRONE_MCP_SHUTDOWN_GRACE, 0..3600, default 30)."""
    try:
        v = float(str(os.environ.get("AVRONE_MCP_SHUTDOWN_GRACE", "")).strip() or DEFAULT_SHUTDOWN_GRACE)
    except (TypeError, ValueError):
        v = DEFAULT_SHUTDOWN_GRACE
    if v != v:
        v = DEFAULT_SHUTDOWN_GRACE
    return max(0.0, min(3600.0, v))


def _valid_id(rid: Any) -> bool:
    """JSON-RPC ids are a string, an integer or null (bools, floats, objects and arrays are refused)."""
    return rid is None or isinstance(rid, str) or (isinstance(rid, int) and not isinstance(rid, bool))


def _id_key(rid: Any) -> tuple[str, Any]:
    # 1 and "1" are different ids
    return ("s", rid) if isinstance(rid, str) else ("n", rid)


def _wire_id(rid: Any) -> Any:
    if isinstance(rid, tuple) and len(rid) == 2 and rid[0] in ("s", "n"):
        return rid[1]
    return rid if _valid_id(rid) else None


def _clamp_size(size: Any) -> int | None:
    """Honour the advertised schema (1..8); junk or missing means 'use the default'."""
    if size is None or isinstance(size, bool):
        return None
    try:
        n = int(size)
    except (TypeError, ValueError, OverflowError):  # 1e999 parses as inf: int(inf) is OverflowError
        return None
    return max(1, min(8, n))


def _clamp_rounds(rounds: Any) -> int | None:
    """Honour the advertised schema (1..5); junk or missing means 'use the default'."""
    from .trinity import clamp_deep_rounds
    return clamp_deep_rounds(rounds)


class _RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _ok(text: str, structured: dict | None = None) -> dict:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


async def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


def protocol_stdout():
    """A private, non-inheritable copy of fd 1 for JSON-RPC, with fd 1 itself pointed at stderr.
    Before, only sys.stdout was swapped: every child (runner shell, MCP servers, tools) inherited
    the real stdout, so one stray print corrupted the protocol, and a child left running held the
    client's pipe open after the server had exited. Falls back to sys.stdout.buffer when fds can't
    be duplicated (an odd embedder)."""
    try:
        sys.stdout.flush()
        fd = os.dup(sys.stdout.fileno())  # os.dup() is non-inheritable since Python 3.4
        os.dup2(sys.stderr.fileno(), 1)
        return os.fdopen(fd, "wb")
    except (OSError, ValueError, AttributeError):
        return sys.stdout.buffer


async def amain() -> None:
    out = protocol_stdout()
    # Anything else that prints must not corrupt the JSON-RPC stream.
    sys.stdout = sys.stderr
    server = AvroneMCPServer()
    await server.serve(await _stdin_reader(), out)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=os.getenv("AVRONE_LOG_LEVEL", "WARNING").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()
