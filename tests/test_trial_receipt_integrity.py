"""Round 196: retained receipts must substantiate their recorded delivery status.

Unlike cryptographically signed off-host ledgers, this only checks local,
retained records against known canonical trial and quarantine state. Pruned
receipts and repeat-blocked raw envelopes cannot be recovered.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="attempt-196", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-196",
        check_id="receipt-audit", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trial.sqlite3"
    store = TrialStore(path)
    passed = trial()
    skipped = trial("skip-196", "skipped")
    conflict = trial(outcome="fail")

    assert store.ingest_many([passed], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([passed], sender="peer-b", transport="fleet")["duplicates"] == 1
    assert store.ingest_many(
        [skipped], sender="peer-a", transport="bridge", bridge_run_id="bridge-196"
    )["non_pass"] == 1
    assert store.ingest_many(
        [conflict], sender="peer-c", transport="bridge", bridge_run_id="bridge-196"
    )["quarantined"] == 1
    assert store.ingest_many(
        [trial("blocked")], sender="peer-c", transport="fleet"
    )["results"][0]["status"] == "sender_quarantined"
    assert [r["result"] for r in store.receipt_records()] == [
        "accepted", "duplicate", "non_pass_stored", "conflict_quarantined",
        "sender_quarantined",
    ]
    return store, path


def sql(store, statement, args=()):
    db = store._connect()
    try:
        db.execute(statement, args)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("result", "unrecognized", id="unrecognized-status"),
        pytest.param("result", "non_pass_stored", id="false-nonpass"),
        pytest.param("trial_id", "f" * 64, id="forged-trial-id"),
        pytest.param("payload_sha256", "f" * 64, id="forged-digest"),
        pytest.param("payload_sha256", "invalid", id="malformed-digest"),
        pytest.param("sender", "../external", id="invalid-sender"),
        pytest.param("sender", "peer-x", id="changed-first-sender"),
        pytest.param("transport", "unknown", id="unsupported-transport"),
        pytest.param("transport", "bridge", id="missing-bridge-run-id"),
        pytest.param("bridge_run_id", "forged-bridge", id="bridge-on-fleet"),
        pytest.param("received_at", -1.0, id="negative-timestamp"),
        pytest.param("received_at", "yesterday", id="text-timestamp"),
        pytest.param("receipt_id", -3, id="invalid-sequence"),
    ],
)
def test_corrupted_accepted_receipt_denied_without_modifying_trial(
    tmp_path, field, value,
):
    store, path = seeded(tmp_path)
    before = store.pool_snapshot()
    sql(store, f"UPDATE trial_receipts SET {field}=? WHERE result='accepted'", (value,))
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()
    reopened = TrialStore(path)
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        reopened.receipt_records()
    assert store.pool_snapshot() == before


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("payload_sha256", "a" * 64, id="forged-digest"),
        pytest.param("trial_id", "a" * 64, id="forged-trial-id"),
        pytest.param("result", "accepted", id="wrong-status"),
        pytest.param("sender", "peer-z", id="wrong-sender"),
        pytest.param("received_at", 0.0, id="wrong-time"),
    ],
)
def test_corrupted_conflict_receipt_does_not_produce_partial_audit(tmp_path, field, value):
    store, _ = seeded(tmp_path)
    sql(store, f"UPDATE trial_receipts SET {field}=? WHERE result='conflict_quarantined'", (value,))
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()
    assert len(store.quarantine_records()) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("sender", "peer-z", id="sender-no-longer-blocked"),
        pytest.param("result", "accepted", id="false-acceptance"),
        pytest.param("payload_sha256", "bad", id="malformed-hash"),
    ],
)
def test_tampered_blocked_delivery_receipt_is_refused(tmp_path, field, value):
    store, _ = seeded(tmp_path)
    sql(store, f"UPDATE trial_receipts SET {field}=? WHERE result='sender_quarantined'", (value,))
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


def test_corrupted_duplicate_receipt_digest_is_refused(tmp_path):
    store, _ = seeded(tmp_path)
    sql(store, "UPDATE trial_receipts SET payload_sha256=? WHERE result='duplicate'", ("f"*64,))
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


def test_wrong_first_receipt_time_does_not_alter_pool(tmp_path):
    store, _ = seeded(tmp_path)
    before = store.pool_snapshot()
    sql(store, "UPDATE trial_receipts SET received_at=received_at+1 WHERE result='non_pass_stored'")
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()
    assert store.pool_snapshot() == before


def test_retained_accepted_receipt_without_stored_trial_is_refused(tmp_path):
    store, _ = seeded(tmp_path)
    sql(store, "DELETE FROM trials WHERE trial_id=?", (trial()["trial_id"],))
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


def test_conflict_receipt_without_quarantine_evidence_is_refused(tmp_path):
    store, _ = seeded(tmp_path)
    sql(store, "DELETE FROM trial_quarantine")
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


def test_blocked_receipt_without_valid_quarantine_marker_is_refused(tmp_path):
    store, _ = seeded(tmp_path)
    sql(store, "DELETE FROM trial_quarantined_senders")
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


def test_valid_receipts_stay_auditable_across_checkpoint_and_restart(tmp_path):
    store, path = seeded(tmp_path)
    expected = store.receipt_records()
    db = store._connect()
    try:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()

    reopened = TrialStore(path)
    assert reopened.receipt_records() == expected
    assert reopened.pool_snapshot()["runs"] == 1
    assert {x["outcome"] for x in reopened.non_pass_records()} == {"skipped"}


def test_receipt_retention_does_not_require_pruned_rows(tmp_path, monkeypatch):
    from avrone import trial_receiver

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", 3)
        store, path = seeded(tmp_path)
        retained = store.receipt_records()
        assert len(retained) == 3
        assert [r["result"] for r in retained] == [
            "non_pass_stored", "conflict_quarantined", "sender_quarantined"
        ]
        reopened = TrialStore(path)
        assert reopened.receipt_records() == retained
        assert reopened.pool_snapshot()["runs"] == 1
