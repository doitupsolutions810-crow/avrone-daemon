from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from avrone import server
from avrone.bridge import Bridge
from avrone.fleet import FleetManager, sign
from avrone.trial_adapters import BridgeTrialAdapter, FleetTrialAdapter
from avrone.trial_contract import make_trial

PATH = "/api/fleet/trials"
FLEET_TOKEN = "fleet-test-token-" + "f" * 40
RECEIPT_SECRET = "separate-receipt-secret-" + "r" * 40


class DummyCodex:
    def emit(self, *args, **kwargs):
        return None


def record(**overrides):
    values = dict(
        system="avrone", node="node-a", run_id="run-1", check_id="suite",
        check_version="7", attempt_id="attempt-1", failed=False,
    )
    values.update(overrides)
    return make_trial(**values)


def test_bridge_adapter_adds_read_only_correlations_and_checks_binding(tmp_path):
    bridge = Bridge("run-1", tmp_path, node_id="node-a")
    post = bridge.post("worker", "finding", "bounded observation")
    wrapped = BridgeTrialAdapter.wrap(bridge, record(), [post["id"]])
    assert wrapped["record"]["check_version"] == "7"
    assert wrapped["bridge"]["node_id"] == "node-a"
    assert wrapped["bridge"]["post_ids"] == [post["id"]]
    try:
        BridgeTrialAdapter.wrap(bridge, record(run_id="other-run"))
    except ValueError as exc:
        assert "context" in str(exc)
    else:
        raise AssertionError("mismatched Bridge context was accepted")
    try:
        BridgeTrialAdapter.wrap(bridge, record(), ["missing-post"])
    except ValueError as exc:
        assert "locally originated" in str(exc)
    else:
        raise AssertionError("missing Bridge post was accepted")


def test_fleet_adapter_requires_explicit_allowlist_and_uses_existing_signed_post():
    class FakeFleet:
        enabled = True
        allow = {"node-b"}
        node_id = "node-a"

        def check_outbound(self, *args):
            self.checked = args

        def pick(self, target):
            assert target == "node-b"
            return {"url": "https://node-b.example"}

        async def _post(self, base, path, payload, timeout):
            self.sent = (base, path, payload, timeout)
            return {"accepted": True}

    fleet = FakeFleet()
    wrapped = {"record": record(), "bridge": None}
    result = asyncio.run(FleetTrialAdapter.send(fleet, "node-b", [wrapped]))
    assert result["accepted"] is True
    assert fleet.checked == ("trial", "trials:ingest", 0, "node-b")
    assert fleet.sent[1] == PATH
    assert fleet.sent[2]["capability"] == "trials:ingest"

    fleet.allow = set()
    try:
        asyncio.run(FleetTrialAdapter.send(fleet, "node-b", [wrapped]))
    except PermissionError as exc:
        assert "explicit" in str(exc)
    else:
        raise AssertionError("trial relay was allowed without an explicit allowlist")


def _client(tmp_path, monkeypatch, allow="node-a"):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", FLEET_TOKEN)
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", allow)
    monkeypatch.setenv("AVRONE_FLEET_REQUIRE_ALLOW", "1" if allow else "0")
    monkeypatch.setenv("AVRONE_NODE_ID", "receiver-node")
    monkeypatch.setenv("AVRONE_TRIAL_DB", str(tmp_path / "trials.sqlite3"))
    monkeypatch.setenv("AVRONE_TRIAL_RECEIPT_SECRET", RECEIPT_SECRET)
    core = SimpleNamespace(codex=DummyCodex())
    core.fleet = FleetManager(core, tmp_path / "fleet")
    monkeypatch.setattr(server, "core", lambda: core)
    app = FastAPI()
    app.add_api_route(PATH, server.fleet_trials, methods=["POST"])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _payload(record_value=None, sender="node-a"):
    return {
        "from": sender,
        "hops": 1,
        "capability": "trials:ingest",
        "events": [{"record": record_value or record(), "bridge": None}],
    }


def _signed_headers(payload, nonce="api_nonce_00000001", signature_override=None):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signed = sign(FLEET_TOKEN, "POST", PATH, body, ts=str(int(time.time())), nonce=nonce)
    if signature_override is not None:
        signed["x-avrone-sig"] = signature_override
    return body, {"content-type": "application/json", **signed}


