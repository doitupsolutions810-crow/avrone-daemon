import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod, server
from avrone.fleet import FleetManager, GatewayRefused, sign
from avrone.trial_contract import make_trial
from avrone.trial_store import MAX_TRIAL_BODY_BYTES

PATH = "/api/fleet/trials"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "peer-a")
    monkeypatch.delenv("AVRONE_FLEET_REQUIRE_ALLOW", raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def manager(tmp_path):
    core = MagicMock()
    core.fleet_hops = 0
    core.codex.emit = MagicMock()
    return FleetManager(core, tmp_path)


def row(outcome="passed", attempt="a1"):
    return make_trial(system="grok-bot", node="peer-a", run_id="run-1",
                      check_id="verify-bridge", check_version="v2",
                      attempt_id=attempt, outcome=outcome)


def request(payload, token="tok"):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return body, sign(token, "POST", PATH, body)


def payload(trials, sender="peer-a", **extra):
    return {"from": sender, "hops": 1, "capability": "evidence:ingest",
            "request_id": "req-1", "trials": trials, **extra}


def test_authenticated_gateway_receives_and_audits_trial(manager):
    body, headers = request(payload([row()]))
    accepted = manager.dispatch_gateway("trials", "POST", PATH, body, headers)
    result = manager.receive_trials(accepted)
    assert result["status"] == "accepted" and result["accepted"] == 1
    assert result["snapshot"]["runs"] == 1
    assert result["independence"] == "not_assessed"
    emitted = [call for call in manager.core.codex.emit.call_args_list
               if call.args[1] == "evidence.trial.receipt.v1"]
    assert emitted and emitted[0].kwargs["check_version"] == "v2"


def test_gateway_rejects_wrong_signature_and_unlisted_sender(manager):
    body, headers = request(payload([row()]), token="wrong")
    with pytest.raises(GatewayRefused) as error:
        manager.dispatch_gateway("trials", "POST", PATH, body, headers)
    assert error.value.code == "bad_signature"
    body, headers = request(payload([row()], sender="intruder"))
    with pytest.raises(GatewayRefused) as error:
        manager.dispatch_gateway("trials", "POST", PATH, body, headers)
    assert error.value.code == "sender_not_allowed"


def test_trials_require_explicit_nonempty_allowlist(manager):
    manager.allow = set()
    body, headers = request(payload([row()]))
    with pytest.raises(GatewayRefused) as error:
        manager.dispatch_gateway("trials", "POST", PATH, body, headers)
    assert error.value.code == "sender_not_allowed"


def test_gateway_rejects_missing_or_wrong_capability(manager):
    body, headers = request(payload([row()], capability="spawn:chat"))
    with pytest.raises(GatewayRefused) as error:
        manager.dispatch_gateway("trials", "POST", PATH, body, headers)
    assert error.value.code == "capability"


def test_direct_receiver_defense_in_depth_requires_allowlisted_sender(manager):
    with pytest.raises(PermissionError, match="explicit"):
        manager.receive_trials(payload([row()], sender="intruder"))


def test_asgi_route_enforces_body_limit_and_does_not_store(manager, monkeypatch):
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=manager))

    async def go():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(PATH, content=b"x" * (MAX_TRIAL_BODY_BYTES + 1))
    response = run(go())
    assert response.status_code == 413
    assert manager._trial_store_instance is None


def test_asgi_route_rejects_deep_json_without_storage(manager, monkeypatch):
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=manager))
    deep = b"[" * 1200 + b"0" + b"]" * 1200
    body = (b'{"from":"peer-a","hops":1,"capability":"evidence:ingest",'
            b'"trials":' + deep + b'}')
    headers = sign("tok", "POST", PATH, body)

    async def go():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(PATH, content=body, headers=headers)
    response = run(go())
    assert response.status_code == 400
    assert manager._trial_store_instance is None


def test_outbound_push_uses_fleet_hmac_and_versioned_contract(manager, monkeypatch):
    manager.allow = {"peer-b"}
    manager.nodes["peer-b"] = {
        "node_id": "peer-b", "url": "http://peer-b.test",
        "last_seen": time.time(), "load": 0, "capabilities": {}, "children": [],
    }
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        seen["headers"] = {key.lower(): value for key, value in request.headers.items()}
        return httpx.Response(200, json={"status": "accepted"})

    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    result = run(manager.push_trials("peer-b", [row()]))
    assert result["status"] == "accepted"
    assert seen["payload"]["capability"] == "evidence:ingest"
    assert seen["payload"]["from"] == "root"
    assert seen["headers"]["x-avrone-sig"]
