"""Extended adversarial tests for the v2 trial receiver boundary."""
from __future__ import annotations

import json
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
