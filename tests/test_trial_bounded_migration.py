"""Round 206: bounded legacy schema migration with no partial evidence commits.

A legacy v1 receiver can contain a large trial corpus. The v2 source-identity
backfill must not fetch all canonical BLOBs in one Python list, must reject
oversized records, and must roll back every update on over-capacity failure.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import MAX_ENVELOPE_BYTES, make_trial
from avrone.trial_receiver import DB_SCHEMA_VERSION, TrialStore, TrialStoreError


def trial(attempt="206", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-206",
        check_id="migration", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path, n=2):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    for i in range(n):
        assert store.ingest_many(
            [trial(f"trial-{i}")], sender="peer-a", transport="fleet"
        )["accepted"] == 1
    downgrade(store)
    return store, path


def downgrade(store):
    db = store._connect()
    try:
        db.execute("PRAGMA user_version=1")
        db.commit()
    finally:
        db.close()


def version_and_counts(path):
    db = sqlite3.connect(path)
    try:
        return (
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM trials").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0],
        )
    finally:
        db.close()


@pytest.mark.parametrize("limit", [0, 1])
def test_legacy_migration_over_capacity_rejects_without_partial_upgrade(
    tmp_path, monkeypatch, limit,
):
    store, path = seeded(tmp_path)
    before = version_and_counts(path)
    from avrone import trial_receiver

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", limit)
        with pytest.raises(TrialStoreError, match="migration capacity"):
            TrialStore(path)

    assert version_and_counts(path) == before == (1, 2, 2)
    restarted = TrialStore(path)
    assert version_and_counts(path)[0] == DB_SCHEMA_VERSION
    assert restarted.pool_snapshot()["runs"] == 2
    assert restarted.receipt_records() == store.receipt_records()


def test_migration_exactly_at_capacity_succeeds_and_preserves_replay(tmp_path, monkeypatch):
    store, path = seeded(tmp_path)
    from avrone import trial_receiver

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", 2)
        reopened = TrialStore(path)
        assert reopened.pool_snapshot()["runs"] == 2
        assert reopened.ingest_many(
            [trial("trial-0")], sender="peer-b", transport="fleet"
        )["duplicates"] == 1
    assert version_and_counts(path) == (DB_SCHEMA_VERSION, 2, 3)


def test_oversized_legacy_blob_fails_before_migration_commits(tmp_path):
    store, path = seeded(tmp_path, n=1)
    db = store._connect()
    try:
        db.execute(
            "UPDATE trials SET canonical_json=zeroblob(?)",
            (MAX_ENVELOPE_BYTES + 1,),
        )
        db.commit()
    finally:
        db.close()

    assert version_and_counts(path) == (1, 1, 1)
    with pytest.raises(TrialStoreError, match="cannot be validated"):
        TrialStore(path)
    assert version_and_counts(path) == (1, 1, 1)


@pytest.mark.parametrize(
    "bad_data",
    [
        pytest.param(b"not-json", id="not-json"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"{}", id="missing-envelope"),
    ],
)
def test_malformed_legacy_canonical_bytes_rejected_without_version_change(
    tmp_path, bad_data,
):
    store, path = seeded(tmp_path, n=1)
    db = store._connect()
    try:
        db.execute(
            "UPDATE trials SET canonical_json=?",
            (sqlite3.Binary(bad_data),),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="cannot be validated"):
        TrialStore(path)
    assert version_and_counts(path) == (1, 1, 1)


def test_legacy_migration_preserves_conflict_and_original_receipts(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    good = trial()
    conflicting = trial(outcome="fail")
    assert store.ingest_many([good], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([conflicting], sender="peer-b", transport="fleet")["quarantined"] == 1
    expected_quarantine = store.quarantine_records()
    expected_receipts = store.receipt_records()
    downgrade(store)

    reopened = TrialStore(path)
    assert reopened.quarantine_records() == expected_quarantine
    assert reopened.receipt_records() == expected_receipts
    assert reopened.pool_snapshot()["runs"] == 1
    assert reopened.ingest_many(
        [trial("later")], sender="peer-b", transport="fleet"
    )["results"][0]["status"] == "sender_quarantined"
    assert version_and_counts(path)[0] == DB_SCHEMA_VERSION


def test_empty_legacy_database_migrates_without_false_capacity_exhaustion(
    tmp_path, monkeypatch,
):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    downgrade(store)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", 0)
        reopened = TrialStore(path)
        assert reopened.pool_snapshot()["runs"] == 0
    assert version_and_counts(path) == (DB_SCHEMA_VERSION, 0, 0)
