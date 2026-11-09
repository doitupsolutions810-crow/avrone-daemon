import asyncio
import hashlib
import json
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest

from avrone.fleet import FleetManager, sign
from avrone.server import app
from avrone.trial_contract import canonical_trial, make_trial
from avrone.trial_receiver import TrialStore, signed_receipt, verify_receipt

TOKEN = "trial-fleet-test-secret-" + "x" * 40
PATH = "/api/fleet/evidence"


class FakeCodex:
    def emit(self, *args, **kwargs):
        return None


def fleet_for(tmp_path, monkeypatch, allow="sender-node"):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOKEN)
    monkeypatch.delenv("AVRONE_FLEET_TOKEN_PREV", raising=False)
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", allow)
    monkeypatch.delenv("AVRONE_FLEET_REQUIRE_ALLOW", raising=False)
    monkeypatch.setenv("AVRONE_NODE_ID", "receiver-node")
    return FleetManager(SimpleNamespace(codex=FakeCodex()), tmp_path)


def delivery(record, sender="sender-node", token=TOKEN, extra=None):
    payload = {
        "schema": "avrone.trial-delivery/v1",
        "capability": "evidence:ingest",
        "from": sender,
        "hops": 1,
        "trial": record,
    }
    if extra:
        payload.update(extra)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", **sign(token, "POST", PATH, body)}
    return body, headers


def client(monkeypatch, fleet):
    import avrone.server as server
    monkeypatch.setattr(server, "_fleet", lambda: fleet)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def run(coro):
    return asyncio.run(coro)


def sample(**overrides):
    values = dict(
        system="cyclekernel", node="node-a", run_id="run-http", check_id="integrity",
        check_version="v4", attempt_id="attempt-1", outcome="pass",
        check_config={"profile": "strict"},
    )
    values.update(overrides)
    return make_trial(**values)


def test_fleet_http_accepts_duplicate_and_returns_signed_receipts(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)

    async def go():
        async with client(monkeypatch, fleet) as c:
            body, headers = delivery(sample())
            first = await c.post(PATH, content=body, headers=headers)
            body2, headers2 = delivery(sample())
            second = await c.post(PATH, content=body2, headers=headers2)
            assert first.status_code == 201
            assert second.status_code == 200
            assert first.json()["status"] == "accepted"
            assert second.json()["status"] == "duplicate"
            assert verify_receipt(TOKEN, first.json()["receipt"])
            assert verify_receipt(TOKEN, second.json()["receipt"])

    run(go())
    assert TrialStore(tmp_path / "trial_evidence.sqlite3").count() == 1


def test_fleet_http_conflict_quarantines_and_non_pass_is_explicit(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)

    async def go():
        async with client(monkeypatch, fleet) as c:
            body, headers = delivery(sample(outcome="pass"))
            passed = await c.post(PATH, content=body, headers=headers)
            conflict_body, conflict_headers = delivery(sample(outcome="fail"))
            conflict = await c.post(PATH, content=conflict_body, headers=conflict_headers)
            timeout_body, timeout_headers = delivery(sample(run_id="run-timeout", outcome="timed_out"))
            timeout = await c.post(PATH, content=timeout_body, headers=timeout_headers)
            assert passed.status_code == 201
            assert conflict.status_code == 409
            assert conflict.json()["status"] == "quarantined"
            assert timeout.status_code == 202
            assert timeout.json()["status"] == "stored_non_pass"
            assert timeout.json()["poolable"] is False

    run(go())
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    assert store.count() == 2 and store.quarantine_count() == 1
    assert store.pool_snapshot()["runs"] == 1


def test_fleet_http_requires_explicit_sender_allowlist(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch, allow="")

    async def go():
        async with client(monkeypatch, fleet) as c:
            body, headers = delivery(sample())
            response = await c.post(PATH, content=body, headers=headers)
            assert response.status_code == 403

    run(go())


