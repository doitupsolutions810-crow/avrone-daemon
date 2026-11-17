"""Table-driven adversarial tests for trial contract and adapter boundaries."""
from __future__ import annotations

import copy
import json

import pytest

from avrone.trial_adapters import (
    BRIDGE_TRIAL_SCHEMA,
    FLEET_TRIAL_SCHEMA,
    BridgeTrialAdapter,
    FleetTrialAdapter,
    strict_json_loads,
)
from avrone.trial_contract import make_trial, validate_trial_envelope
from avrone.trial_receiver import TrialStore


def trial(*, outcome="pass", attempt_id="attempt-1", **overrides):
    values = dict(
        system="cyclekernel",
        node="node-a",
        run_id="run-1",
        check_id="verify",
        check_version="v1",
        attempt_id=attempt_id,
        outcome=outcome,
        check_config={"mode": "airgap"},
    )
    values.update(overrides)
    return make_trial(**values)


def fleet_payload(envelopes, **overrides):
    return {
        "schema": FLEET_TRIAL_SCHEMA,
        "from": "peer-a",
        "hops": 1,
        "capability": "evidence:trial:ingest",
        "trials": envelopes,
        **overrides,
    }


def bridge_payload(envelopes, **overrides):
    return {
        "schema": BRIDGE_TRIAL_SCHEMA,
        "from": "peer-a",
        "hops": 1,
        "capability": "evidence:trial:ingest",
        "run_id": "bridge-1",
        "trials": envelopes,
        **overrides,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b" ",
        b"{",
        b'{"x":',
        b'{"x":1,}',
        b"[1,]",
        b'{"x":"\\ud800"}',
        b'{"x":"\\udfff"}',
        b'{"x":1e9999}',
        b'{"x":1e-9999}',
        b'{"x":' + b"9" * 5000 + b"}",
    ],
)
def test_strict_json_rejects_malformed_or_unrepresentable_inputs(raw):
    with pytest.raises(ValueError):
        strict_json_loads(raw)


@pytest.mark.parametrize("raw", [b"null", b"true", b"false", b"0", b"-0", b'""', b"[]", b"{}"])
def test_strict_json_accepts_valid_json_root_values(raw):
    strict_json_loads(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"outer":{"a":1,"a":2}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
    ],
)
def test_strict_json_rejects_ambiguous_or_nonfinite_values(raw):
    with pytest.raises(ValueError):
        strict_json_loads(raw)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sender": ""},
        {"sender": "Peer-A"},
        {"sender": "../peer"},
        {"sender": "peer a"},
        {"sender": "a" * 64},
        {"transport": "http"},
        {"transport": "bridge", "bridge_run_id": None},
        {"transport": "bridge", "bridge_run_id": " "},
        {"transport": "bridge", "bridge_run_id": "x" * 129},
        {"transport": "bridge", "bridge_run_id": "bad\nvalue"},
        {"transport": "fleet", "bridge_run_id": "unexpected"},
    ],
)
def test_store_rejects_invalid_sender_transport_and_bridge_metadata(tmp_path, kwargs):
    store = TrialStore(tmp_path / "trials.sqlite3")
    sender = kwargs.pop("sender", "peer-a")
    transport = kwargs.pop("transport", "fleet")
    bridge_run_id = kwargs.pop("bridge_run_id", None)
    with pytest.raises(ValueError):
        store.ingest_many(
            [trial()], sender=sender, transport=transport, bridge_run_id=bridge_run_id
        )


@pytest.mark.parametrize("batch", [None, (), {}, [], [trial()] * 11])
def test_store_rejects_non_list_empty_and_oversized_batches(tmp_path, batch):
    store = TrialStore(tmp_path / "trials.sqlite3")
    with pytest.raises(ValueError):
        store.ingest_many(batch, sender="peer-a", transport="fleet")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(schema="avrone.trial/v999"),
        lambda row: row.update(trial_id="0" * 64),
        lambda row: row.update(source="forged:source"),
        lambda row: row.update(outcome="success"),
        lambda row: row.update(system="bad:system"),
        lambda row: row.update(node="bad:node"),
        lambda row: row.update(attempt_id=1),
        lambda row: row.update(check_config_sha256="not-a-digest"),
        lambda row: row.update(unexpected=True),
    ],
)
def test_trial_envelope_tampering_is_rejected(mutation):
    record = trial()
    mutation(record)
    assert not validate_trial_envelope(record)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "wrong"},
        {"from": "peer-b"},
        {"hops": True},
        {"hops": 0},
        {"hops": 9},
        {"capability": "admin:*"},
        {"trials": []},
        {"unexpected": "field"},
    ],
)
def test_fleet_adapter_rejects_invalid_wrapper_fields(tmp_path, overrides):
    adapter = FleetTrialAdapter(TrialStore(tmp_path / "trials.sqlite3"))
    payload = fleet_payload([trial()])
    payload.update(overrides)
    with pytest.raises(ValueError):
        adapter.receive(payload, "peer-a", 8)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "wrong"},
        {"from": "peer-b"},
        {"hops": False},
        {"hops": 0},
        {"hops": 9},
        {"capability": "evidence:read"},
        {"run_id": ""},
        {"run_id": "x" * 129},
        {"run_id": "bad\rvalue"},
        {"trials": []},
        {"unexpected": "field"},
    ],
)
def test_bridge_adapter_rejects_invalid_wrapper_fields(tmp_path, overrides):
    adapter = BridgeTrialAdapter(TrialStore(tmp_path / "trials.sqlite3"))
    payload = bridge_payload([trial()])
    payload.update(overrides)
    with pytest.raises(ValueError):
        adapter.receive(payload, "peer-a", 8)


