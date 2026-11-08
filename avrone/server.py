"""Ghost Shell: the surface users and admins touch (web UI, REST, WebSocket HUD)."""
from __future__ import annotations

from typing import Any

import hmac

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import anyio
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .bootstrap import build_core, run_cleanups
from .config import settings
from .trinity import TrinityCore, clamp_deep_rounds

log = logging.getLogger("avrone.server")

STATIC = Path(__file__).parent / "static"
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # build_core establishes the environment FIRST (so agents never hit a missing
    # sandbox mid-task), then wires tools, plugins and MCP servers.
    core_, cleanups = await build_core(settings)
    state["core"] = core_
    state["tasks"] = set()
    try:
        yield
    finally:
        for t in list(state["tasks"]):
            t.cancel()
        await run_cleanups(cleanups)


app = FastAPI(title="AVRONE", version=__version__, lifespan=lifespan)
from .evidence_receiver import ingest_evidence  # noqa: E402

app.add_api_route("/api/evidence/events", ingest_evidence, methods=["POST"])


from fastapi.exceptions import RequestValidationError as _RequestValidationError  # noqa: E402


@app.exception_handler(_RequestValidationError)
async def _validation_error(request: Request, exc: _RequestValidationError):
    """422 that can always be encoded. FastAPI echoes the offending input back; a body of 1e999
    (parsed as inf) or NaN made that echo itself fail strict JSON encoding, so a malformed request
    answered 500 instead of 422 on /api/chat, /api/fleet/tell and /api/terminal; so did a lone
    surrogate in the echoed input."""
    import json as _json

    from fastapi.responses import Response

    from .codex import jsonable
    # ensure_ascii: a lone surrogate in the echoed input (\\ud800 in the body) cannot be UTF-8
    # encoded and raised inside JSONResponse; as an ASCII escape it is valid JSON text
    body = _json.dumps({"detail": jsonable(exc.errors())}, ensure_ascii=True, allow_nan=False)
    return Response(body, status_code=422, media_type="application/json")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def core() -> TrinityCore:
    return state["core"]


def token_eq(given: Any, expected: str) -> bool:
    """Constant-time token check that never raises. hmac.compare_digest refuses str with non-ASCII
    characters (TypeError): a header or ?token= carrying one (latin-1 bytes, %ff, an emoji) turned a
    refused request into a 500 on every admin route and crashed the /ws/events handler."""
    if given is None or not expected:
        return False
    a = str(given).encode("utf-8", "surrogatepass")
    return hmac.compare_digest(a, expected.encode("utf-8", "surrogatepass"))


def require_admin(token: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(403, "Admin access is off. Set AVRONE_ADMIN_TOKEN to enable it.")
    if not token_eq(token, settings.admin_token):
        raise HTTPException(401, "Bad admin token")


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
ACCESS_POLICIES = ("loopback", "admin", "all")


def access_policy() -> str:
    """AVRONE_PUBLIC_ACCESS decides who may use the routes that run agents or read run history:
    loopback (default) - callers on 127.0.0.1/::1, or anyone with the admin token;
    admin - the admin token only, even from loopback; all - anyone who can reach the port
    (the pre-v0.27 behaviour; use it only behind a port bound to 127.0.0.1 or an auth proxy).
    Unknown values fall back to loopback, never to all."""
    import os
    v = (os.getenv("AVRONE_PUBLIC_ACCESS") or "loopback").strip().lower()
    return v if v in ACCESS_POLICIES else "loopback"


def caller_allowed(host: str | None, token: str | None) -> bool:
    policy = access_policy()
    if policy == "all":
        return True
    if settings.admin_token and token_eq(token, settings.admin_token):
        return True
    return policy == "loopback" and (host or "") in LOOPBACK_HOSTS


def require_local(request: Request, token: str | None) -> None:
    host = request.client.host if request.client else None
    if not caller_allowed(host, token):
        raise HTTPException(403, "This route is local-only: call from 127.0.0.1 or send X-Admin-Token "
                                 "(AVRONE_PUBLIC_ACCESS=loopback|admin|all).")


DEFAULT_MAX_BACKGROUND_RUNS = 32
HARD_MAX_BACKGROUND_RUNS = 1024
DEFAULT_MAX_MESSAGE_CHARS = 200_000
HARD_MAX_MESSAGE_CHARS = 5_000_000


def _env_cap(name: str, default: int, hard: int) -> int:
    """Positive int from env, clamped to 1..hard; junk or unset -> default."""
    try:
        n = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(hard, n))


