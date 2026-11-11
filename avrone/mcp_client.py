"""Dependency-free MCP (Model Context Protocol) client.

Speaks JSON-RPC 2.0 over two transports:
  * stdio            - spawn a process, newline-delimited JSON on stdin/stdout
  * Streamable HTTP  - POST JSON-RPC to a URL; the reply may be application/json
                       or text/event-stream; the Mcp-Session-Id header is kept.

Servers come from an ``mcp.json`` in Claude Desktop format::

    {"mcpServers": {
        "fs":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."], "env": {}},
        "web":  {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
    }}

Path: env AVRONE_MCP_CONFIG (default ./mcp.json).

Every remote tool is registered into AVRONE's ToolRegistry as
``mcp__<server>__<tool>`` with ``source="mcp:<server>"``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import logging
import os
import math
import re
import shlex
from pathlib import Path
from typing import Any

import httpx

from .tools import Tool, ToolRegistry

log = logging.getLogger("avrone.mcp")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "avrone", "version": "0.2.0"}
DEFAULT_TIMEOUT = float(os.getenv("AVRONE_MCP_TIMEOUT", "60"))


class MCPError(RuntimeError):
    """A JSON-RPC error or transport failure talking to an MCP server."""

    def __init__(self, message: str, code: int | None = None, data: Any = None,
                 session_expired: bool = False):
        super().__init__(message)
        self.code = code
        self.data = data
        self.session_expired = session_expired


def _safe_part(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", str(s))[:64] or "_"


# tools taken from one server: a hostile or buggy server paging 100 x 32 MB of tool entries used to
# put millions of tools into the registry (and into every agent's tool docs)
MAX_TOOLS_PER_SERVER = 512
STDERR_LINE_CHARS = 2000  # one kept stderr line (the last one is quoted when the server dies)
SEND_TIMEOUT_S = 30.0  # notifications and server-request replies: a request uses its own timeout

DEFAULT_MAX_RESPONSE, MIN_MAX_RESPONSE, HARD_MAX_RESPONSE = 32 * 1024 * 1024, 64 * 1024, 1024 * 1024 * 1024


def max_response_bytes() -> int:
    """Largest MCP reply accepted, in bytes (AVRONE_MCP_MAX_RESPONSE_BYTES): one HTTP body, one SSE
    event, or one stdio line. A server streaming without end used to grow memory without limit."""
    try:
        n = int(os.environ.get("AVRONE_MCP_MAX_RESPONSE_BYTES", "").strip() or DEFAULT_MAX_RESPONSE)
    except ValueError:
        n = DEFAULT_MAX_RESPONSE
    return max(MIN_MAX_RESPONSE, min(HARD_MAX_RESPONSE, n))


def clamp_timeout(v: Any, default: float = 60.0) -> float:
    """A per-server timeout from mcp.json: a number of seconds in 1..3600. "abc", NaN, 0 and negatives
    fall back to the default instead of raising or waiting forever."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f) or f <= 0:
        return default
    return max(1.0, min(3600.0, f))


def wire_safe(v: Any) -> Any:
    """Tool arguments as strict, UTF-8-encodable JSON. Python's json.loads accepts NaN/Infinity and
    lone surrogates, so a model's tool call can carry them: over HTTP httpx raised a raw ValueError
    ("Out of range float values") or UnicodeEncodeError, and over stdio a bare NaN token reached the
    server (invalid JSON-RPC: many servers drop or crash on it). NaN/inf -> null, surrogates ->
    U+FFFD, tuple/set -> list (via codex.jsonable, depth-capped)."""
    from .codex import jsonable

    def fix(x: Any) -> Any:
        if isinstance(x, str):
            return _SURR_RE.sub("\ufffd", x)
        if isinstance(x, dict):
            return {fix(k): fix(y) for k, y in x.items()}
        if isinstance(x, list):
            return [fix(y) for y in x]
        return x
    return fix(jsonable(v))


