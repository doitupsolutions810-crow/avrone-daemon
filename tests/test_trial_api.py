import json

import pytest
from fastapi.testclient import TestClient

from avrone.fleet import sign
from avrone.trial_adapters import BRIDGE_TRIAL_SCHEMA, FLEET_TRIAL_SCHEMA
from avrone.trial_contract import make_trial

TOKEN = "t" * 32


@pytest.fixture
def client(tmp_path, monkeypatch):
    from avrone import server

    monkeypatch.setattr(server.settings, "workspace", tmp_path / "workspace")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOKEN)
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "peer-a")
    monkeypatch.setenv("AVRONE_FLEET_REQUIRE_ALLOW", "true")
    with TestClient(server.app) as test_client:
        yield test_client


def envelope(outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-1", run_id="run-1", check_id="airgap-verify",
        check_version="10.2", attempt_id="attempt-1", outcome=outcome,
    )


def payload(schema, trial, **extra):
    return {
        "schema": schema, "from": "peer-a", "hops": 1,
        "capability": "evidence:trial:ingest", "trials": [trial], **extra,
    }


def post_signed(client, path, body_payload, token=TOKEN):
    raw = json.dumps(
        body_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return client.post(
        path, content=raw,
        headers={"content-type": "application/json", **sign(token, "POST", path, raw)},
    )


def test_fleet_endpoint_authenticates_and_quarantines_conflicts(client):
    path = "/api/fleet/trials"
    first = post_signed(client, path, payload(FLEET_TRIAL_SCHEMA, envelope("pass")))
    assert first.status_code == 200, first.text
    assert first.json()["results"][0]["status"] == "accepted"

    duplicate = post_signed(client, path, payload(FLEET_TRIAL_SCHEMA, envelope("pass")))
    assert duplicate.status_code == 200
    assert duplicate.json()["results"][0]["status"] == "duplicate"

    conflict = post_signed(client, path, payload(FLEET_TRIAL_SCHEMA, envelope("fail")))
    assert conflict.status_code == 200
    assert conflict.json()["results"][0]["status"] == "conflict_quarantined"


def test_fleet_endpoint_rejects_bad_signature_and_unallowlisted_sender(client):
    path = "/api/fleet/trials"
    bad_sig = post_signed(client, path, payload(FLEET_TRIAL_SCHEMA, envelope()), token="x" * 32)
    assert bad_sig.status_code == 401

    body = payload(FLEET_TRIAL_SCHEMA, envelope())
    body["from"] = "peer-b"
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    refused = client.post(
        path, content=raw,
        headers={"content-type": "application/json", **sign(TOKEN, "POST", path, raw)},
    )
    assert refused.status_code == 403


def test_bridge_endpoint_requires_versioned_bridge_wrapper(client):
    path = "/api/fleet/bridge/trials"
    body = payload(BRIDGE_TRIAL_SCHEMA, envelope(), run_id="bridge-9")
    response = post_signed(client, path, body)
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["status"] == "accepted"


def test_trial_endpoint_has_a_hard_body_cap():
    from avrone.server import max_body_bytes

    assert max_body_bytes("/api/fleet/trials") == 256 * 1024
    assert max_body_bytes("/api/fleet/bridge/trials") == 256 * 1024
    assert max_body_bytes("/avrone/api/fleet/trials") == 256 * 1024
    assert max_body_bytes("/prefix/avrone/api/fleet/bridge/trials") == 256 * 1024


def test_trial_endpoint_rejects_duplicate_json_keys(client):
    path = "/api/fleet/trials"
    body = payload(FLEET_TRIAL_SCHEMA, envelope())
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    raw = raw.replace(b'"schema":', b'"schema":"avrone.trial-ingest/v1","schema":', 1)
    response = client.post(
        path, content=raw,
        headers={"content-type": "application/json", **sign(TOKEN, "POST", path, raw)},
    )
    assert response.status_code == 400


def test_trial_endpoint_rejects_oversized_body_before_auth(client):
    response = client.post("/api/fleet/trials", content=b"x" * (256 * 1024 + 1))
    assert response.status_code == 413


def test_trial_ingest_requires_a_nonempty_sender_allowlist(client):
    from avrone import server

    fleet = server.state["core"].fleet
    fleet.allow = set()
    fleet.require_allow = False
    response = post_signed(
        client, "/api/fleet/trials", payload(FLEET_TRIAL_SCHEMA, envelope())
    )
    assert response.status_code == 403