def max_background_runs() -> int:
    return _env_cap("AVRONE_MAX_BACKGROUND_RUNS", DEFAULT_MAX_BACKGROUND_RUNS, HARD_MAX_BACKGROUND_RUNS)


def max_message_chars() -> int:
    return _env_cap("AVRONE_MAX_MESSAGE_CHARS", DEFAULT_MAX_MESSAGE_CHARS, HARD_MAX_MESSAGE_CHARS)


DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024
HARD_MAX_BODY_BYTES = 512 * 1024 * 1024


def max_body_bytes(path: str = "") -> int:
    """Largest request body this server reads for ``path``. Never below what the message cap needs
    (message + first_answer, worst-case JSON escaping), and the agent-pack route also fits a
    base64 bundle at the agent-fs quota."""
    n = max(_env_cap("AVRONE_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, HARD_MAX_BODY_BYTES),
            12 * max_message_chars() + 64 * 1024)
    if path == "/api/fleet/agentpack":
        from .agentpack import quota_bytes
        n = max(n, (quota_bytes() + 1024 * 1024) * 4 // 3 + 4 * 1024 * 1024)
    return n


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """ASGI guard: refuse an oversized request body with 413 before it is buffered. Routes read the
    whole body before auth (HMAC needs it; pydantic parses it), so without this an unauthenticated
    caller could make the server hold any amount of memory - e.g. 1 GB to /api/fleet/register.
    A declared Content-Length over the cap is refused at once; a chunked body is counted as it
    streams and cut off at the cap."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        limit = max_body_bytes(scope.get("path", ""))
        declared = None
        for k, v in scope.get("headers") or ():
            if k.lower() == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = None
        if declared is not None and declared > limit:
            return await self._refuse(send, limit)
        seen = 0
        started = False

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg.get("type") == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > limit:
                    raise _BodyTooLarge()
            return msg

        async def tracking_send(msg):
            nonlocal started
            if msg.get("type") == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._refuse(send, limit)

    @staticmethod
    async def _refuse(send, limit: int) -> None:
        import json as _json
        body = _json.dumps({"detail": f"request body too large (max {limit} bytes; AVRONE_MAX_BODY_BYTES)"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(BodyLimit)


class ChatIn(BaseModel):
    message: str
    mode: str = "squad"  # squad | chat | deep
    squad_size: int | None = None
    first_answer: str | None = None  # deep mode: the answer to stress-test (else a squad runs first)
    rounds: int | None = None  # deep mode: max waves, clamped to 1..5 (default settings/env, else 1)
    background: bool = False
    # squad/deep: server-managed checkpoint (data_dir/checkpoints/<id>.json). Re-posting the same
    # id and task resumes an interrupted (or background) run; a different task is refused (409).
    checkpoint_id: str | None = None
    # squad only: netsquad placement for this request (local|network|hybrid); default AVRONE_SQUAD_DISPATCH
    dispatch: str | None = None


class TerminalIn(BaseModel):
    cmd: str


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    c = core()
    return {
        "runner": c.runner.kind,
        "workspace": str(c.runner.workspace),
        "provider": settings.provider,
        "model": settings.llm_model,
        "backend": settings.llm_base_url if settings.llm_mode != "mock" else "mock",
        "squad_size": settings.squad_size,
        "deep_squad_size": settings.deep_squad_size,
        "max_parallel": settings.max_parallel,
        "tools": len(c.tools.names()),
        "admin_enabled": bool(settings.admin_token),
        "agents": c.active,
    }


def _open_checkpoint(cid_raw: str, task: str, mode: str):
    """(checkpoint id, file path, stored meta or None, release) for a client checkpoint_id."""
    from .checkpoint import CheckpointConflict, CheckpointError, check_compatible, claim, managed_path, sanitize_client_id
    if mode not in ("squad", "deep"):
        raise HTTPException(400, "checkpoint_id works with mode squad or deep")
    try:
        cid = sanitize_client_id(cid_raw)
        path = managed_path(settings.data_dir, cid)
        meta = check_compatible(path, task, mode)
        release = claim(path)
    except CheckpointConflict as e:
        raise HTTPException(409, str(e)) from None
    except CheckpointError as e:
        raise HTTPException(400, str(e)) from None
    return cid, path, meta, release


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request, x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    c = core()
    limit = max_message_chars()
    if len(body.message) > limit or len(body.first_answer or "") > limit:
        raise HTTPException(413, f"message too long (max {limit} characters; AVRONE_MAX_MESSAGE_CHARS)")
    if body.background and len(state.get("tasks") or ()) >= max_background_runs():
        raise HTTPException(429, f"too many background runs in flight (max {max_background_runs()}; "
                                 "AVRONE_MAX_BACKGROUND_RUNS); wait for one to finish")
    if body.mode not in ("squad", "chat", "deep"):
        raise HTTPException(400, f"Unknown mode {body.mode!r}. Use squad, chat or deep.")
    if body.dispatch is not None:
        if body.mode != "squad":
            raise HTTPException(400, "dispatch works with mode squad")
        if body.dispatch not in ("local", "network", "hybrid"):
            raise HTTPException(400, f"Unknown dispatch {body.dispatch!r}. Use local, network or hybrid.")
    run_id = uuid.uuid4().hex[:10]
    ck: dict = {}
    cid, release = None, None
    if body.checkpoint_id is not None:
        cid, path, meta, release = _open_checkpoint(body.checkpoint_id, body.message, body.mode)
        ck = {"checkpoint": path}
        if meta and isinstance(meta.get("run_id"), str):
            run_id = meta["run_id"]  # a resume keeps logging under the original run
    try:
        if body.mode == "squad":
            # Only pass dispatch when asked, so cores without the knob keep working.
            dk = {"dispatch": body.dispatch} if body.dispatch is not None else {}
            job = c.squad(body.message, body.squad_size, run_id, **ck, **dk)
        elif body.mode == "chat":
            job = c.chat(body.message, run_id)
        else:  # deep
            rounds = clamp_deep_rounds(body.rounds)
            # Only pass rounds when asked, so cores without the knob keep working.
            kw = {"rounds": rounds} if rounds is not None else {}
            job = c.deep(body.message, body.first_answer, body.squad_size, run_id, **kw, **ck)
    except BaseException:
        if release:
            release()
        raise
    extra = {"checkpoint_id": cid} if cid else {}
    if body.background:  # long-running artifact: return now, watch it on the HUD
        t = asyncio.create_task(job)
        state["tasks"].add(t)
        t.add_done_callback(state["tasks"].discard)
        if release:  # a background run that dies frees its checkpoint; re-post to resume
            t.add_done_callback(lambda _t: release())
        return {"run_id": run_id, "status": "running", **extra}
    try:
        res = await job
    finally:
        if release:
            release()
    return {**res, **extra} if isinstance(res, dict) and extra else res


# ------------------------------------------------------------------ checkpoints (admin)
def _ck_key(ck_id: str) -> str:
    from .checkpoint import CheckpointError, managed_path
    try:
        managed_path(settings.data_dir, ck_id)
    except CheckpointError:
        raise HTTPException(400, "bad checkpoint id (1-64 chars of A-Z a-z 0-9 _ -)") from None
    return ck_id


@app.get("/api/capgate")
async def capgate_status(x_admin_token: str | None = Header(default=None)):
    """capgate mode, the root policy, and a full walk of the receipt chain (admin only)."""
    require_admin(x_admin_token)
    from . import capgate
    return {"mode": capgate.mode(), "policy": capgate.policy(), "ttl": capgate.ttl(),
            "chain": capgate.verify_chain(settings.data_dir)}


@app.get("/api/checkpoints")
async def checkpoints_list(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    from .checkpoint import list_managed
    return list_managed(settings.data_dir)


@app.get("/api/checkpoints/{ck_id}")
async def checkpoint_get(ck_id: str, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    from .checkpoint import describe, managed_path
    row = describe(managed_path(settings.data_dir, _ck_key(ck_id)), full=True)
    if row is None:
        raise HTTPException(404, f"no checkpoint {ck_id!r}")
    return row


@app.delete("/api/checkpoints/{ck_id}")
async def checkpoint_delete(ck_id: str, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    from .checkpoint import CheckpointConflict, delete_managed
    try:
        if not delete_managed(settings.data_dir, _ck_key(ck_id)):
            raise HTTPException(404, f"no checkpoint {ck_id!r}")
    except CheckpointConflict as e:
        raise HTTPException(409, str(e)) from None
    return {"deleted": ck_id}


@app.get("/api/tools")
async def tools():
    return core().tools.list()


@app.get("/api/extensions")
async def extensions():
    return getattr(core(), "extensions", {})


# ------------------------------------------------------------------ fleet
def _fleet():
    f = getattr(core(), "fleet", None)
    if f is None or not f.enabled:
        raise HTTPException(404, "fleet disabled (set AVRONE_FLEET_TOKEN)")
    return f


async def _gateway(request: Request, kind: str) -> dict:
    """Inbound remote spawn/message: the single vetted path (fleet.dispatch_gateway)."""
    from .fleet import GatewayRefused
    f = _fleet()
    body = await request.body()
    try:
        return f.dispatch_gateway(kind, request.method, request.url.path, body,
                                  {k.lower(): v for k, v in request.headers.items()})
    except GatewayRefused as e:
        if e.code == "bad_body":
            raise HTTPException(400, str(e)) from None
        raise HTTPException(401 if e.auth else 403, str(e)) from None


async def _verified(request: Request) -> dict:
    f = _fleet()
    body = await request.body()
    try:
        f.verify(request.method, request.url.path, body, {k.lower(): v for k, v in request.headers.items()})
    except PermissionError as e:
        raise HTTPException(401, str(e)) from None
    import json as _json
    try:
        payload = _json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "body must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    return payload


@app.get("/api/fleet")
async def fleet_info(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    f = _fleet()
    from .codex import jsonable
    return jsonable({**f.info(), "nodes": f.list_nodes()})  # records saved before v0.40 may hold inf


@app.post("/api/fleet/register")
async def fleet_register(request: Request):
    payload = await _verified(request)
    try:
        return _fleet().register(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/fleet/message")
async def fleet_message(request: Request):
    payload = await _gateway(request, "message")
    try:
        return await _fleet().handle_message(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:  # a signed but malformed body is the caller's error, not a bad gateway
        raise HTTPException(400, str(e)[:300]) from None
    except (RuntimeError, KeyError) as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/fleet/directory")
async def fleet_directory(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    from .presence import directory
    return await directory(core())


class AgentMsgIn(BaseModel):
    agent: str
    message: str
    wait: bool = False


@app.post("/api/fleet/tell")
async def fleet_tell(body: AgentMsgIn, x_admin_token: str | None = Header(default=None)):
    """Operator/chatbot -> any agent in the fleet, by name or route/name."""
    require_admin(x_admin_token)
    from .presence import directory, find_agent
    f = _fleet()
    try:
        route, name = find_agent(await directory(core()), body.agent)
        return await f.send_message([p for p in route.split("/") if p], name, body.message,
                                    f"{f.node_id}/operator", 0, body.wait)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except (RuntimeError, PermissionError) as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/fleet/jobs")
async def fleet_jobs(job_id: str | None = None, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    f = _fleet()
    if job_id:
        j = await f.queue.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j
    return await f.queue.stats()


@app.post("/api/fleet/memory")
async def fleet_memory(request: Request):
    """Inbound memory sync from a child node: same signed gateway as run/message (kind "memory")."""
    payload = await _gateway(request, "memory")
    try:
        return _fleet().receive_memory(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/fleet/agentpack")
async def fleet_agentpack(request: Request):
    """Inbound agent-home migration (edgepack): signed gateway, kind "agentpack", cap agent:migrate."""
    payload = await _gateway(request, "agentpack")
    try:
        return _fleet().receive_agentpack(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/fleet/run")
async def fleet_run(request: Request):
    payload = await _gateway(request, "run")
    f = _fleet()
    from .checkpoint import CheckpointConflict, CheckpointError
    try:
        return await f.handle_run(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except CheckpointConflict as e:
        raise HTTPException(409, str(e)) from None
    except CheckpointError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/gateway")
async def gateway_status():
    return core().llm.status()


@app.get("/api/evolve")
async def api_evolve(request: Request, x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    from .evolve import list_accepted
    return {"accepted": list_accepted(core().settings)}


@app.get("/api/osint/metrics")
async def api_osint_metrics(request: Request, x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    from types import SimpleNamespace
    from .osint import t_osint_metrics
    c = core()
    ctx = SimpleNamespace(core=c, run_id="api", name="api", depth=0)
    return {"metrics": await t_osint_metrics(ctx, {})}


@app.get("/api/telemetry")
async def api_telemetry(request: Request, hours: float = 24, x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    from .telemetry import telemetry
    from .codex import jsonable
    return jsonable(telemetry(core(), hours * 3600))  # strict JSON whatever a sensor or log held


@app.get("/api/map")
async def api_map(request: Request, hours: float = 24, format: str = "json", x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    from fastapi.responses import PlainTextResponse
    from .telemetry import system_map, to_mermaid
    g = system_map(core(), hours * 3600)
    return PlainTextResponse(to_mermaid(g)) if format == "mermaid" else g


@app.get("/api/runs")
async def runs(request: Request, x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    return core().codex.runs()


@app.get("/api/runs/{run_id}")
async def run_log(run_id: str, request: Request, x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    import asyncio as _asyncio
    from .codex import replay_limit
    cx = core().codex
    limit = replay_limit()

    def read() -> tuple[list, int]:
        ev = cx.replay(run_id, limit=limit + 1)
        return ev, len(ev)
    # off the event loop: a big log used to stall every other request while it was parsed
    events, got = await _asyncio.to_thread(read)
    if not events:
        raise HTTPException(404, f"no run {run_id!r}")
    from fastapi.responses import JSONResponse
    headers = {}
    if got > limit:
        events = events[-limit:]
        headers["X-Avrone-Truncated"] = f"newest {limit} events (AVRONE_REPLAY_MAX_EVENTS)"
    return JSONResponse(events, headers=headers)


@app.get("/api/runs/{run_id}/bridge")
async def run_bridge(run_id: str, request: Request, x_admin_token: str | None = Header(default=None)):
    require_local(request, x_admin_token)
    """The learning bridge of a run: every post agents shared with each other, oldest first."""
    from .bridge import bridge_run_id, load_posts, safe_run_id
    if safe_run_id(run_id) != run_id:
        raise HTTPException(400, "bad run id")
    c = core()
    open_ = (getattr(c, "_bridges", None) or {}).get(bridge_run_id(run_id))
    posts = open_.posts() if open_ is not None else load_posts(c.settings.data_dir, run_id)
    if posts is None:
        if not c.codex.replay(run_id):
            raise HTTPException(404, f"no run {run_id!r}")
        posts = []
    return {"run_id": run_id, "count": len(posts), "posts": posts}


@app.post("/api/terminal")
async def terminal(body: TerminalIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    try:  # a lone surrogate ("\\udfff" in the JSON) cannot become a command line: 422, not a 500
        body.cmd.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(422, "cmd is not valid UTF-8 (lone surrogate)") from None
    # a NUL is NOT rejected here: the runner answers it as a result (exit -1, "NUL" in stderr),
    # the v0.26 contract (tests/test_robust_v026_api.py)
    res = await core().runner.run(body.cmd)
    core().codex.emit("admin", "tool.result", "Admin", tool="terminal", output=res.text()[:4000])
    return asdict(res)


def ws_send_timeout() -> float:
    """Seconds one event send may block before the client is dropped (AVRONE_WS_SEND_TIMEOUT)."""
    import math as _m
    import os
    try:
        v = float(os.environ.get("AVRONE_WS_SEND_TIMEOUT", "").strip() or 30)
    except ValueError:
        v = 30.0
    if not _m.isfinite(v):
        v = 30.0
    return max(1.0, min(600.0, v))


@app.websocket("/ws/events")
async def events(ws: WebSocket):
    token = ws.headers.get("x-admin-token") or ws.query_params.get("token")
    if not caller_allowed(ws.client.host if ws.client else None, token):
        await ws.close(code=1008)  # policy violation: the live codex stream shows every run
        return
    from .codex import SubscribersFull
    codex = core().codex
    try:
        q = codex.subscribe()
    except SubscribersFull:
        await ws.close(code=1013)  # try again later: the stream is at AVRONE_MAX_WS_SUBSCRIBERS
        return
    try:
        await ws.accept()
    except BaseException:
        codex.unsubscribe(q)
        raise


    try:
        async with anyio.create_task_group() as tg:
            async def pump() -> None:
                from .codex import dumps_event
                try:
                    while True:
                        ev = await q.get()
                        # strict JSON text: send_json raised on bytes/sets and sent bare NaN
                        with anyio.fail_after(ws_send_timeout()):
                            # a client that stops reading (stalled TCP, suspended laptop) used to
                            # hold its subscriber slot forever: 64 of them locked the HUD out
                            await ws.send_text(dumps_event(asdict(ev)))
                except TimeoutError:
                    with anyio.CancelScope(shield=True), anyio.move_on_after(1):
                        try:
                            await ws.close(code=1008, reason="client too slow to read the event stream")
                        except Exception:  # noqa: BLE001
                            pass
                except (WebSocketDisconnect, RuntimeError, OSError):
                    pass  # a send to a vanished client is a disconnect, not a server error
                tg.cancel_scope.cancel()

            async def watch() -> None:
                # A client that leaves while no events flow used to keep its queue (and this handler)
                # alive until the next event failed to send; listening for the disconnect frees it.
                try:
                    while True:
                        msg = await ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                except (WebSocketDisconnect, RuntimeError, OSError):
                    pass
                tg.cancel_scope.cancel()

            tg.start_soon(pump)
            tg.start_soon(watch)
    finally:
        codex.unsubscribe(q)


def main() -> None:
    import os
    import uvicorn
    uvicorn.run("avrone.server:app", host=os.getenv("AVRONE_HOST", "127.0.0.1"),
                port=int(os.getenv("AVRONE_PORT", "8710")))


if __name__ == "__main__":
    main()
