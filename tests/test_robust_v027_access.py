"""v0.27: routes that run agents or read run history are local-only by default.
Before: anyone who could reach the port could POST /api/chat (agents with shell), read every run log
and bridge, and subscribe to /ws/events. AVRONE_PUBLIC_ACCESS=loopback|admin|all decides."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ADMIN = "test-token"  # tests/conftest sets AVRONE_ADMIN_TOKEN
GUARDED_GET = ["/api/runs", "/api/runs/x", "/api/runs/x/bridge", "/api/telemetry", "/api/map", "/api/evolve",
               "/api/osint/metrics"]
OPEN_GET = ["/api/status", "/api/tools", "/api/extensions", "/api/gateway"]


@pytest.fixture()
def mk(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)

    def make(policy, host="203.0.113.9"):
        if policy is None:
            monkeypatch.delenv("AVRONE_PUBLIC_ACCESS", raising=False)
        else:
            monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", policy)
        return TestClient(server.app, client=(host, 50000))
    return make


def _codes(c, headers=None):
    return {p: c.get(p, headers=headers or {}).status_code for p in GUARDED_GET}


def test_remote_refused_by_default(mk):
    with mk(None) as c:
        assert set(_codes(c).values()) == {403}
        r = c.post("/api/chat", json={"message": "rm -rf", "mode": "chat"})
        assert r.status_code == 403 and "local-only" in r.json()["detail"]
        for p in OPEN_GET:
            assert c.get(p).status_code == 200, p
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws/events") as ws:
                ws.receive_json()
        assert e.value.code == 1008


def test_remote_with_admin_token(mk):
    with mk("loopback") as c:
        assert 403 not in _codes(c, {"x-admin-token": ADMIN}).values()
        assert set(_codes(c, {"x-admin-token": "wrong"}).values()) == {403}
        with c.websocket_connect("/ws/events?token=" + ADMIN):
            pass


def test_loopback_allowed(mk):
    with mk(None, host="127.0.0.1") as c:
        assert 403 not in _codes(c).values()
    with mk(None, host="::1") as c:
        assert 403 not in _codes(c).values()


def test_admin_policy_needs_token_even_locally(mk):
    with mk("admin", host="127.0.0.1") as c:
        assert set(_codes(c).values()) == {403}
        assert 403 not in _codes(c, {"x-admin-token": ADMIN}).values()


def test_all_and_unknown_policy(mk):
    with mk("all") as c:
        assert 403 not in _codes(c).values()
    with mk("everyone") as c:  # typo falls back to loopback, never open
        assert set(_codes(c).values()) == {403}


def test_non_ascii_token_is_refused(mk, monkeypatch):
    # hmac.compare_digest on str raises TypeError for non-ASCII and used to 500.
    from fastapi import HTTPException
    from avrone import server
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "admin")
    assert server.caller_allowed("127.0.0.1", "édge") is False
    assert server.caller_allowed("203.0.113.9", "édge") is False
    assert server.caller_allowed("203.0.113.9", ADMIN) is True
    with pytest.raises(HTTPException) as ei:
        server.require_admin("édge")
    assert ei.value.status_code == 401
    with mk("admin", host="127.0.0.1") as c:
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws/events?token=%C3%A9dge") as ws:
                ws.receive_json()
        assert e.value.code == 1008


def test_admin_unset_means_no_token_bypass(mk, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "admin_token", "")
    with mk("loopback") as c:
        assert set(_codes(c, {"x-admin-token": ""}).values()) == {403}
