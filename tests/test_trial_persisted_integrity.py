"""Round 190: corrupt persisted trial projections must never become evidence.

These tests model SQLite corruption or incomplete unauthorized modification.
They do not claim protection against a privileged adversary who recomputes all
fields and hashes consistently: independent signed attestation is still needed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from avrone.trial_contract import MAX_ENVELOPE_BYTES, canonical_trial_bytes, make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def _trial(outcome="pass", attempt_id="attempt-190"):
    return make_trial(
        system="cyclekernel", node="node-190",
        run_id="run-190", check_id="check-190", check_version="v2",
        attempt_id=attempt_id, outcome=outcome,
        check_config={"profile": "strict"},
    )


def _seed(tmp_path, *, outcome="pass"):
    path = tmp_path / "private" / "trial.sqlite3"
    store = TrialStore(path)
    envelope = _trial(outcome=outcome)
    result = store.ingest_many([envelope], sender="peer-a", transport="fleet")
    assert result["results"][0]["status"] in {"accepted", "non_pass_stored"}
    return store, path, envelope


def _patch(store, sql, params=()):
    db = store._connect()
    try:
        db.execute(sql, params)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "column,bad",
    [
        pytest.param("outcome", "fail", id="outcome-flipped-to-failure"),
        pytest.param("outcome", "skipped", id="outcome-hidden-from-pool"),
        pytest.param("source", "forged:source", id="forged-source"),
        pytest.param("system", "other-system", id="system"),
        pytest.param("node", "other-node", id="node"),
        pytest.param("run_id", "other-run", id="run-id"),
        pytest.param("check_id", "other-check", id="check-id"),
        pytest.param("check_version", "v999", id="check-version"),
        pytest.param("check_config_sha256", "f" * 64, id="config-sha"),
        pytest.param("attempt_id", "other-attempt", id="attempt-id"),
        pytest.param("payload_sha256", "f" * 64, id="stored-payload-digest"),
        pytest.param("source_identity_key", "f" * 64, id="stored-source-key"),
        pytest.param("trial_id", "f" * 64, id="trial-id"),
    ],
)
def test_tampered_projection_fails_closed_for_every_evidence_reader(
    tmp_path, column, bad,
):
    store, path, envelope = _seed(tmp_path)
    _patch(store, f"UPDATE trials SET {column}=?", (bad,))

    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.non_pass_records()
    reopened = TrialStore(path)
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        reopened.pool_snapshot()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"not-json", id="malformed-json"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"{}", id="missing-envelope-fields"),
        pytest.param(b'{"x": NaN}', id="nonfinite-json"),
        pytest.param(b"X" * (MAX_ENVELOPE_BYTES + 1), id="overlimit"),
    ],
)
def test_malformed_or_unbounded_persisted_envelope_is_rejected(tmp_path, raw):
    store, _, _ = _seed(tmp_path)
    _patch(
        store,
        "UPDATE trials SET canonical_json=?, payload_sha256=?",
        (sqlite3.Binary(raw), hashlib.sha256(raw).hexdigest()),
    )
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()


def test_valid_but_noncanonical_serialization_is_rejected_even_with_updated_digest(tmp_path):
    store, _, envelope = _seed(tmp_path)
    noncanonical = json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8")
    assert noncanonical != canonical_trial_bytes(envelope)
    _patch(
        store,
        "UPDATE trials SET canonical_json=?,payload_sha256=?",
        (sqlite3.Binary(noncanonical), hashlib.sha256(noncanonical).hexdigest()),
    )
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()


def test_tampering_same_id_replay_cannot_add_false_duplicate_receipt(tmp_path):
    store, _, envelope = _seed(tmp_path)
    _patch(store, "UPDATE trials SET outcome='fail'")
    db = store._connect()
    try:
        before = db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0]
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.ingest_many([envelope], sender="peer-b", transport="fleet")
    db = store._connect()
    try:
        after = db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0]
        assert after == before
        assert db.execute("SELECT outcome FROM trials").fetchone()[0] == "fail"
    finally:
        db.close()


def test_alias_conflict_never_quarantines_sender_when_existing_alias_is_corrupt(tmp_path):
    store, _, envelope = _seed(tmp_path)
    _patch(store, "UPDATE trials SET source='forged:source'")
    alias = _trial(outcome="pass")
    alias["system"] = "CYCLEKERNEL"
    # Recompute identity legitimately using the shared source-equivalence rules.
    alias = make_trial(
        system="CYCLEKERNEL", node=envelope["node"],
        run_id=envelope["run_id"], check_id=envelope["check_id"],
        check_version=envelope["check_version"], attempt_id=envelope["attempt_id"],
        outcome="pass", check_config={"profile": "strict"},
    )
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.ingest_many([alias], sender="peer-b", transport="fleet")
    db = store._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM trial_quarantine").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trial_quarantined_senders").fetchone()[0] == 0
    finally:
        db.close()


def test_nonpass_record_cannot_be_promoted_to_pass_by_sqlite_outcome_edit(tmp_path):
    store, _, envelope = _seed(tmp_path, outcome="timed_out")
    assert store.pool_snapshot()["runs"] == 0
    assert [r["trial_id"] for r in store.non_pass_records()] == [envelope["trial_id"]]
    _patch(store, "UPDATE trials SET outcome='pass'")

    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.non_pass_records()


def test_one_bad_row_prevents_partial_report_from_other_healthy_rows(tmp_path):
    store, _, _ = _seed(tmp_path)
    second = _trial(attempt_id="second")
    assert store.ingest_many([second], sender="peer-a", transport="fleet")["accepted"] == 1
    _patch(store, "UPDATE trials SET outcome='fail' WHERE trial_id=?", (second["trial_id"],))
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()


def test_clean_mixed_outcomes_survive_restart_and_idempotent_replay(tmp_path):
    store, path, passed = _seed(tmp_path)
    failed = _trial(outcome="fail", attempt_id="failed")
    skipped = _trial(outcome="skipped", attempt_id="skipped")
    timed_out = _trial(outcome="timed_out", attempt_id="timeout")
    result = store.ingest_many([failed, skipped, timed_out], sender="peer-a", transport="fleet")
    assert result["accepted"] == 1 and result["non_pass"] == 2
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 2 and snapshot["failures"] == 1
    assert {r["outcome"] for r in store.non_pass_records()} == {"skipped", "timed_out"}

    reopened = TrialStore(path)
    assert reopened.pool_snapshot() == snapshot
    replay = reopened.ingest_many([passed, failed, skipped, timed_out], sender="peer-b", transport="fleet")
    assert replay["duplicates"] == 4
    assert reopened.pool_snapshot() == snapshot