def test_fleet_http_rejects_unlisted_sender_and_bad_signature(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)

    async def go():
        async with client(monkeypatch, fleet) as c:
            body, headers = delivery(sample(), sender="stranger-node")
            assert (await c.post(PATH, content=body, headers=headers)).status_code == 403
            body, headers = delivery(sample(), token="wrong-" + TOKEN)
            assert (await c.post(PATH, content=body, headers=headers)).status_code == 401

    run(go())


def test_fleet_http_rejects_privileged_flag(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)

    async def go():
        async with client(monkeypatch, fleet) as c:
            body, headers = delivery(sample(), extra={"privileged": True})
            assert (await c.post(PATH, content=body, headers=headers)).status_code == 403

    run(go())


def test_fleet_http_rejects_duplicate_json_keys_and_oversized_body(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)

    async def go():
        async with client(monkeypatch, fleet) as c:
            raw = b'{"schema":"avrone.trial-delivery/v1","schema":"avrone.trial-delivery/v1",'
            raw += b'"capability":"evidence:ingest","from":"sender-node","hops":1,"trial":'
            raw += json.dumps(sample()).encode() + b"}"
            headers = {"content-type": "application/json", **sign(TOKEN, "POST", PATH, raw)}
            assert (await c.post(PATH, content=raw, headers=headers)).status_code == 400
            body, headers = delivery(sample(), extra={"padding": "x" * (70 * 1024)})
            assert (await c.post(PATH, content=body, headers=headers)).status_code == 413

    run(go())


def test_fleet_http_rejects_deep_json_and_invalid_media_type(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)

    async def go():
        async with client(monkeypatch, fleet) as c:
            raw = b"[" * 1500 + b"0" + b"]" * 1500
            headers = {"content-type": "application/json", **sign(TOKEN, "POST", PATH, raw)}
            assert (await c.post(PATH, content=raw, headers=headers)).status_code == 400
            body, headers = delivery(sample())
            headers["content-type"] = "text/plain"
            assert (await c.post(PATH, content=body, headers=headers)).status_code == 415

    run(go())


def test_outbound_push_trial_rejects_receipt_for_wrong_peer(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)

    async def fake_post(base, path, payload, timeout=30):
        receipt_data = {
            "receipt_id": str(uuid.uuid4()),
            "trial_id": payload["trial"]["trial_id"],
            "payload_digest": hashlib.sha256(canonical_trial(payload["trial"])).hexdigest(),
            "status": "accepted",
            "received_at": int(time.time()),
        }
        return {
            "status": "accepted",
            "receipt": signed_receipt(TOKEN, receipt_data, payload["from"], "fleet", "other-peer"),
        }

    fleet.pick = lambda node_id: {"url": "http://peer-node"}
    fleet._post = fake_post
    with pytest.raises(RuntimeError, match="mismatched trial receipt"):
        run(fleet.push_trial("sender-node", sample()))


def test_outbound_push_trial_uses_existing_allowlisted_peer_and_contract(tmp_path, monkeypatch):
    fleet = fleet_for(tmp_path, monkeypatch)
    captured = {}

    async def fake_post(base, path, payload, timeout=30):
        captured.update(base=base, path=path, payload=payload, timeout=timeout)
        receipt_data = {
            "receipt_id": str(uuid.uuid4()),
            "trial_id": payload["trial"]["trial_id"],
            "payload_digest": hashlib.sha256(canonical_trial(payload["trial"])).hexdigest(),
            "status": "accepted",
            "received_at": int(time.time()),
        }
        return {
            "status": "accepted",
            "receipt": signed_receipt(TOKEN, receipt_data, payload["from"], "fleet", "peer-node"),
        }

    fleet.pick = lambda node_id: {"url": "http://peer-node"}
    fleet._post = fake_post
    result = run(fleet.push_trial("sender-node", sample()))
    assert result["status"] == "accepted"
    assert captured["base"] == "http://peer-node"
    assert captured["path"] == PATH
    assert captured["payload"]["capability"] == "evidence:ingest"
    assert captured["payload"]["trial"]["check_version"] == "v4"