_SURR_RE = re.compile(r"[\ud800-\udfff]")


def coerce_args(v: Any) -> list[str]:
    """mcp.json "args". A string used to be split into single characters ("-y srv" ran
    `cmd - y   s r v`); a string is now split like a shell would, a scalar becomes one arg."""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            return shlex.split(v)
        except ValueError:
            return v.split()
    if isinstance(v, (list, tuple)):
        return [str(a) for a in v if a is not None]
    return [str(v)]


def coerce_str_map(v: Any, what: str) -> dict[str, str]:
    """mcp.json "headers"/"env": a JSON object of strings. Anything else raises a clear MCPError."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise MCPError(f"'{what}' must be an object of strings, not {type(v).__name__}")
    return {str(k): ("" if x is None else str(x)) for k, x in v.items()}


_SESSION_ID_RE = re.compile(r"[\x21-\x7e]{1,1024}")


def _valid_session_id(sid: str) -> bool:
    """MCP session ids are visible ASCII (spec). A CR/LF or non-ASCII id would be sent back as a
    header: httpx raises on it (every later call failed) and it is a header-injection vector."""
    return bool(_SESSION_ID_RE.fullmatch(sid))


def _hashable_id(v: Any) -> bool:
    return isinstance(v, (int, str)) and not isinstance(v, bool)


def _client_reply(m: dict) -> dict:
    """The answer to a server->client request: pong for ping, method-not-found for the rest."""
    if m.get("method") == "ping":
        return {"jsonrpc": "2.0", "id": m["id"], "result": {}}
    return {"jsonrpc": "2.0", "id": m["id"],
            "error": {"code": -32601, "message": "Method not supported by client"}}


# --------------------------------------------------------------------------- transports
class _Transport:
    async def connect(self) -> None: ...
    async def request(self, method: str, params: dict | None, timeout: float) -> Any: ...
    async def notify(self, method: str, params: dict | None = None) -> None: ...
    async def close(self) -> None: ...


class StdioTransport(_Transport):
    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 cwd: str | None = None):
        self.command = command
        self.args = coerce_args(args)
        self.env = coerce_str_map(env, "env")
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self.stderr_tail: list[str] = []
        self._limit = max_response_bytes()

    async def connect(self) -> None:
        env = {**os.environ, **self.env}
        self._limit = max_response_bytes()
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=self.cwd, limit=self._limit)
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Keep reading stderr in chunks, whatever its shape. Line iteration stopped for good at the
        first line over the stream limit (ValueError, swallowed): the pipe then filled and the server
        blocked on its next stderr write. Whole 12 MB lines were also kept and quoted into errors."""
        assert self.proc and self.proc.stderr
        partial = bytearray()

        def keep(raw: bytes) -> None:
            line = raw[:STDERR_LINE_CHARS * 4].decode(errors="replace").rstrip()[:STDERR_LINE_CHARS]
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]
                log.debug("mcp stderr [%s]: %s", self.command, line)
        with contextlib.suppress(Exception):
            while True:
                chunk = await self.proc.stderr.read(65536)
                if not chunk:
                    break
                parts = chunk.split(b"\n")
                for i, piece in enumerate(parts):
                    if len(partial) < STDERR_LINE_CHARS * 4:
                        partial += piece[:STDERR_LINE_CHARS * 4 - len(partial)]
                    if i < len(parts) - 1:  # a newline ended this piece
                        keep(bytes(partial)); partial.clear()
            keep(bytes(partial))

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self._lines():
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, RecursionError):  # junk, deep nesting, a 5000-digit number
                    log.debug("mcp non-JSON stdout line ignored: %s", line[:200])
                    continue
                for m in (msg if isinstance(msg, list) else [msg]):
                    try:
                        await self._dispatch(m)
                    except MCPError as e:  # replying to a server request after it exited
                        log.debug("mcp reply failed: %s", e)
        except Exception as e:  # pragma: no cover
            log.debug("mcp stdio reader stopped: %s", e)
        finally:
            self._fail_pending(MCPError("MCP server process exited" + (
                f": {self.stderr_tail[-1][:300]}" if self.stderr_tail else "")))

    async def _lines(self):
        """stdout lines, each at most self._limit bytes. A longer line used to kill the reader (the
        StreamReader raised ValueError): every pending request then failed as "process exited" and
        later ones hung until their timeout. Now the long line is discarded up to its newline, the
        requests waiting at that moment fail with the reason (the reply can't be matched to an id),
        and the connection keeps going."""
        assert self.proc and self.proc.stdout
        buf = bytearray()
        discarding = False
        while True:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                if buf and not discarding:
                    yield bytes(buf)
                return
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > self._limit:
                        if not discarding:
                            discarding = True
                            self._fail_pending(MCPError(
                                f"MCP server sent a reply line over {self._limit} bytes; it was dropped"))
                        buf.clear()
                    break
                line, buf = bytes(buf[:nl]), buf[nl + 1:]
                if discarding:
                    discarding = False  # this was the tail of the long line
                    continue
                if len(line) > self._limit:
                    self._fail_pending(MCPError(
                        f"MCP server sent a reply line over {self._limit} bytes; it was dropped"))
                    continue
                yield line

    def _fail_pending(self, err: MCPError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def _dispatch(self, m: Any) -> None:
        if not isinstance(m, dict) or "id" not in m:
            return  # notifications (logging, progress, list_changed) are ignored
        if not _hashable_id(m["id"]):
            return  # a list/dict/bool id used to raise TypeError here and kill the reader
        if "result" in m or "error" in m:
            fut = self._pending.pop(m["id"], None)
            if fut and not fut.done():
                fut.set_result(m)
        elif isinstance(m.get("method"), str):
            # Server->client request (ping, sampling, roots...). Answer ping, decline the rest.
            await self._send(_client_reply(m))

    async def _send(self, obj: dict, timeout: float | None = None) -> None:
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise MCPError("MCP server process is not running")
        if self._reader is not None and self._reader.done():
            # its stdout closed: no reply can ever be read, so don't wait out the timeout for one
            raise MCPError("MCP server closed its output; the connection is gone")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        timeout = SEND_TIMEOUT_S if timeout is None else timeout
        writing = False
        try:
            async with asyncio.timeout(timeout):
                async with self._write_lock:
                    writing = True
                    self.proc.stdin.write(data)
                    await self.proc.stdin.drain()
        except TimeoutError:
            if not writing:  # queued behind another write: nothing of ours went out
                raise MCPError(f"MCP server input busy for {timeout:g}s; nothing was sent")
            # a server that stopped reading stdin: drain() blocked forever, past the request timeout
            # and holding the write lock, so every later call on this server hung too. A half-written
            # line can't be taken back, so the connection is dropped.
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            raise MCPError(f"MCP server stopped reading its input ({timeout:g}s); connection dropped")
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as e:
            # the server died between the returncode check and the write: this used to escape
            # as a raw BrokenPipeError past the tool wrapper (which only catches MCPError)
            raise MCPError(f"MCP server process is not running ({type(e).__name__})")

    async def request(self, method: str, params: dict | None, timeout: float) -> Any:
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            t0 = time.monotonic()
            await self._send(msg, timeout)
            resp = await asyncio.wait_for(fut, max(0.01, timeout - (time.monotonic() - t0)))
        except asyncio.TimeoutError:
            raise MCPError(f"timeout waiting for {method}")
        finally:
            self._pending.pop(rid, None)
        return _unwrap(resp)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def close(self) -> None:
        p = self.proc
        if p and p.returncode is None:
            with contextlib.suppress(Exception):
                p.stdin.close()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(p.wait(), 2)
            except (asyncio.TimeoutError, Exception):
                with contextlib.suppress(ProcessLookupError):
                    p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 2)
                except (asyncio.TimeoutError, Exception):
                    with contextlib.suppress(ProcessLookupError):
                        p.kill()
        for t in (self._reader, self._stderr_task):
            if t and not t.done():
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t


