"""Extended adversarial tests for the v2 trial receiver boundary."""
from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from avrone.trial_adapters import (
    BRIDGE_TRIAL_SCHEMA,
    FLEET_TRIAL_SCHEMA,
    BridgeTrialAdapter,
    FleetTrialAdapter,
    strict_json_loads,
)
from avrone.trial_contract import canonical_config_bytes, make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def sample(outcome: str = "pass", attempt_id: str = "attempt-1"):
    return make_trial(
        system="cyclekernel",
        node="node-a",
        run_id="run-8",
        check_id="verify",
        check_version="10.2",
        attempt_id=attempt_id,
        outcome=outcome,
        check_config={"profile": "airgap", "label": "robot-\U0001F916"},
    )


def wrapper(schema: str, trials: list[dict], **extra):
    return {
        "schema": schema,
        "from": "peer-a",
        "hops": 1,
        "capability": "evidence:trial:ingest",
        "trials": trials,
        **extra,
    }


def test_store_rejects_symlinked_ancestor_before_creating_descendants(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(TrialStoreError, match="symlink"):
        TrialStore(linked_parent / "nested" / "trials.sqlite3")

    assert not (real_parent / "nested").exists()


def test_store_rejects_corrupt_database_without_replacing_bytes(tmp_path):
    path = tmp_path / "trials.sqlite3"
    sentinel = b"not-a-sqlite-database\x00do-not-replace"
    path.write_bytes(sentinel)

    with pytest.raises(TrialStoreError):
        TrialStore(path)

    assert path.read_bytes() == sentinel


def test_database_and_parent_permissions_are_private(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    TrialStore(path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_strict_json_rejects_overflowing_exponent_and_nested_duplicate_keys():
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(b'{"value":1e999}')
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(b'{"value":1e-999}')
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(b'{"outer":{"key":1,"key":2}}')


def test_strict_json_rejects_invalid_utf8_and_nonfinite_constants():
    for raw in (b'{"from":"peer-a","bad":NaN}', b'{"from":"peer-a","bad":Infinity}', b'\xff'):
        with pytest.raises(ValueError, match="unambiguous"):
            strict_json_loads(raw)


def test_strict_json_depth_boundary_is_exact():
    at_limit = b"[" * 32 + b"0" + b"]" * 32
    over_limit = b"[" * 33 + b"0" + b"]" * 33
    assert isinstance(strict_json_loads(at_limit), list)
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(over_limit)


def test_strict_json_node_budget_rejects_large_shallow_payload():
    # 100,001 array members stay below the 256 KiB HTTP byte cap.
    raw = b"[" + (b"0," * 100_000) + b"0]"
    assert len(raw) < 256 * 1024
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(raw)


def test_config_canonicalization_handles_supplementary_unicode_and_combining_marks():
    composed = {"emoji": "\U0001F916", "label": "caf\u00e9"}
    decomposed = {"label": "cafe\u0301", "emoji": "\U0001F916"}
    assert canonical_config_bytes(composed) == canonical_config_bytes(decomposed)


def test_conflicting_outcomes_racing_concurrently_keep_exactly_one_first_writer(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    pair = [("peer-a", sample("pass")), ("peer-b", sample("fail"))]
    barrier = Barrier(2)

    def ingest(item):
        sender, envelope = item
        barrier.wait(timeout=5)
        result = store.ingest_many([envelope], sender=sender, transport="fleet")
        return sender, envelope["outcome"], result["results"][0]["status"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(ingest, pair))

    accepted = [item for item in outcomes if item[2] == "accepted"]
    conflicts = [item for item in outcomes if item[2] == "conflict_quarantined"]
    assert len(accepted) == 1
    assert len(conflicts) == 1

    db = store._connect()
    try:
        row = db.execute(
            "SELECT canonical_json,outcome,first_sender FROM trials"
        ).fetchone()
        quarantine_count = db.execute(
            "SELECT COUNT(*) FROM trial_quarantine"
        ).fetchone()[0]
    finally:
        db.close()

    assert row["outcome"] == accepted[0][1]
    assert row["first_sender"] == accepted[0][0]
    assert json.loads(bytes(row["canonical_json"]))["outcome"] == accepted[0][1]
    assert quarantine_count == 1
    assert store.pool_snapshot()["runs"] == 1
    assert store.pool_snapshot()["independence"] == "not_assessed"

    quarantined_sender = conflicts[0][0]
    again = store.ingest_many(
        [sample(conflicts[0][1])], sender=quarantined_sender, transport="fleet"
    )
    assert again["results"][0]["status"] == "sender_quarantined"


def test_same_batch_conflict_quarantines_sender_without_replacing_first_trial(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    result = store.ingest_many(
        [sample("pass"), sample("fail")],
        sender="peer-a",
        transport="fleet",
    )

    assert result["status"] == "partial"
    assert [row["status"] for row in result["results"]] == [
        "accepted",
        "conflict_quarantined",
    ]
    assert store.pool_snapshot()["runs"] == 1
    assert store.pool_snapshot()["failures"] == 0

    fresh_attempt = store.ingest_many(
        [sample("pass", "attempt-2")], sender="peer-a", transport="fleet"
    )
    assert fresh_attempt["results"][0]["status"] == "sender_quarantined"


def test_bridge_and_fleet_duplicate_delivery_preserves_first_transport(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    envelope = sample()

    bridge = BridgeTrialAdapter(store)
    fleet = FleetTrialAdapter(store)
    first = bridge.receive(
        wrapper(BRIDGE_TRIAL_SCHEMA, [envelope], run_id="bridge-parent-wave1"),
        "peer-a",
        3,
    )
    retry = fleet.receive(wrapper(FLEET_TRIAL_SCHEMA, [envelope]), "peer-a", 3)

    assert first["results"][0]["status"] == "accepted"
    assert retry["results"][0]["status"] == "duplicate"

    db = store._connect()
    try:
        row = db.execute(
            "SELECT first_transport,bridge_run_id,first_sender FROM trials"
        ).fetchone()
    finally:
        db.close()

    assert row["first_transport"] == "bridge"
    assert row["bridge_run_id"] == "bridge-parent"
    assert row["first_sender"] == "peer-a"


def test_quarantine_capacity_exhaustion_rolls_back_sender_quarantine(tmp_path, monkeypatch):
    import avrone.trial_receiver as receiver

    monkeypatch.setattr(receiver, "MAX_QUARANTINE_RECORDS", 0)
    store = TrialStore(tmp_path / "trials.sqlite3")
    original = sample("pass")
    conflict = sample("fail")
    store.ingest_many([original], sender="peer-a", transport="fleet")

    with pytest.raises(TrialStoreError, match="capacity"):
        store.ingest_many([conflict], sender="peer-b", transport="fleet")

    # The failed conflict transaction must not quarantine peer-b or mutate the first row.
    duplicate = store.ingest_many([original], sender="peer-b", transport="fleet")
    assert duplicate["results"][0]["status"] == "duplicate"
    assert store.quarantine_records() == []
    assert store.pool_snapshot()["runs"] == 1


def test_receipt_retention_is_bounded_without_deleting_evidence(tmp_path, monkeypatch):
    import avrone.trial_receiver as receiver

    monkeypatch.setattr(receiver, "MAX_RECEIPTS", 2)
    store = TrialStore(tmp_path / "trials.sqlite3")
    envelopes = [sample("pass", f"attempt-{index}") for index in range(3)]

    for envelope in envelopes:
        store.ingest_many([envelope], sender="peer-a", transport="fleet")

    db = store._connect()
    try:
        trials = db.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
        receipts = db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0]
    finally:
        db.close()

    assert trials == 3
    assert receipts == 2
    assert store.pool_snapshot()["runs"] == 3


def test_receipt_failure_during_conflict_rolls_back_quarantine_and_sender_block(tmp_path, monkeypatch):
    store = TrialStore(tmp_path / "trials.sqlite3")
    original = sample("pass")
    conflict = sample("fail")
    store.ingest_many([original], sender="peer-a", transport="fleet")

    def fail_receipt(*args, **kwargs):
        raise RuntimeError("injected conflict receipt failure")

    monkeypatch.setattr(store, "_record_receipt", fail_receipt)
    with pytest.raises(RuntimeError, match="injected"):
        store.ingest_many([conflict], sender="peer-b", transport="fleet")

    reopened = TrialStore(tmp_path / "trials.sqlite3")
    assert reopened.quarantine_records() == []
    assert reopened.pool_snapshot()["runs"] == 1
    duplicate = reopened.ingest_many([original], sender="peer-b", transport="fleet")
    assert duplicate["results"][0]["status"] == "duplicate"


def test_trial_capacity_exhaustion_preserves_existing_evidence(tmp_path, monkeypatch):
    import avrone.trial_receiver as receiver

    monkeypatch.setattr(receiver, "MAX_TRIAL_RECORDS", 1)
    store = TrialStore(tmp_path / "trials.sqlite3")
    first = sample("pass", "first")
    second = sample("fail", "second")
    store.ingest_many([first], sender="peer-a", transport="fleet")

    with pytest.raises(TrialStoreError, match="capacity"):
        store.ingest_many([second], sender="peer-b", transport="fleet")

    duplicate = store.ingest_many([first], sender="peer-b", transport="fleet")
    assert duplicate["results"][0]["status"] == "duplicate"
    assert store.pool_snapshot()["runs"] == 1


def test_partial_batch_commit_is_recoverable_by_resending_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "trials.sqlite3"
    store = TrialStore(path)
    envelopes = [sample("pass", "batch-a"), sample("fail", "batch-b")]
    original_record_receipt = store._record_receipt
    calls = 0

    def fail_second_receipt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second-envelope receipt failure")
        return original_record_receipt(*args, **kwargs)

    monkeypatch.setattr(store, "_record_receipt", fail_second_receipt)
    with pytest.raises(RuntimeError, match="second-envelope"):
        store.ingest_many(envelopes, sender="peer-a", transport="fleet")

    reopened = TrialStore(path)
    db = reopened._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 1
    finally:
        db.close()

    retry = reopened.ingest_many(envelopes, sender="peer-a", transport="fleet")
    assert [item["status"] for item in retry["results"]] == ["duplicate", "accepted"]
    assert reopened.pool_snapshot()["runs"] == 2


def test_body_limit_rejects_oversized_chunked_request_without_reading_more(tmp_path):
    import asyncio

    from avrone.server import BodyLimit

    chunks = [
        {"type": "http.request", "body": b"a" * 200_000, "more_body": True},
        {"type": "http.request", "body": b"b" * (64 * 1024 + 1), "more_body": True},
        {"type": "http.request", "body": b"c", "more_body": False},
    ]
    reads = 0
    sent = []

    async def receive():
        nonlocal reads
        message = chunks[reads]
        reads += 1
        return message

    async def send(message):
        sent.append(message)

    async def downstream(scope, limited_receive, downstream_send):
        while True:
            message = await limited_receive()
            if not message.get("more_body", False):
                break
        await downstream_send({"type": "http.response.start", "status": 200, "headers": []})
        await downstream_send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/fleet/trials",
        "headers": [],
    }
    asyncio.run(BodyLimit(downstream)(scope, receive, send))

    assert reads == 2
    assert sent[0]["status"] == 413


@pytest.fixture
def trial_api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from avrone import server

    monkeypatch.setattr(server.settings, "workspace", tmp_path / "workspace")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "t" * 32)
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "peer-a")
    monkeypatch.setenv("AVRONE_FLEET_REQUIRE_ALLOW", "true")
    with TestClient(server.app) as client:
        yield client


def _signed_api_request(client, path, payload, *, sign_path=None, raw_override=None):
    from avrone.fleet import sign

    raw = raw_override
    if raw is None:
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    signed_path = sign_path or path
    headers = {
        "content-type": "application/json",
        **sign("t" * 32, "POST", signed_path, raw),
    }
    return client.post(path, content=raw, headers=headers)


def test_http_replay_nonce_is_rejected_and_does_not_duplicate_receipt(trial_api_client, tmp_path):
    from avrone.trial_adapters import FLEET_TRIAL_SCHEMA

    path = "/api/fleet/trials"
    body = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    from avrone.fleet import sign
    headers = {"content-type": "application/json", **sign("t" * 32, "POST", path, raw)}

    first = trial_api_client.post(path, content=raw, headers=headers)
    replay = trial_api_client.post(path, content=raw, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 401

    store = TrialStore(tmp_path / "data" / "trials" / "trials.sqlite3")
    db = store._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 1
    finally:
        db.close()


def test_http_body_tampering_after_signature_is_rejected_before_storage(trial_api_client, tmp_path):
    from avrone.fleet import sign
    from avrone.trial_adapters import FLEET_TRIAL_SCHEMA

    path = "/api/fleet/trials"
    body = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    tampered = raw.replace(b'"from":"peer-a"', b'"from":"peer-b"')
    assert tampered != raw and len(tampered) == len(raw)
    headers = {"content-type": "application/json", **sign("t" * 32, "POST", path, raw)}

    response = trial_api_client.post(path, content=tampered, headers=headers)
    assert response.status_code == 401
    assert not (tmp_path / "data" / "trials" / "trials.sqlite3").exists()


def test_http_signature_is_bound_to_exact_bridge_route(trial_api_client, tmp_path):
    from avrone.trial_adapters import BRIDGE_TRIAL_SCHEMA

    fleet_path = "/api/fleet/trials"
    bridge_path = "/api/fleet/bridge/trials"
    body = wrapper(BRIDGE_TRIAL_SCHEMA, [sample()], run_id="bridge-1")
    response = _signed_api_request(
        trial_api_client, bridge_path, body, sign_path=fleet_path
    )
    assert response.status_code == 401
    assert not (tmp_path / "data" / "trials" / "trials.sqlite3").exists()


def test_http_strict_json_rejects_overflowing_number_after_valid_hmac(trial_api_client, tmp_path):
    from avrone.trial_adapters import FLEET_TRIAL_SCHEMA

    path = "/api/fleet/trials"
    body = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    raw = raw[:-1] + b',"overflow":1e999}'
    response = _signed_api_request(trial_api_client, path, body, raw_override=raw)

    assert response.status_code == 400
    assert not (tmp_path / "data" / "trials" / "trials.sqlite3").exists()


def test_strict_json_rejects_escaped_lone_surrogates_in_values_and_keys():
    for raw in (b'{"value":"\\ud800"}', b'{"\\udfff":"value"}'):
        with pytest.raises(ValueError, match="unambiguous"):
            strict_json_loads(raw)


def test_body_limit_accepts_exact_trial_body_cap():
    import asyncio

    from avrone.server import BodyLimit, TRIAL_INGEST_MAX_BODY_BYTES

    payload = b"x" * TRIAL_INGEST_MAX_BODY_BYTES
    reads = 0
    sent = []

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        sent.append(message)

    async def downstream(scope, limited_receive, downstream_send):
        message = await limited_receive()
        assert len(message["body"]) == TRIAL_INGEST_MAX_BODY_BYTES
        await downstream_send({"type": "http.response.start", "status": 200, "headers": []})
        await downstream_send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/fleet/trials",
        "headers": [(b"content-length", str(len(payload)).encode("ascii"))],
    }
    asyncio.run(BodyLimit(downstream)(scope, receive, send))

    assert reads == 1
    assert sent[0]["status"] == 200


def test_body_limit_rejects_declared_oversize_before_calling_downstream():
    import asyncio

    from avrone.server import BodyLimit, TRIAL_INGEST_MAX_BODY_BYTES

    reads = 0
    sent = []

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(message):
        sent.append(message)

    async def downstream(scope, limited_receive, downstream_send):
        raise AssertionError("downstream must not run for a declared oversized request")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/fleet/trials",
        "headers": [(b"content-length", str(TRIAL_INGEST_MAX_BODY_BYTES + 1).encode("ascii"))],
    }
    asyncio.run(BodyLimit(downstream)(scope, receive, send))

    assert reads == 0
    assert sent[0]["status"] == 413


def test_cross_process_duplicate_delivery_commits_one_trial_and_two_receipts(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path

    path = tmp_path / "trials.sqlite3"
    TrialStore(path)
    go = tmp_path / "go"
    payload = json.dumps(sample(), separators=(",", ":"), ensure_ascii=False)
    root = Path(__file__).resolve().parents[1]
    worker = """
import json, pathlib, sys, time
from avrone.trial_receiver import TrialStore
database, ready, gate, raw = sys.argv[1:]
store = TrialStore(database)
pathlib.Path(ready).touch()
deadline = time.monotonic() + 12
while not pathlib.Path(gate).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(3)
    time.sleep(0.005)
result = store.ingest_many([json.loads(raw)], sender="peer-a", transport="fleet")
print(result["results"][0]["status"])
"""
    processes = []
    ready_files = [tmp_path / "ready-a", tmp_path / "ready-b"]
    results = []
    try:
        for ready in ready_files:
            processes.append(subprocess.Popen(
                [sys.executable, "-c", worker, str(path), str(ready), str(go), payload],
                cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 15
        while not all(ready.exists() for ready in ready_files):
            if time.monotonic() >= deadline:
                raise AssertionError("workers did not reach the synchronized start gate")
            if any(proc.poll() is not None for proc in processes):
                raise AssertionError("a worker exited before the synchronized start gate")
            time.sleep(0.01)
        go.touch()
        results = [proc.communicate(timeout=20) for proc in processes]
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    assert all(proc.returncode == 0 for proc in processes), results
    assert sorted(stdout.strip() for stdout, _ in results) == ["accepted", "duplicate"]

    reopened = TrialStore(path)
    db = reopened._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 2
    finally:
        db.close()
    assert reopened.pool_snapshot()["runs"] == 1
    assert reopened.pool_snapshot()["independence"] == "not_assessed"


def test_process_exit_before_receipt_commit_recovers_without_partial_trial(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "trials.sqlite3"
    TrialStore(path)
    envelope = json.dumps(sample(), separators=(",", ":"), ensure_ascii=False)
    root = Path(__file__).resolve().parents[1]
    script = """
import json, os, sys
from avrone.trial_receiver import TrialStore
store = TrialStore(sys.argv[1])
def crash_before_receipt(*args, **kwargs):
    os._exit(73)
store._record_receipt = crash_before_receipt
store.ingest_many([json.loads(sys.argv[2])], sender="peer-a", transport="fleet")
raise SystemExit(4)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path), envelope],
        cwd=str(root), capture_output=True, text=True, timeout=15,
    )
    assert child.returncode == 73, child.stderr

    recovered = TrialStore(path)
    db = recovered._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trial_quarantine").fetchone()[0] == 0
    finally:
        db.close()
    assert recovered.pool_snapshot()["runs"] == 0


def test_http_valid_hmac_from_unlisted_sender_is_denied_before_storage(trial_api_client, tmp_path):
    from avrone.fleet import sign
    from avrone.trial_adapters import FLEET_TRIAL_SCHEMA

    path = "/api/fleet/trials"
    body = wrapper(FLEET_TRIAL_SCHEMA, [sample()], **{"from": "peer-b"})
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", **sign("t" * 32, "POST", path, raw)}

    response = trial_api_client.post(path, content=raw, headers=headers)
    assert response.status_code == 403
    assert not (tmp_path / "data" / "trials" / "trials.sqlite3").exists()


def test_http_trial_ingest_denies_empty_allowlist_even_with_valid_hmac(trial_api_client, tmp_path):
    from avrone import server
    from avrone.fleet import sign
    from avrone.trial_adapters import FLEET_TRIAL_SCHEMA

    server._fleet().allow = []
    server._fleet().require_allow = False
    path = "/api/fleet/trials"
    body = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", **sign("t" * 32, "POST", path, raw)}

    response = trial_api_client.post(path, content=raw, headers=headers)
    assert response.status_code == 403
    assert "non-empty" in response.text
    assert not (tmp_path / "data" / "trials" / "trials.sqlite3").exists()


def test_invalid_later_batch_member_rejects_before_any_trial_or_receipt_is_written(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    valid = sample("pass", "attempt-valid")
    malformed = sample("fail", "attempt-invalid")
    malformed["source"] = "forged:source"

    with pytest.raises(ValueError, match="invalid avrone.trial"):
        store.ingest_many([valid, malformed], sender="peer-a", transport="fleet")

    db = store._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trial_quarantine").fetchone()[0] == 0
    finally:
        db.close()
    assert store.pool_snapshot()["runs"] == 0


def test_acknowledgement_loss_after_commit_retries_as_duplicate(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "trials.sqlite3"
    TrialStore(path)
    envelope = json.dumps(sample("pass", "ack-loss"), separators=(",", ":"))
    root = Path(__file__).resolve().parents[1]
    script = """
import json, os, sys
from avrone.trial_receiver import TrialStore
store = TrialStore(sys.argv[1])
result = store.ingest_many([json.loads(sys.argv[2])], sender="peer-a", transport="fleet")
assert result["results"][0]["status"] == "accepted"
os._exit(74)  # commit completed; transport acknowledgement is deliberately lost
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path), envelope],
        cwd=str(root), capture_output=True, text=True, timeout=15,
    )
    assert child.returncode == 74, child.stderr

    recovered = TrialStore(path)
    retry = recovered.ingest_many(
        [json.loads(envelope)], sender="peer-a", transport="fleet"
    )
    assert retry["results"][0]["status"] == "duplicate"

    db = recovered._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 2
    finally:
        db.close()
    assert recovered.pool_snapshot()["runs"] == 1


def test_store_rejects_hardlinked_existing_database_without_mutating_original(tmp_path):
    import sqlite3

    original = tmp_path / "sensitive.sqlite3"
    connection = sqlite3.connect(original)
    try:
        connection.execute("CREATE TABLE protected (value TEXT NOT NULL)")
        connection.execute("INSERT INTO protected(value) VALUES ('do-not-modify')")
        connection.commit()
    finally:
        connection.close()

    linked_dir = tmp_path / "receiver"
    linked_dir.mkdir(mode=0o700)
    linked = linked_dir / "trials.sqlite3"
    os.link(original, linked)
    before = original.read_bytes()

    assert original.stat().st_ino == linked.stat().st_ino
    assert original.stat().st_nlink == 2
    with pytest.raises(TrialStoreError, match="hard link"):
        TrialStore(linked)

    assert original.read_bytes() == before
    connection = sqlite3.connect(original)
    try:
        assert connection.execute("SELECT value FROM protected").fetchone()[0] == "do-not-modify"
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "trials" not in names
    finally:
        connection.close()


@pytest.mark.parametrize("sidecar", ["-wal", "-shm"])
def test_sqlite_sidecar_symlink_never_modifies_external_target(tmp_path, sidecar):
    from pathlib import Path
    path = tmp_path / "trials.sqlite3"
    TrialStore(path)
    target = tmp_path / "external-sentinel.bin"
    sentinel = b"external data must remain unchanged"
    target.write_bytes(sentinel)
    link = Path(str(path) + sidecar)
    link.symlink_to(target)

    try:
        store = TrialStore(path)
        store.ingest_many([sample("pass", "sidecar-check")], sender="peer-a", transport="fleet")
    except TrialStoreError:
        # Refusal is safe; the key invariant is never following the planted sidecar link.
        pass

    assert target.read_bytes() == sentinel


def test_cross_process_conflicting_outcomes_quarantine_only_losing_sender(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path

    path = tmp_path / "trials.sqlite3"
    TrialStore(path)
    gate = tmp_path / "go"
    payloads = [
        ("peer-a", sample("pass", "cross-process-conflict")),
        ("peer-b", sample("fail", "cross-process-conflict")),
    ]
    root = Path(__file__).resolve().parents[1]
    worker = """
import json, pathlib, sys, time
from avrone.trial_receiver import TrialStore
database, sender, ready, gate, raw = sys.argv[1:]
store = TrialStore(database)
pathlib.Path(ready).touch()
deadline = time.monotonic() + 12
while not pathlib.Path(gate).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(3)
    time.sleep(0.005)
result = store.ingest_many([json.loads(raw)], sender=sender, transport="fleet")
print(json.dumps({"sender": sender, **result["results"][0]}, sort_keys=True))
"""
    processes = []
    ready_files = [tmp_path / "ready-peer-a", tmp_path / "ready-peer-b"]
    outputs = []
    try:
        for (sender, envelope), ready in zip(payloads, ready_files):
            raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
            processes.append(subprocess.Popen(
                [sys.executable, "-c", worker, str(path), sender, str(ready), str(gate), raw],
                cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 15
        while not all(ready.exists() for ready in ready_files):
            if time.monotonic() >= deadline:
                raise AssertionError("conflicting workers did not become ready")
            if any(proc.poll() is not None for proc in processes):
                raise AssertionError("a conflicting worker exited before the start gate")
            time.sleep(0.01)
        gate.touch()
        outputs = [proc.communicate(timeout=20) for proc in processes]
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    assert all(proc.returncode == 0 for proc in processes), outputs
    results = [json.loads(stdout) for stdout, _ in outputs]
    accepted = [item for item in results if item["status"] == "accepted"]
    quarantined = [item for item in results if item["status"] == "conflict_quarantined"]
    assert len(accepted) == len(quarantined) == 1

    winner = accepted[0]
    loser = quarantined[0]
    store = TrialStore(path)
    db = store._connect()
    try:
        row = db.execute(
            "SELECT outcome,first_sender FROM trials"
        ).fetchone()
        q = db.execute(
            "SELECT sender,incoming_json FROM trial_quarantine"
        ).fetchone()
        sender_blocks = db.execute(
            "SELECT sender FROM trial_quarantined_senders"
        ).fetchall()
    finally:
        db.close()

    assert row["outcome"] == winner["outcome"]
    assert row["first_sender"] == winner["sender"]
    assert q["sender"] == loser["sender"]
    assert json.loads(bytes(q["incoming_json"]))["outcome"] == loser["outcome"]
    assert {item["sender"] for item in sender_blocks} == {loser["sender"]}
    retry = store.ingest_many(
        [sample("pass" if loser["outcome"] == "fail" else "fail", "fresh-attempt")],
        sender=loser["sender"], transport="fleet",
    )
    assert retry["results"][0]["status"] == "sender_quarantined"


def test_batch_size_accepts_exact_limit_and_rejects_one_over_without_partial_write(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    maximum = [sample("pass", f"batch-limit-{index}") for index in range(10)]
    accepted = store.ingest_many(maximum, sender="peer-a", transport="fleet")

    assert accepted["status"] == "accepted"
    assert accepted["accepted"] == 10
    assert len(accepted["results"]) == 10

    over_limit = [sample("pass", f"batch-over-{index}") for index in range(11)]
    with pytest.raises(ValueError, match="1..10"):
        store.ingest_many(over_limit, sender="peer-a", transport="fleet")

    db = store._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 10
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 10
    finally:
        db.close()
    assert store.pool_snapshot()["runs"] == 10


def test_sqlite_busy_writer_fails_closed_then_recovers_after_lock_release(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    blocker = store._connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(TrialStoreError, match="transaction failed"):
            store.ingest_many(
                [sample("pass", "busy-writer")],
                sender="peer-a",
                transport="fleet",
            )
    finally:
        blocker.rollback()
        blocker.close()

    db = store._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 0
    finally:
        db.close()

    recovered = store.ingest_many(
        [sample("pass", "busy-writer")],
        sender="peer-a",
        transport="fleet",
    )
    assert recovered["results"][0]["status"] == "accepted"
    assert store.pool_snapshot()["runs"] == 1


def test_quarantined_sender_capacity_exhaustion_is_transactional(tmp_path, monkeypatch):
    import avrone.trial_receiver as receiver

    monkeypatch.setattr(receiver, "MAX_QUARANTINED_SENDERS", 1)
    store = TrialStore(tmp_path / "trials.sqlite3")

    first = sample("pass", "sender-cap-a")
    store.ingest_many([first], sender="peer-a", transport="fleet")
    store.ingest_many([sample("fail", "sender-cap-a")], sender="peer-b", transport="fleet")
    assert store.quarantine_records()[0]["sender"] == "peer-b"

    second = sample("pass", "sender-cap-b")
    store.ingest_many([second], sender="peer-a", transport="fleet")
    with pytest.raises(TrialStoreError, match="quarantine capacity"):
        store.ingest_many(
            [sample("fail", "sender-cap-b")],
            sender="peer-c",
            transport="fleet",
        )

    # Capacity failure must not persist either the conflict or a sender-block row.
    assert len(store.quarantine_records()) == 1
    db = store._connect()
    try:
        blocked = {
            row[0] for row in db.execute(
                "SELECT sender FROM trial_quarantined_senders"
            ).fetchall()
        }
        trials = db.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    finally:
        db.close()
    assert blocked == {"peer-b"}
    assert trials == 2

    duplicate = store.ingest_many([second], sender="peer-c", transport="fleet")
    assert duplicate["results"][0]["status"] == "duplicate"


def test_sqlite_full_error_on_receipt_rolls_back_staged_trial(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "trials.sqlite3"
    store = TrialStore(path)
    original_connect = store._connect

    class FailReceiptWrite:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("INSERT INTO TRIAL_RECEIPTS"):
                raise sqlite3.OperationalError("database or disk is full")
            return self.connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        store,
        "_connect",
        lambda: FailReceiptWrite(original_connect()),
    )
    with pytest.raises(TrialStoreError, match="transaction failed"):
        store.ingest_many(
            [sample("pass", "sqlite-full-receipt")],
            sender="peer-a",
            transport="fleet",
        )

    monkeypatch.setattr(store, "_connect", original_connect)
    recovered = original_connect()
    try:
        # The trial INSERT succeeded inside the transaction; the injected SQLITE_FULL
        # on the receipt INSERT must roll that trial back before it becomes visible.
        assert recovered.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 0
        assert recovered.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 0
    finally:
        recovered.close()
    assert store.pool_snapshot()["runs"] == 0