def test_adapter_accepts_hop_upper_bound_and_rejects_one_over(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    adapter = FleetTrialAdapter(store)
    payload = fleet_payload([trial()])
    payload["hops"] = 8
    assert adapter.receive(payload, "peer-a", 8)["accepted"] == 1

    other = fleet_payload([trial(attempt_id="attempt-2")])
    other["hops"] = 9
    with pytest.raises(ValueError, match="hops"):
        adapter.receive(other, "peer-a", 8)


def test_invalid_later_envelope_prevents_any_batch_storage(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    good = trial(attempt_id="good")
    bad = copy.deepcopy(trial(attempt_id="bad"))
    bad["trial_id"] = "f" * 64
    with pytest.raises(ValueError):
        store.ingest_many([good, bad], sender="peer-a", transport="fleet")
    assert store.pool_snapshot()["runs"] == 0


def test_valid_batch_commits_each_envelope_and_duplicate_order_is_stable(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    records = [trial(attempt_id=f"attempt-{i}") for i in range(10)]
    first = store.ingest_many(records, sender="peer-a", transport="fleet")
    retry = store.ingest_many(records, sender="peer-a", transport="fleet")
    assert first["accepted"] == 10
    assert retry["duplicates"] == 10
    assert [item["status"] for item in retry["results"]] == ["duplicate"] * 10
    assert store.pool_snapshot()["runs"] == 10


def test_nonpass_first_delivery_and_duplicate_never_enter_evidence_pool(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    record = trial(outcome="inconclusive")
    first = store.ingest_many([record], sender="peer-a", transport="fleet")
    retry = store.ingest_many([record], sender="peer-a", transport="fleet")
    assert first["non_pass"] == 1
    assert retry["duplicates"] == 1
    assert store.pool_snapshot()["runs"] == 0
    assert len(store.non_pass_records()) == 1


def test_failed_batch_receipt_keeps_prior_envelope_commit_and_rolls_back_current(tmp_path, monkeypatch):
    store = TrialStore(tmp_path / "trials.sqlite3")
    first = trial(attempt_id="committed-first")
    second = trial(attempt_id="must-rollback-second")
    original = store._record_receipt
    calls = 0

    def fail_second(db, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second receipt failure")
        return original(db, **kwargs)

    monkeypatch.setattr(store, "_record_receipt", fail_second)
    with pytest.raises(RuntimeError, match="second receipt"):
        store.ingest_many([first, second], sender="peer-a", transport="fleet")

    reopened = TrialStore(tmp_path / "trials.sqlite3")
    assert reopened.pool_snapshot()["runs"] == 1
    retry = reopened.ingest_many([first], sender="peer-a", transport="fleet")
    assert retry["duplicates"] == 1
    retry_second = reopened.ingest_many([second], sender="peer-a", transport="fleet")
    assert retry_second["accepted"] == 1


def test_receipt_retention_does_not_change_trial_idempotency(tmp_path, monkeypatch):
    import avrone.trial_receiver as receiver

    monkeypatch.setattr(receiver, "MAX_RECEIPTS", 1)
    store = TrialStore(tmp_path / "trials.sqlite3")
    records = [trial(attempt_id=f"r-{i}") for i in range(3)]
    for record in records:
        store.ingest_many([record], sender="peer-a", transport="fleet")

    db = store._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 3
    finally:
        db.close()

    assert store.ingest_many([records[0]], sender="peer-a", transport="fleet")["duplicates"] == 1
    assert store.pool_snapshot()["runs"] == 3