def test_fleet_api_accepts_retry_and_rejects_replayed_nonce(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            payload = _payload()
            body, headers = _signed_headers(payload)
            first = await client.post(PATH, content=body, headers=headers)
            _, retry_headers = _signed_headers(payload, nonce="api_nonce_00000002")
            retry = await client.post(PATH, content=body, headers=retry_headers)
            replay = await client.post(PATH, content=body, headers=headers)
            assert first.status_code == 201
            assert first.json()["results"][0]["status"] == "accepted"
            assert retry.status_code == 200
            assert retry.json()["results"][0]["status"] == "duplicate"
            assert replay.status_code == 401
            assert first.json()["results"][0]["receipt"]["receipt_id"] == retry.json()["results"][0]["receipt"]["receipt_id"]
    asyncio.run(go())


def test_fleet_api_requires_valid_signature_and_explicit_sender_allowlist(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch, allow="node-b") as client:
            payload = _payload()
            body, headers = _signed_headers(payload)
            refused = await client.post(PATH, content=body, headers=headers)
            assert refused.status_code == 403

            _, bad = _signed_headers(payload, nonce="api_nonce_00000003", signature_override="0" * 64)
            unauthenticated = await client.post(PATH, content=body, headers=bad)
            assert unauthenticated.status_code == 401
    asyncio.run(go())


def test_fleet_api_rejects_record_node_not_bound_to_signed_sender(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            payload = _payload(record(node="node-b"), sender="node-a")
            body, headers = _signed_headers(payload, nonce="api_nonce_00000010")
            response = await client.post(PATH, content=body, headers=headers)
            assert response.status_code == 422
            assert "does not match trial node" in response.json()["detail"]
    asyncio.run(go())


def test_empty_allowlist_is_not_allow_all_for_trial_ingest(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch, allow="") as client:
            payload = _payload()
            body, headers = _signed_headers(payload, nonce="api_nonce_00000004")
            response = await client.post(PATH, content=body, headers=headers)
            assert response.status_code == 403
    asyncio.run(go())


def test_fleet_api_rejects_duplicate_key_and_deep_json(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            duplicate = (
                b'{"from":"node-a","from":"node-a","hops":1,'
                b'"capability":"trials:ingest","events":[]}'
            )
            signed = sign(FLEET_TOKEN, "POST", PATH, duplicate,
                          ts=str(int(time.time())), nonce="api_nonce_00000005")
            response = await client.post(PATH, content=duplicate,
                                         headers={"content-type": "application/json", **signed})
            assert response.status_code == 400

            deep = (b'{"from":"node-a","hops":1,"capability":"trials:ingest","events":['
                    + b"[" * 80 + b"0" + b"]" * 80 + b"]}")
            signed = sign(FLEET_TOKEN, "POST", PATH, deep,
                          ts=str(int(time.time())), nonce="api_nonce_00000006")
            response = await client.post(PATH, content=deep,
                                         headers={"content-type": "application/json", **signed})
            assert response.status_code == 400

            # UTF-16LE plus U+0122 (whose low byte is 0x22) can desynchronize a
            # byte-only quote scanner and hide deeply nested JSON from its cap.
            payload = _payload()
            payload["events"][0]["record"]["run_id"] = "run-\u0122"
            deep_value = 0
            for _ in range(80):
                deep_value = [deep_value]
            payload["events"][0]["record"]["deep"] = deep_value
            utf16 = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-16-le")
            signed = sign(FLEET_TOKEN, "POST", PATH, utf16,
                          ts=str(int(time.time())), nonce="api_nonce_00000009")
            response = await client.post(PATH, content=utf16,
                                         headers={"content-type": "application/json", **signed})
            assert response.status_code == 400
            assert response.json()["detail"] == "body must be strict JSON"
    asyncio.run(go())


def test_fleet_api_rejects_oversized_body_and_duplicate_auth_headers(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            oversized = b"x" * (256 * 1024 + 1)
            response = await client.post(PATH, content=oversized,
                                         headers={"content-type": "application/json"})
            assert response.status_code == 413

            payload = _payload()
            body, headers = _signed_headers(payload, nonce="api_nonce_00000007")
            duplicate_headers = list(headers.items()) + [("x-avrone-ts", headers["x-avrone-ts"])]
            response = await client.post(PATH, content=body, headers=duplicate_headers)
            assert response.status_code == 401

            payload = _payload()
            body, privileged = _signed_headers(payload, nonce="api_nonce_00000008")
            privileged["x-avrone-privileged"] = "true"
            response = await client.post(PATH, content=body, headers=privileged)
            assert response.status_code == 403
    asyncio.run(go())
