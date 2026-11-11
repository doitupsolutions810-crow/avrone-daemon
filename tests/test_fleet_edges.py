"""Offline edge-case tests for avrone.fleet: auth failures, registry rules, routing,
hop limits, status ageing, queue handling and the agent-facing fleet tools.

No real network: outbound fleet calls go through httpx.MockTransport via fleet.HTTP_TRANSPORT.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, sign, WINDOW_S


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
              "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def core():
    c = MagicMock()
    c.active = {"a": {"status": "working"}, "b": {"status": "done"}}
    c.tools.names.return_value = ["shell"]
    c.runner.kind = "local"
    c.codex.emit = MagicMock()
    c.fleet_hops = 0
    return c


@pytest.fixture
def fm(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    return FleetManager(core, tmp_path)


def add_node(fm, nid, url="http://n.test", age=0.0, load=0):
    fm.nodes[nid] = {"node_id": nid, "url": url, "last_seen": time.time() - age, "load": load,
                     "capabilities": {"runner": "local"}, "children": []}


def mock_transport(monkeypatch, handler):
    calls = []

    def h(request: httpx.Request):
        calls.append(request)
        return handler(request)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(h))
    return calls


# ---------------------------------------------------------------- construction / state
def test_env_config_and_persisted_id(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_FLEET_MAX_HOPS", "5")
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "a, b ,,")
    monkeypatch.setenv("AVRONE_FLEET_PARENT", "https://p.test/")
    monkeypatch.setenv("AVRONE_PUBLIC_URL", "https://me.test/")
    f = FleetManager(core, tmp_path)
    assert f.max_hops == 5 and f.allow == {"a", "b"}
    assert f.parent == "https://p.test" and f.public_url == "https://me.test"
    # generated id persists across restarts
    f2 = FleetManager(core, tmp_path)
    assert f2.node_id == f.node_id
    info = f.info()
    assert info["enabled"] and info["active_agents"] == 1 and info["children"] == 0


def test_hostname_fallback_when_sanitized_empty(core, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_mod.socket, "gethostname", lambda: "___")
    f = FleetManager(core, tmp_path)
    assert f.node_id.startswith("node-") and not f.enabled


def test_status_online_degraded_offline(fm):
    hb = fm.heartbeat_s
    assert fm.status_of({"last_seen": time.time()}) == "online"
    assert fm.status_of({"last_seen": time.time() - 5 * hb}) == "degraded"
    assert fm.status_of({"last_seen": time.time() - 20 * hb}) == "offline"
    assert fm.status_of({}) == "offline"
    add_node(fm, "x", age=5 * hb)
    assert fm.list_nodes()[0]["status"] == "degraded"


# ---------------------------------------------------------------- verify
def test_verify_disabled(core, tmp_path):
    f = FleetManager(core, tmp_path)
    with pytest.raises(PermissionError, match="fleet disabled"):
        f.verify("POST", "/p", b"", {})


@pytest.mark.parametrize("ts", ["", "abc", "12.5"])
def test_verify_bad_timestamp(fm, ts):
    with pytest.raises(PermissionError, match="bad timestamp"):
        fm.verify("POST", "/p", b"", {"x-avrone-ts": ts, "x-avrone-nonce": "n", "x-avrone-sig": "s"})


def test_verify_future_timestamp_is_stale(fm):
    h = sign("tok", "POST", "/p", b"", ts=str(int(time.time()) + WINDOW_S + 30))
    with pytest.raises(PermissionError, match="stale"):
        fm.verify("POST", "/p", b"", h)


@pytest.mark.parametrize("mutate", [
    lambda h, b, p: ("POST", "/other", b),       # path swapped
    lambda h, b, p: ("GET", p, b),               # method swapped
    lambda h, b, p: ("POST", p, b + b"x"),       # body tampered
])
def test_verify_signature_binds_method_path_body(fm, mutate):
    body, path = b'{"a":1}', "/api/fleet/run"
    h = sign("tok", "POST", path, body)
    m, p, b = mutate(h, body, path)
    with pytest.raises(PermissionError, match="bad signature"):
        fm.verify(m, p, b, h)


def test_verify_wrong_token_and_missing_sig(fm):
    h = sign("other", "POST", "/p", b"")
    with pytest.raises(PermissionError, match="bad signature"):
        fm.verify("POST", "/p", b"", h)
    h = sign("tok", "POST", "/p", b"")
    h.pop("x-avrone-sig")
    with pytest.raises(PermissionError, match="bad signature"):
        fm.verify("POST", "/p", b"", h)


def test_verify_nonce_reuse_with_fresh_signature_is_replay(fm):
    h1 = sign("tok", "POST", "/p", b"", nonce="fixed")
    fm.verify("POST", "/p", b"", h1)
    h2 = sign("tok", "POST", "/p", b"{}", nonce="fixed")  # different body, same nonce
    with pytest.raises(PermissionError, match="replayed"):
        fm.verify("POST", "/p", b"{}", h2)


def test_old_nonces_are_pruned(fm):
    fm._nonces = {"old": time.time() - 3 * WINDOW_S}
    fm.verify("POST", "/p", b"", sign("tok", "POST", "/p", b""))
    assert "old" not in fm._nonces and len(fm._nonces) == 1


# ---------------------------------------------------------------- register
@pytest.mark.parametrize("payload,exc,msg", [
    ({"node_id": "Bad ID", "url": "http://x"}, ValueError, "invalid node_id"),
    ({"node_id": "", "url": "http://x"}, ValueError, "invalid node_id"),
    ({"node_id": "root", "url": "http://x"}, ValueError, "collides"),
    ({"node_id": "kid", "url": "ftp://x"}, ValueError, "http"),
    ({"node_id": "kid"}, ValueError, "http"),
])
def test_register_rejects(fm, payload, exc, msg):
    with pytest.raises(exc, match=msg):
        fm.register(payload)
    assert "kid" not in fm.nodes


def test_register_allowlist(fm):
    fm.allow = {"good"}
    with pytest.raises(PermissionError, match="AVRONE_FLEET_ALLOW"):
        fm.register({"node_id": "evil", "url": "http://e"})
    assert fm.register({"node_id": "good", "url": "http://g/"}) == {"ok": True, "parent": "root"}
    assert fm.nodes["good"]["url"] == "http://g"


def test_register_collision_online_vs_offline(fm, core):
    fm.register({"node_id": "kid", "url": "http://a"})
    with pytest.raises(ValueError, match="already online"):
        fm.register({"node_id": "kid", "url": "http://b"})
    # same url re-registering (heartbeat) is fine
    fm.register({"node_id": "kid", "url": "http://a", "load": 4})
    assert fm.nodes["kid"]["load"] == 4
    # once the old one is no longer online, the id may move
    fm.nodes["kid"]["last_seen"] = time.time() - 5 * fm.heartbeat_s
    fm.register({"node_id": "kid", "url": "http://b"})
    assert fm.nodes["kid"]["url"] == "http://b"
    saved = json.loads(fm.path.read_text())
    assert saved["nodes"]["kid"]["url"] == "http://b"
    core.codex.emit.assert_called()


# ---------------------------------------------------------------- pick / routing
def test_pick_auto(fm):
    with pytest.raises(RuntimeError, match="no online nodes"):
        fm.pick("auto")
    add_node(fm, "busy", load=5)
    add_node(fm, "idle", load=1)
    add_node(fm, "dead", age=1000, load=0)
    add_node(fm, "blocked", load=0)
    fm.allow = {"busy", "idle", "dead"}
    assert fm.pick("")["node_id"] == "idle"


def test_pick_errors(fm):
    with pytest.raises(RuntimeError, match="unknown node 'x'; known: none"):
        fm.pick("x")
    add_node(fm, "off", age=1000)
    with pytest.raises(RuntimeError, match="offline"):
        fm.pick("off")
    add_node(fm, "deg", age=5 * fm.heartbeat_s)
    assert fm.pick("deg")["node_id"] == "deg"  # degraded is still usable
    fm.allow = {"deg"}
    with pytest.raises(PermissionError):
        fm.pick("off")


def test_dispatch_hop_limit(fm):
    with pytest.raises(PermissionError, match="max fleet hops"):
        run(fm.dispatch(["x"], "squad", "t", hops=fm.max_hops))


def test_dispatch_success_signs_and_balances_load(fm, monkeypatch):
    add_node(fm, "kid", url="http://kid.test", load=2)

    def handler(req):
        body = req.content
        # the receiving side must be able to verify what we sent
        other = FleetManager.__new__(FleetManager)
        other.token, other._nonces = "tok", {}
        other.verify("POST", req.url.path, body, dict(req.headers))
        p = json.loads(body)
        assert p["route"] == ["grandkid"] and p["hops"] == 2 and p["from"] == "root" and p["origin_run"] == "r1"
        assert fm.nodes["kid"]["load"] == 3  # bumped while in flight
        return httpx.Response(200, json={"node": "kid", "path": ["kid"], "answer": "ok"})
    calls = mock_transport(monkeypatch, handler)
    res = run(fm.dispatch(["kid", "grandkid"], "chat", "hi", hops=1, n=2, origin_run="r1"))
    assert res["answer"] == "ok" and len(calls) == 1
    assert str(calls[0].url) == "http://kid.test/api/fleet/run"
    assert fm.nodes["kid"]["load"] == 2


def test_dispatch_http_error_restores_load(fm, monkeypatch):
    add_node(fm, "kid", load=0)
    mock_transport(monkeypatch, lambda r: httpx.Response(502, text="boom" * 200))
    with pytest.raises(RuntimeError, match="-> 502: boom") as ei:
        run(fm.dispatch(["kid"], "squad", "t", 0))
    assert len(str(ei.value)) < 400  # body truncated
    assert fm.nodes["kid"]["load"] == 0


def test_dispatch_connect_error_restores_load(fm, monkeypatch):
    add_node(fm, "kid", load=1)

    def handler(req):
        raise httpx.ConnectError("refused", request=req)
    mock_transport(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        run(fm.dispatch(["kid"], "squad", "t", 0))
    assert fm.nodes["kid"]["load"] == 1


# ---------------------------------------------------------------- messages
def test_send_message_remote_and_hops(fm, monkeypatch):
    with pytest.raises(PermissionError):
        run(fm.send_message(["kid"], "A-1", "x", "me", hops=fm.max_hops))
    add_node(fm, "kid", url="http://kid.test")
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"path": ["kid"], "delivered": "live"})
    mock_transport(monkeypatch, handler)
    res = run(fm.send_message(["kid", "gk"], "A-1", "do it", "me", hops=0, wait=True))
    assert res["path"] == ["root", "kid"] and seen["route"] == ["gk"] and seen["wait"] is True


def test_deliver_local_live(fm, core):
    core.message_agent.return_value = True
    res = run(fm.send_message([], "Builder-2", "hey", "op"))
    assert res["delivered"] == "live" and res["path"] == ["root"]


def test_deliver_local_respawn_wait(fm, core):
    core.message_agent.return_value = False
    core.run_agent = AsyncMock(return_value="answer!")
    res = run(fm.deliver_local("Builder-2", "hey", "op", wait=True))
    assert res["delivered"] == "respawned" and res["answer"] == "answer!"
    assert core.run_agent.call_args.args[1] == "Builder"
    kinds = [c.args[1] for c in core.codex.emit.call_args_list]
    assert "run.start" in kinds and "run.done" in kinds


def test_deliver_local_respawn_background(fm, core):
    core.message_agent.return_value = False
    core.run_agent = AsyncMock(return_value="bg")

    async def go():
        res = await fm.deliver_local("Solo", "hey", "op", wait=False)
        assert len(fm._bg) == 1
        await asyncio.gather(*fm._bg)
        await asyncio.sleep(0)
        return res
    res = run(go())
    assert res["delivered"] == "respawned" and "answer" not in res and not fm._bg
    assert core.run_agent.call_args.args[1] == "Solo"


def test_handle_message(fm, core):
    with pytest.raises(PermissionError, match="exceeded"):
        run(fm.handle_message({"hops": fm.max_hops + 1, "agent": "a", "message": "m"}))
    with pytest.raises(ValueError, match="agent"):
        run(fm.handle_message({"hops": 1}))
    core.message_agent.return_value = True
    res = run(fm.handle_message({"agent": "A-1", "message": "m", "route": None}))
    assert res["delivered"] == "live"


# ---------------------------------------------------------------- handle_run / run_job
@pytest.mark.parametrize("mode,attr,key", [("chat", "chat", "answer"), ("deep", "deep", "report"),
                                           ("squad", "squad", "answer"), ("weird", "squad", "answer")])
def test_handle_run_local_modes(fm, core, mode, attr, key):
    seen = {}

    async def impl(*a, **k):
        seen["hops"] = core.fleet_hops
        return {"run_id": "r9", key: "done"}
    setattr(core, attr, impl)
    core.fleet_hops = 0
    res = run(fm.handle_run({"mode": mode, "prompt": "p", "hops": 2, "from": "parent"}))
    assert res == {"node": "root", "path": ["root"], "run_id": "r9", "answer": "done"}
    assert seen["hops"] == 2 and core.fleet_hops == 0  # restored


def test_handle_run_restores_hops_on_error(fm, core):
    core.squad = AsyncMock(side_effect=RuntimeError("x"))
    core.fleet_hops = 1
    with pytest.raises(RuntimeError):
        run(fm.handle_run({"prompt": "p", "hops": 2}))
    assert core.fleet_hops == 1


def test_handle_run_hop_limit_and_forward(fm, monkeypatch):
    with pytest.raises(PermissionError):
        run(fm.handle_run({"hops": fm.max_hops + 1}))
    add_node(fm, "kid")
    mock_transport(monkeypatch, lambda r: httpx.Response(200, json={"path": ["kid"], "answer": "a"}))
    res = run(fm.handle_run({"route": ["kid"], "hops": 1, "prompt": "p"}))
    assert res["path"] == ["root", "kid"]


def test_run_job_paths(fm, core, monkeypatch):
    core.chat = AsyncMock(return_value={"answer": "c"})
    core.squad = AsyncMock(return_value={"answer": "s"})
    core.message_agent.return_value = True
    assert run(fm.run_job("run", {"task": "t", "node": "local", "mode": "chat"}))["answer"] == "c"
    assert run(fm.run_job("run", {"task": "t", "node": "local"}))["answer"] == "s"
    assert run(fm.run_job("message", {"agent": "A", "message": "m"}))["delivered"] == "live"
    # "auto" with no nodes -> routing error the queue will retry
    with pytest.raises(RuntimeError, match="no online nodes"):
        run(fm.run_job("run", {"task": "t", "node": "auto"}))
    add_node(fm, "kid")
    mock_transport(monkeypatch, lambda r: httpx.Response(200, json={"answer": "remote"}))
    assert run(fm.run_job("run", {"task": "t", "node": "kid"}))["answer"] == "remote"


# ---------------------------------------------------------------- parent link
def test_join_parent_once_and_heartbeat_backoff(fm, core, monkeypatch):
    fm.parent, fm.public_url = "http://parent.test", "http://me.test"
    import avrone.presence as presence
    monkeypatch.setattr(presence, "snapshot", AsyncMock(return_value={"node_id": "root"}))
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "parent": "p"})
    mock_transport(monkeypatch, handler)
    assert run(fm.join_parent_once())["ok"]
    assert sent["node_id"] == "root" and sent["url"] == "http://me.test" and sent["load"] == 1

    # heartbeat: fail twice (backoff doubles), succeed (resets), then stop
    outcomes = [RuntimeError("down"), RuntimeError("down"), {"ok": True}]
    sleeps = []

    async def fake_join():
        o = outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o

    async def fake_sleep(s):
        sleeps.append(s)
        if not outcomes:
            raise asyncio.CancelledError
    monkeypatch.setattr(fm, "join_parent_once", fake_join)
    monkeypatch.setattr(fleet_mod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(fm.heartbeat_loop())
    hb = fm.heartbeat_s
    assert sleeps == [2 * hb, 4 * hb, hb]


def test_start_requires_parent_and_url(fm, monkeypatch):
    fm.start()  # no parent -> no task, and no running loop needed
    assert fm._task is None

    async def go():
        fm.parent, fm.public_url = "http://p", "http://me"
        monkeypatch.setattr(fm, "heartbeat_loop", AsyncMock())
        fm.start()
        t = fm._task
        assert t is not None
        fm.start()  # idempotent
        assert fm._task is t
        await fm.stop()
        assert fm._task is None
    run(go())


def test_start_queue_and_stop(fm, tmp_path):
    async def go():
        fm.start_queue(tmp_path)
        assert fm.queue is not None and fm.worker is not None
        t = asyncio.create_task(asyncio.sleep(10))
        fm._bg.add(t)
        await fm.stop()
        await asyncio.sleep(0)
        assert t.cancelled()
    run(go())


# ---------------------------------------------------------------- agent tools
def ctx_for(core, name="Lead-1"):
    return SimpleNamespace(core=core, run_id="run1", name=name, depth=0)


def test_tools_require_enabled_fleet(core, tmp_path):
    core.fleet = None
    with pytest.raises(RuntimeError, match="fleet disabled"):
        run(fleet_mod.t_fleet_nodes(ctx_for(core), {}))
    core.fleet = FleetManager(core, tmp_path)  # no token
    with pytest.raises(RuntimeError, match="fleet disabled"):
        run(fleet_mod.t_remote_spawn(ctx_for(core), {"task": "x"}))


def test_t_fleet_nodes(fm, core):
    core.fleet = fm
    assert "no child nodes registered" in run(fleet_mod.t_fleet_nodes(ctx_for(core), {}))
    add_node(fm, "kid", load=3)
    out = run(fleet_mod.t_fleet_nodes(ctx_for(core), {}))
    assert "kid [online] load=3" in out and "runner=local" in out


def test_t_remote_spawn(fm, core, monkeypatch):
    core.fleet = fm
    add_node(fm, "kid")
    mock_transport(monkeypatch, lambda r: httpx.Response(200, json={"path": ["kid"], "run_id": "x1", "answer": "hi"}))
    out = run(fleet_mod.t_remote_spawn(ctx_for(core), {"task": "t", "node": "kid"}))
    assert out.startswith("[root -> kid] run x1") and out.endswith("hi")
    out = run(fleet_mod.t_remote_spawn(ctx_for(core), {"task": "t"}))  # auto
    assert "run x1" in out


def test_t_remote_broadcast(fm, core, monkeypatch):
    core.fleet = fm
    assert run(fleet_mod.t_remote_broadcast(ctx_for(core), {"task": "t"})) == "ERROR: no online nodes"
    add_node(fm, "good", url="http://good.test")
    add_node(fm, "bad", url="http://bad.test")

    def handler(req):
        if req.url.host == "bad.test":
            return httpx.Response(500, text="nope")
        return httpx.Response(200, json={"answer": "yay"})
    mock_transport(monkeypatch, handler)
    out = run(fleet_mod.t_remote_broadcast(ctx_for(core), {"task": "t"}))
    assert "## good\nyay" in out and "## bad\nERROR: RuntimeError" in out
    # explicit list is deduped; unknown target reports error rather than raising
    out = run(fleet_mod.t_remote_broadcast(ctx_for(core), {"task": "t", "nodes": ["good", "good", "ghost"]}))
    assert out.count("## good") == 1 and "unknown node 'ghost'" in out


def test_t_fleet_agents_and_message(fm, core, monkeypatch):
    core.fleet = fm
    import avrone.presence as presence
    rows = [{"route": "", "node_id": "root", "status": "online", "env": {}, "agents": [{"name": "Builder-1", "status": "working"},
                                                                    {"name": "Critic-1", "status": "done"}]}]
    monkeypatch.setattr(presence, "directory", AsyncMock(return_value=rows))
    out = run(fleet_mod.t_fleet_agents(ctx_for(core), {"filter": "builder"}))
    assert out.startswith("directory from root:") and "Builder-1" in out and "Critic-1" not in out
    out = run(fleet_mod.t_fleet_agents(ctx_for(core), {"filter": "zzz-no-match"}))
    assert "Builder-1" not in out

    monkeypatch.setattr(presence, "find_agent", lambda rows, ref: ("", "Builder-1"))
    core.message_agent.return_value = True
    out = run(fleet_mod.t_agent_message(ctx_for(core), {"agent": "Builder-1", "message": "go"}))
    assert "[root] Builder-1: live" in out and "inbox" in out
    assert core.message_agent.call_args.args[2] == "root/Lead-1"
    core.message_agent.return_value = False
    core.run_agent = AsyncMock(return_value="did it")
    out = run(fleet_mod.t_agent_message(ctx_for(core), {"agent": "Builder-1", "message": "go", "wait": True}))
    assert out.endswith("\ndid it")


def test_t_queue_tools(fm, core, tmp_path):
    core.fleet = fm
    assert run(fleet_mod.t_queue_submit(ctx_for(core), {"task": "t"})) == "ERROR: queue not running"
    assert run(fleet_mod.t_queue_status(ctx_for(core), {})) == "ERROR: queue not running"

    async def go():
        from avrone.jobqueue import JobQueue
        fm.queue = JobQueue(tmp_path / "q.db")
        try:
            out = await fleet_mod.t_queue_submit(ctx_for(core), {"task": "t", "node": "kid", "priority": 2})
            assert out.startswith("queued job ") and "-> kid" in out
            jid = out.split()[2]
            st = await fleet_mod.t_queue_status(ctx_for(core), {"job_id": jid})
            assert st.startswith(f"job {jid} ") and "attempts=0/3" in st
            assert await fleet_mod.t_queue_status(ctx_for(core), {"job_id": "nope"}) == "ERROR: no such job"
            stats = json.loads(await fleet_mod.t_queue_status(ctx_for(core), {}))
            assert isinstance(stats, dict)
        finally:
            await fm.queue.close()
    run(go())


def test_register_fleet_tools():
    from avrone.tools import ToolRegistry
    r = ToolRegistry()
    fleet_mod.register_fleet_tools(r)
    assert {"fleet_nodes", "remote_spawn", "remote_broadcast", "agent_message",
            "queue_submit", "queue_status", "fleet_agents"} <= set(r.names())


def test_future_skewed_nonce_survives_until_request_leaves_timestamp_window(fm, monkeypatch):
    fixed_now = 1_800_000_000.0
    monkeypatch.setattr(fleet_mod.time, "time", lambda: fixed_now)
    timestamp = str(int(fixed_now) + WINDOW_S)
    headers = sign("tok", "POST", "/p", b"", ts=timestamp, nonce="future-window-edge")
    fm.verify("POST", "/p", b"", headers)

    # At exactly the opposite edge, the original signed request is still valid.
    # Its nonce must therefore remain reserved even though 2*WINDOW_S elapsed.
    fixed_now += 2 * WINDOW_S
    with pytest.raises(PermissionError, match="replayed"):
        fm.verify("POST", "/p", b"", headers)
    assert fm._nonces["future-window-edge"] == fixed_now
