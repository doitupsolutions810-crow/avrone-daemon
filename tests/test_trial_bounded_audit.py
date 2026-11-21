"""Round 197: evidence audits must stay bounded even when SQLite is over capacity.

A maliciously enlarged or damaged local store must be rejected rather than
silently truncated. Validation streams one canonical record at a time, keeps
only compact report fields, and refuses partial results after later corruption.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="197", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-197", run_id="run-197",
        check_id="capacity", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def store_with_trials(tmp_path, n=3):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    envelopes = [trial(f"row-{i}", "skipped" if i % 2 else "pass") for i in range(n)]
    for envelope in envelopes:
        result = store.ingest_many([envelope], sender="peer-a", transport="fleet")
        assert result["accepted"] + result["non_pass"] == 1
    return store, path, envelopes


def store_with_quarantines(tmp_path):
    store, path, _ = store_with_trials(tmp_path, 2)
    for i, sender in enumerate(("peer-b", "peer-c")):
        response = store.ingest_many(
            [trial(f"row-{i}", "fail")], sender=sender, transport="fleet"
        )
        assert response["quarantined"] == 1
    assert len(store.quarantine_records()) == 2
    return store, path


def read_only_counts(store):
    db = store._connect()
    try:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in (
            "trials", "trial_receipts", "trial_quarantine", "trial_quarantined_senders"
        ))
    finally:
        db.close()


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_oversize_trial_pool_fails_closed_without_truncation(tmp_path, monkeypatch, limit):
    store, path, _ = store_with_trials(tmp_path)
    before = read_only_counts(store)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", limit)
        with pytest.raises(TrialStoreError, match="trial count exceeds"):
            store.pool_snapshot()
        with pytest.raises(TrialStoreError, match="trial count exceeds"):
            store.non_pass_records()
    assert read_only_counts(store) == before
    assert store.pool_snapshot()["runs"] == 2
    assert TrialStore(path).pool_snapshot()["runs"] == 2


@pytest.mark.parametrize("limit", [0, 1])
def test_oversize_quarantine_audit_fails_closed_without_partial_report(
    tmp_path, monkeypatch, limit,
):
    store, path = store_with_quarantines(tmp_path)
    original = store.quarantine_records()
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_QUARANTINE_RECORDS", limit)
        with pytest.raises(TrialStoreError, match="quarantine count exceeds"):
            store.quarantine_records()
    assert TrialStore(path).quarantine_records() == original


def test_oversize_marker_set_is_rejected_without_hiding_extra_sender(tmp_path, monkeypatch):
    store, _ = store_with_quarantines(tmp_path)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_QUARANTINED_SENDERS", 1)
        with pytest.raises(TrialStoreError, match="quarantined sender count exceeds"):
            store.quarantine_records()
    assert len(store.quarantine_records()) == 2


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_oversize_receipt_audit_fails_closed_without_pruning(tmp_path, monkeypatch, limit):
    store, path, _ = store_with_trials(tmp_path, 3)
    assert len(store.receipt_records()) == 3
    before = read_only_counts(store)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", limit)
        with pytest.raises(TrialStoreError, match="receipt count exceeds"):
            store.receipt_records()
    assert read_only_counts(store) == before
    assert len(TrialStore(path).receipt_records()) == 3


def test_exact_capacity_boundary_succeeds_without_false_positive(tmp_path, monkeypatch):
    store, _, _ = store_with_trials(tmp_path, 2)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", 2)
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", 2)
        assert store.pool_snapshot()["runs"] == 1
        assert len(store.non_pass_records()) == 1
        assert len(store.receipt_records()) == 2


def test_exact_quarantine_and_sender_capacity_boundary_succeeds(tmp_path, monkeypatch):
    store, _, = store_with_quarantines(tmp_path)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_QUARANTINE_RECORDS", 2)
        ctx.setattr(trial_receiver, "MAX_QUARANTINED_SENDERS", 2)
        assert len(store.quarantine_records()) == 2


def test_verified_trial_projections_discard_canonical_json_blob(tmp_path):
    store, _, records = store_with_trials(tmp_path, 3)
    db = store._connect()
    try:
        rows = store._verified_trial_rows(db)
    finally:
        db.close()
    assert len(rows) == 3
    assert all(isinstance(row, dict) for row in rows)
    assert all("canonical_json" not in row and "payload_sha256" not in row for row in rows)
    assert [row["trial_id"] for row in rows] == sorted(r["trial_id"] for r in records)


def test_later_corrupt_trial_does_not_leak_partially_valid_report(tmp_path):
    store, _, records = store_with_trials(tmp_path, 3)
    db = store._connect()
    try:
        last_id = sorted(r["trial_id"] for r in records)[-1]
        db.execute("UPDATE trials SET outcome='fail' WHERE trial_id=?", (last_id,))
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.non_pass_records()


def test_later_corrupt_receipt_prevents_partial_audit(tmp_path):
    store, _, _ = store_with_trials(tmp_path, 3)
    db = store._connect()
    try:
        db.execute(
            "UPDATE trial_receipts SET payload_sha256='bad' "
            "WHERE receipt_id=(SELECT MAX(receipt_id) FROM trial_receipts)"
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


def test_later_corrupt_quarantine_prevents_partial_audit(tmp_path):
    store, _ = store_with_quarantines(tmp_path)
    db = store._connect()
    try:
        db.execute(
            "UPDATE trial_quarantine SET incoming_sha256='bad' "
            "WHERE quarantine_id=(SELECT MAX(quarantine_id) FROM trial_quarantine)"
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()


def test_each_audit_query_uses_bounded_sentinel_limit(tmp_path):
    store, _ = store_with_quarantines(tmp_path)
    statements = []
    db = store._connect()
    try:
        db.set_trace_callback(statements.append)
        store._verified_trial_rows(db)
        assert len(store._verified_trial_rows(db)) == 2
    finally:
        db.close()
    # The public APIs make their own SQLite connections; query statements on
    # this instrumented handle show a bound LIMIT max+1 for trial BLOB scans.
    assert any("FROM trials ORDER BY trial_id LIMIT" in x for x in statements)
    assert len(store.receipt_records()) == 4
    assert len(store.quarantine_records()) == 2