class HTTPTransport(_Transport):
    """MCP Streamable HTTP transport."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = coerce_str_map(headers, "headers")
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._id = 0
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=15),
                                         follow_redirects=True,
                                         event_hooks={"request": [self._strip_cross_origin]})

    def _origin(self, url: Any) -> tuple[str, str, int | None]:
        u = httpx.URL(str(url))
        return u.scheme, u.host, u.port or {"http": 80, "https": 443}.get(u.scheme)

    async def _strip_cross_origin(self, request: httpx.Request) -> None:
        """A redirect to another origin got every configured header: httpx drops Authorization on
        such hops, but an X-Api-Key (and the session id) went to whatever host the server named."""
        if self._origin(request.url) == self._origin(self.url):
            return
        drop = {k.lower() for k in self.headers} | {"mcp-session-id", "authorization", "cookie",
                                                    "proxy-authorization"}
        for k in list(request.headers.keys()):
            if k.lower() in drop:
                del request.headers[k]

    def _hdrs(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def _post(self, body: dict, timeout: float, want_id: int | None) -> Any:
        assert self._client is not None, "connect() first"
        async with self._client.stream("POST", self.url, json=body, headers=self._hdrs(),
                                       timeout=timeout) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                if _valid_session_id(sid):
                    self.session_id = sid
                else:
                    log.warning("MCP server sent an invalid Mcp-Session-Id (%d chars); ignored", len(sid))
            if r.status_code == 404 and self.session_id and body.get("method") != "initialize":
                raise MCPError("MCP session expired (HTTP 404)", code=404, session_expired=True)
            if r.status_code >= 400:
                text = (await self._read_capped(r, 4096)).decode(errors="replace")
                raise MCPError(f"HTTP {r.status_code}: {text[:300]}", code=r.status_code)
            if want_id is None:  # notification: 202 Accepted, nothing to read
                return None
            cap = max_response_bytes()
            ctype = r.headers.get("content-type", "").lower()
            if "text/event-stream" in ctype:
                data_lines: list[str] = []
                size = 0
                async for line in _capped_lines(r, cap):
                    if line.startswith(":"):
                        continue  # comment / keep-alive
                    field, colon, value = line.partition(":")
                    if field == "data" and (colon or line == "data"):
                        if value.startswith(" "):
                            value = value[1:]  # SSE strips exactly one space
                        size += len(value) + 1
                        if size > cap:
                            raise MCPError(f"MCP event larger than {cap} bytes "
                                           "(AVRONE_MCP_MAX_RESPONSE_BYTES)")
                        data_lines.append(value)
                    elif line == "":
                        if data_lines:
                            payload, data_lines, size = "\n".join(data_lines), [], 0
                            found = await self._handle_event(payload, want_id)
                            if found is not None:
                                return found
                if data_lines:
                    found = await self._handle_event("\n".join(data_lines), want_id)
                    if found is not None:
                        return found
                raise MCPError("event stream ended without a response")
            raw = await self._read_capped(r, cap, strict=True)
            if not raw.strip():
                raise MCPError("empty response body")
            found = _match_response(raw.decode(errors="replace"), want_id)
            if found is None:
                raise MCPError("no matching JSON-RPC response in body")
            return found

    @staticmethod
    async def _read_capped(r: httpx.Response, cap: int, strict: bool = False) -> bytes:
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf += chunk
            if len(buf) > cap:
                if strict:
                    raise MCPError(f"MCP response larger than {cap} bytes (AVRONE_MCP_MAX_RESPONSE_BYTES)")
                return bytes(buf[:cap])
        return bytes(buf)

    async def _handle_event(self, payload: str, want_id: int) -> dict | None:
        """One SSE event: our response, or a server->client request to answer (the server may block
        on it, e.g. a ping, until it gets one), or noise."""
        found = _match_response(payload, want_id)
        if found is not None:
            return found
        try:
            obj = json.loads(payload)
        except (ValueError, RecursionError):
            return None
        for m in (obj if isinstance(obj, list) else [obj]):
            if (isinstance(m, dict) and isinstance(m.get("method"), str) and "id" in m
                    and _hashable_id(m["id"]) and "result" not in m and "error" not in m):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._post(_client_reply(m), 10, None), 10)
        return None

    async def request(self, method: str, params: dict | None, timeout: float) -> Any:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            # httpx's timeout is per read: an SSE stream sending keep-alives (or a trickle of
            # progress events) every few seconds used to hold the call open forever
            resp = await asyncio.wait_for(self._post(msg, timeout, self._id), timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"timeout waiting for {method} ({timeout:g}s)")
        except httpx.HTTPError as e:
            raise MCPError(f"{type(e).__name__}: {e}")
        return _unwrap(resp)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._post(msg, DEFAULT_TIMEOUT, None)
        except (httpx.HTTPError, MCPError) as e:
            log.debug("mcp notify %s failed: %s", method, e)

    async def close(self) -> None:
        if self._client is None:
            return
        if self.session_id:  # politely end the session; servers may answer 405
            with contextlib.suppress(Exception):
                await self._client.delete(self.url, headers=self._hdrs(), timeout=5)
        await self._client.aclose()
        self._client = None


async def _capped_lines(r: httpx.Response, cap: int):
    """SSE lines (LF, CRLF or CR endings), none longer than `cap` bytes. httpx's aiter_lines buffers
    until a newline, and only `data:` bytes were counted, so a server streaming 1 GB with no newline
    (or one huge `event:` or comment line) was held whole in memory."""
    buf = bytearray()
    scanned = 0
    async for chunk in r.aiter_bytes():
        buf += chunk
        while True:
            lf, cr = buf.find(b"\n", scanned), buf.find(b"\r", scanned)
            ends = [i for i in (lf, cr) if i >= 0]
            if not ends:
                scanned = len(buf)
                if len(buf) > cap:
                    raise MCPError(f"MCP event line larger than {cap} bytes (AVRONE_MCP_MAX_RESPONSE_BYTES)")
                break
            cut = min(ends)
            if buf[cut] == 13 and cut + 1 == len(buf):
                scanned = cut  # a CR at the chunk edge: wait for a possible LF
                break
            step = 2 if buf[cut] == 13 and buf[cut + 1:cut + 2] == b"\n" else 1
            if cut > cap:
                raise MCPError(f"MCP event line larger than {cap} bytes (AVRONE_MCP_MAX_RESPONSE_BYTES)")
            line = bytes(buf[:cut])
            del buf[:cut + step]
            scanned = 0
            yield line.decode("utf-8", errors="replace")
    if buf:
        yield bytes(buf).rstrip(b"\r").decode("utf-8", errors="replace")


def _match_response(payload: str, want_id: int) -> dict | None:
    try:
        obj = json.loads(payload)
    except (ValueError, RecursionError):  # junk, very deep nesting, an over-long integer
        return None
    for m in (obj if isinstance(obj, list) else [obj]):
        if isinstance(m, dict) and m.get("id") == want_id and ("result" in m or "error" in m):
            return m
    return None


def _unwrap(resp: dict) -> Any:
    if "error" in resp and resp["error"] is not None:
        err = resp["error"] if isinstance(resp["error"], dict) else {"message": str(resp["error"])}
        code = err.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            code = None
        raise MCPError(str(err.get("message", "error"))[:2000], code, err.get("data"))
    return resp.get("result")


def _obj(v: Any) -> dict[str, Any]:
    """A result that should be a JSON object; a list/string/number from a broken server reads as {}."""
    return v if isinstance(v, dict) else {}


# --------------------------------------------------------------------------- client
class MCPClient:
    """One connection to one MCP server."""

    def __init__(self, name: str, config: dict[str, Any], timeout: float = DEFAULT_TIMEOUT):
        self.name = name
        self.config = config
        self.timeout = clamp_timeout(config.get("timeout", timeout), clamp_timeout(timeout))
        if config.get("url"):
            self.transport: _Transport = HTTPTransport(str(config["url"]), config.get("headers"))
            self.kind = "http"
        elif config.get("command"):
            self.transport = StdioTransport(str(config["command"]), config.get("args"),
                                            config.get("env"), config.get("cwd"))
            self.kind = "stdio"
        else:
            raise MCPError(f"server '{name}' needs either 'command' or 'url'")
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.tools: list[dict[str, Any]] = []

    async def connect(self) -> None:
        await self.transport.connect()
        await self.initialize()

    async def initialize(self) -> dict[str, Any]:
        res = _obj(await self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.timeout))
        version = res.get("protocolVersion")
        if not isinstance(version, str) or not re.fullmatch(r"[\x21-\x7e]{1,64}", version):
            version = PROTOCOL_VERSION  # it goes back out as a header: never a list, number or CR/LF
        if version not in SUPPORTED_VERSIONS:
            log.warning("MCP server %s negotiated unknown protocol %s; continuing", self.name, version)
        self.protocol_version = version
        if isinstance(self.transport, HTTPTransport):
            self.transport.protocol_version = version
        self.server_info = _obj(res.get("serverInfo"))
        self.capabilities = _obj(res.get("capabilities"))
        await self.transport.notify("notifications/initialized")
        return res

    async def _request(self, method: str, params: dict | None, timeout: float) -> Any:
        """A request that survives one HTTP session expiry: the spec says a 404 on a session means
        start a new one. Before, every tool on that server failed until AVRONE restarted."""
        try:
            return await self.transport.request(method, params, timeout=timeout)
        except MCPError as e:
            if not (e.session_expired and isinstance(self.transport, HTTPTransport)):
                raise
        log.info("MCP server %s: session expired, starting a new one", self.name)
        self.transport.session_id = None
        await self.initialize()
        return await self.transport.request(method, params, timeout=timeout)

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        names: set[str] = set()
        for _ in range(100):  # pagination guard
            params = {"cursor": cursor} if cursor else {}
            res = _obj(await self._request("tools/list", params, timeout=self.timeout))
            page = res.get("tools")
            if isinstance(page, list):
                # a non-string name (42, ["x"]) used to raise in registration and abort the whole
                # manager start, taking every other server's tools down with it
                for t in page:
                    if (isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip()
                            and t["name"] not in names):  # a page repeated by the server lists once
                        names.add(t["name"])
                        tools.append(t)
                        if len(tools) >= MAX_TOOLS_PER_SERVER:
                            break
            if len(tools) >= MAX_TOOLS_PER_SERVER:
                log.warning("MCP server %s lists more than %d tools; the rest are ignored",
                            self.name, MAX_TOOLS_PER_SERVER)
                break
            cursor = res.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                break  # a server repeating its cursor used to be asked 100 times for the same page
            seen_cursors.add(cursor)
        self.tools = tools
        return tools

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        res = await self._request("tools/call", {"name": tool, "arguments": wire_safe(arguments or {})},
                                  timeout=self.timeout)
        if not isinstance(res, dict):
            return "(no content)" if res is None else f"ERROR (from MCP tool {self.name}/{tool}): " \
                f"malformed result {json.dumps(res, default=str)[:300]}"
        text = format_content(res)
        if res.get("isError"):
            return f"ERROR (from MCP tool {self.name}/{tool}): {text}"
        return text

    async def ping(self) -> bool:
        await self.transport.request("ping", None, timeout=10)
        return True

    async def close(self) -> None:
        await self.transport.close()


def format_content(result: dict[str, Any]) -> str:
    """Flatten an MCP CallToolResult into text for an agent."""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, (str, dict)):
        content = [content if isinstance(content, dict) else {"type": "text", "text": content}]
    elif not isinstance(content, list):
        content = []  # a bare string used to come out one character per line
    for c in content:
        if not isinstance(c, dict):
            parts.append(str(c))
            continue
        kind = c.get("type")
        if kind == "text":
            parts.append(str(c.get("text", "")))
        elif kind == "image":
            parts.append(f"[image: {c.get('mimeType', 'unknown')}, {len(str(c.get('data', '')))} b64 chars omitted]")
        elif kind == "audio":
            parts.append(f"[audio: {c.get('mimeType', 'unknown')} omitted]")
        elif kind == "resource":
            r = _obj(c.get("resource"))
            if "text" in r:
                parts.append(f"[resource {r.get('uri', '')}]\n{r['text']}")
            else:
                parts.append(f"[resource: {r.get('uri', '')} ({r.get('mimeType', 'binary')}) omitted]")
        elif kind == "resource_link":
            parts.append(f"[resource link: {c.get('uri', '')} {c.get('name', '')}]".strip())
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts) if parts else "(no content)"


_TYPE_MAP = {"string": "str", "integer": "int", "number": "float", "boolean": "bool",
             "array": "list", "object": "dict", "null": "null"}


def args_doc_from_schema(schema: dict[str, Any] | None, limit: int = 300) -> str:
    """Compact '{"path": str, "depth"?: int}' style doc from a JSON Schema."""
    if not isinstance(schema, dict):
        return "{}"
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    req = schema.get("required")
    # a bare string ("path") used to become set("path") = {"p","a","t","h"}
    required = {str(x) for x in req if isinstance(x, (str, int))} if isinstance(req, list) else (
        {req} if isinstance(req, str) else set())
    items = []
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        t = spec.get("type")
        if isinstance(t, list):
            t = "|".join(_TYPE_MAP.get(x, str(x)) if isinstance(x, str) else "any"
                         for x in t[:8] if x != "null") or "any"
        elif isinstance(spec.get("enum"), list) and spec["enum"]:
            t = "|".join(json.dumps(v, default=str) for v in spec["enum"][:6])
        else:  # a dict/list "type" (unhashable) used to raise and drop the whole tool
            t = _TYPE_MAP.get(t, "any") if isinstance(t, str) and t else "any"
        items.append(f'"{json.dumps(str(key), ensure_ascii=False)[1:-1][:80]}"{"" if key in required else "?"}: {t}')
        if sum(len(i) + 2 for i in items) > limit:
            break  # 100k properties used to be rendered in full before the doc was cut to `limit`
    doc = "{" + ", ".join(items) + "}"
    if len(doc) > limit:
        doc = doc[: limit - 5].rsplit(",", 1)[0] + ", ...}"
    return doc


def load_config(path: str | os.PathLike | None = None) -> dict[str, dict[str, Any]]:
    """Read the mcpServers map. Missing file -> {}."""
    p = Path(path or os.getenv("AVRONE_MCP_CONFIG", "./mcp.json"))
    if not p.exists():
        return {}
    if not p.is_file():  # a FIFO here blocked server startup forever in read_text()
        raise ValueError(f"MCP config {str(p)!r} is not a regular file")
    from .safeio import NotRegularFile, open_read
    try:  # round 106: a FIFO swapped in after is_file() blocked startup (O_NONBLOCK + fstat now)
        f = open_read(p, follow=True)
    except NotRegularFile as e:
        raise ValueError(f"MCP config {str(p)!r} is not a regular file") from e
    with f:
        raw = f.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"MCP config is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except RecursionError as e:
        raise ValueError("MCP config is nested too deeply") from e
    servers = data.get("mcpServers", data.get("servers", {})) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        raise ValueError(f"mcpServers must be an object of servers, not {type(servers).__name__}")
    out = {str(k): v for k, v in servers.items()
           if isinstance(v, dict) and not v.get("disabled")}
    if len(out) > MAX_SERVERS:  # 10k entries used to spawn 10k server processes at once
        raise ValueError(f"MCP config lists {len(out)} servers; at most {MAX_SERVERS} are allowed")
    return out


MAX_CONFIG_BYTES = 1024 * 1024
MAX_SERVERS = 64


# --------------------------------------------------------------------------- manager
class MCPManager:
    """Connects every server in mcp.json and exposes their tools to AVRONE agents."""

    def __init__(self, config_path: str | os.PathLike | None = None,
                 servers: dict[str, dict[str, Any]] | None = None):
        self.config_path = config_path
        self._servers_override = servers
        self.clients: dict[str, MCPClient] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._registered: list[str] = []
        self.registry: ToolRegistry | None = None

    async def start(self, registry: ToolRegistry) -> dict[str, dict[str, Any]]:
        self.registry = registry
        try:
            servers = self._servers_override if self._servers_override is not None \
                else load_config(self.config_path)
        except Exception as e:
            log.error("could not read MCP config: %s", e)
            self._status["_config"] = {"connected": False, "error": f"config: {e}", "tools": 0}
            return self.status()
        await asyncio.gather(*(self._start_one(n, c, registry) for n, c in servers.items()))
        return self.status()

    async def _start_one(self, name: str, cfg: dict[str, Any], registry: ToolRegistry) -> None:
        client: MCPClient | None = None
        try:
            client = MCPClient(name, cfg)
            await asyncio.wait_for(client.connect(), client.timeout)
            tools = await client.list_tools()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}" or type(e).__name__
            log.warning("MCP server '%s' failed to start: %s", name, msg)
            self._status[name] = {"connected": False, "error": msg, "tools": 0,
                                  "transport": client.kind if client else None}
            if client:
                with contextlib.suppress(Exception):
                    await client.close()
            return
        self.clients[name] = client
        count = 0
        mine = set(self._registered)
        for t in tools:
            base = f"mcp__{_safe_part(name)}__{_safe_part(t['name'])}"
            tool_name, n = base, 2
            # "a b" and "a_b" (or two servers that sanitize alike) used to map to one name, and
            # register() silently replaced the first tool with the second
            while tool_name in mine or registry.get(tool_name) is not None:
                tool_name, n = f"{base}_{n}", n + 1
            try:
                registry.register(Tool(
                    name=tool_name,
                    description=str(t.get("description") or t.get("title") or f"MCP tool {t['name']} on {name}").strip().replace("\n", " ")[:500],  # docs() one_line()s it
                    args_doc=args_doc_from_schema(t.get("inputSchema")),
                    fn=self._make_fn(client, t["name"]),
                    source=f"mcp:{name}",
                ))
                self._registered.append(tool_name)
                mine.add(tool_name)
                count += 1
            except ValueError as e:
                log.warning("skipping MCP tool %s/%s: %s", name, t.get("name"), e)
        self._status[name] = {"connected": True, "error": None, "tools": count, "transport": client.kind,
                              "server": client.server_info, "protocol": client.protocol_version}
        log.info("MCP server '%s' connected with %d tools", name, count)

    @staticmethod
    def _make_fn(client: MCPClient, remote: str):
        async def fn(ctx: Any, args: dict[str, Any]) -> str:
            try:
                return await client.call_tool(remote, args if isinstance(args, dict) else {})
            except MCPError as e:
                return f"ERROR: MCP {client.name}/{remote}: {e}"
        return fn

    async def stop(self) -> None:
        if self.registry is not None:
            for n in self._registered:
                self.registry.unregister(n)
        self._registered.clear()
        await asyncio.gather(*(c.close() for c in self.clients.values()), return_exceptions=True)
        for name in self.clients:
            if name in self._status:
                self._status[name]["connected"] = False
        self.clients.clear()

    def status(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._status.items()}
