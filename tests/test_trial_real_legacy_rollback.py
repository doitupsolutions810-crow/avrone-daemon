"""Round 208: migration must be atomic against genuinely legacy-shaped schemas.

Unlike a PRAGMA user_version-only downgrade, these tests remove the actual
source_identity_key column, its index and both triggers while preserving the
trial_id PRIMARY KEY. A failure after one valid row was backfilled must roll
back the ALTER TABLE and all updates, with the original evidence recoverable.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from avrone.trial_contract import MAX_ENVELOPE_BYTES, make_trial
from avrone.trial_receiver import (
    DB_SCHEMA_VERSION,
    TrialStore,
    TrialStoreError,
)


def trial(attempt):
    return make_trial(
        system="cyclekernel", node="node-208", run_id="run-208",
        check_id="real-legacy", check_version="v2",
        attempt_id=attempt, outcome="pass",
    )


def seed(tmp_path, count=3):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    for number in range(count):
        result = store.ingest_many(
            [trial(f"legacy-{number}")], sender="peer-a", transport="fleet"
        )
        assert result["accepted"] == 1
    return store, path


def make_real_legacy(store):
    db = store._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP TRIGGER IF EXISTS trial_source_identity_key_required_insert")
        db.execute("DROP TRIGGER IF EXISTS trial_source_identity_key_required_update")
        db.execute("DROP INDEX trial_source_identity_key")
        db.execute("ALTER TABLE trials DROP COLUMN source_identity_key")
        db.execute("PRAGMA user_version=1")
        db.commit()
    finally:
        db.close()


def inspect(path):
    db = sqlite3.connect(path)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(trials)")}
        return {
            "version": db.execute("PRAGMA user_version").fetchone()[0],
            "columns": columns,
            "trials": db.execute("SELECT COUNT(*) FROM trials").fetchone()[0],
            "receipts": db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0],
            "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        db.close()


def corrupt_last(path, raw):
    db = sqlite3.connect(path)
    try:
        row = db.execute(
            "SELECT trial_id,canonical_json FROM trials ORDER BY trial_id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        db.execute(
            "UPDATE trials SET canonical_json=? WHERE trial_id=?",
            (sqlite3.Binary(raw), row[0]),
        )
        db.commit()
        return row
    finally:
        db.close()


@pytest.mark.parametrize("bad", [b"not-json", b"\xff", b"{}"])
def test_late_corrupt_canonical_bytes_roll_back_column_add_and_prior_backfill(
    tmp_path, bad,
):
    store, path = seed(tmp_path)
    make_real_legacy(store)
    corrupt_last(path, bad)
    before = inspect(path)
    assert before["version"] == 1
    assert "source_identity_key" not in before["columns"]
    assert before["integrity"] == "ok"

    with pytest.raises(TrialStoreError, match="cannot be validated"):
        TrialStore(path)

    assert inspect(path) == before


def test_late_oversized_sqlite_blob_rolls_back_all_migration_changes(tmp_path):
    store, path = seed(tmp_path)
    make_real_legacy(store)
    db = sqlite3.connect(path)
    try:
        last_id = db.execute(
            "SELECT trial_id FROM trials ORDER BY trial_id DESC LIMIT 1"
        ).fetchone()[0]
        db.execute(
            "UPDATE trials SET canonical_json=zeroblob(?) WHERE trial_id=?",
            (MAX_ENVELOPE_BYTES + 1, last_id),
        )
        db.commit()
    finally:
        db.close()
    before = inspect(path)
    with pytest.raises(TrialStoreError, match="cannot be validated"):
        TrialStore(path)
    assert inspect(path) == before


def test_capacity_failure_after_first_legacy_row_rolls_back_schema(tmp_path, monkeypatch):
    store, path = seed(tmp_path, count=3)
    make_real_legacy(store)
    before = inspect(path)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", 1)
        with pytest.raises(TrialStoreError, match="migration capacity"):
            TrialStore(path)
    assert inspect(path) == before


def test_restore_late_evidence_then_complete_trusted_backfill_and_replay(tmp_path):
    store, path = seed(tmp_path)
    make_real_legacy(store)
    original_id, original_bytes = corrupt_last(path, b"corrupt")
    with pytest.raises(TrialStoreError, match="cannot be validated"):
        TrialStore(path)
    assert "source_identity_key" not in inspect(path)["columns"]
    db = sqlite3.connect(path)
    try:
        db.execute(
            "UPDATE trials SET canonical_json=? WHERE trial_id=?",
            (sqlite3.Binary(original_bytes), original_id),
        )
        db.commit()
    finally:
        db.close()

    migrated = TrialStore(path)
    after = inspect(path)
    assert after["version"] == DB_SCHEMA_VERSION
    assert "source_identity_key" in after["columns"]
    assert after["trials"] == after["receipts"] == 3
    assert after["integrity"] == "ok"
    db = migrated._connect()
    try:
        keys = [row["source_identity_key"] for row in db.execute(
            "SELECT source_identity_key FROM trials"
        )]
        assert len(set(keys)) == 3
        assert all(isinstance(k, str) and len(k) == 64 for k in keys)
    finally:
        db.close()
    assert migrated.pool_snapshot()["runs"] == 3
    assert migrated.ingest_many(
        [trial("legacy-0")], sender="peer-b", transport="fleet"
    )["duplicates"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 3


def test_empty_legacy_column_backfill_does_not_create_spurious_rows(tmp_path):
    store, path = seed(tmp_path, count=0)
    make_real_legacy(store)
    assert inspect(path)["version"] == 1
    migrated = TrialStore(path)
    assert migrated.pool_snapshot()["runs"] == 0
    assert migrated.receipt_records() == []
    assert inspect(path)["trials"] == inspect(path)["receipts"] == 0
    assert inspect(path)["version"] == DB_SCHEMA_VERSION


def test_real_legacy_schema_rebuild_is_idempotent(tmp_path):
    store, path = seed(tmp_path, count=2)
    make_real_legacy(store)
    first = TrialStore(path)
    assert first.pool_snapshot()["runs"] == 2
    state = inspect(path)
    second = TrialStore(path)
    assert second.pool_snapshot() == first.pool_snapshot()
    assert inspect(path) == state
