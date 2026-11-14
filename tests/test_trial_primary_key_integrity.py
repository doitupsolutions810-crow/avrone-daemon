"""Round 202: same-name SQLite tables must preserve their primary-key contract.

CREATE TABLE IF NOT EXISTS and a column-name inventory do not verify PK
constraints. A locally rebuilt table with identical columns but no PK can
permit duplicate receipt IDs or trial IDs. Refuse weakened tables at startup,
during ingestion, and in streaming evidence audits.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


TABLES = (
    ("trials", "trial_id"),
    ("trial_receipts", "receipt_id"),
    ("trial_quarantine", "quarantine_id"),
    ("trial_quarantined_senders", "sender"),
)


def envelope(attempt="r202", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-202",
        check_id="primary-keys", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = envelope()
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([envelope(outcome="fail")], sender="peer-b",
                             transport="fleet")["quarantined"] == 1
    assert store.quarantine_records()[0]["sender"] == "peer-b"
    return store, path, first


def drop_pk_keep_columns(store, table):
    assert table in {t for t, _ in TABLES}
    db = store._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        # A CTAS clone has the same data and column names, but no primary key,
        # unique indexes, not-null constraints, or validation triggers.
        db.execute(f"CREATE TABLE replacement_{table} AS SELECT * FROM {table}")
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE replacement_{table} RENAME TO {table}")
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("table,pk", TABLES)
def test_running_receiver_refuses_recreated_table_without_primary_key(
    tmp_path, table, pk,
):
    store, path, first = seeded(tmp_path)
    drop_pk_keep_columns(store, table)
    db = store._connect()
    try:
        assert any(row["name"] == pk for row in db.execute(
            f"PRAGMA table_info({table})"
        ))
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="primary key integrity"):
        store.ingest_many([envelope("new")], sender="peer-c", transport="fleet")
    with pytest.raises(TrialStoreError, match="primary key integrity"):
        TrialStore(path)


def test_duplicate_trial_id_cannot_enter_pool_after_primary_key_is_removed(tmp_path):
    store, path, first = seeded(tmp_path)
    drop_pk_keep_columns(store, "trials")
    db = store._connect()
    try:
        # Also remove source uniqueness to emulate a corrupted or substituted
        # schema that permits two individually correct copies of one trial.
        db.execute(
            "INSERT INTO trials SELECT * FROM trials WHERE trial_id=?",
            (first["trial_id"],),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="duplicate trial identity"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="duplicate trial identity"):
        store.non_pass_records()
    with pytest.raises(TrialStoreError, match="primary key integrity"):
        TrialStore(path)


def test_duplicate_receipt_id_is_never_reported_as_two_distinct_events(tmp_path):
    store, _, first = seeded(tmp_path)
    drop_pk_keep_columns(store, "trial_receipts")
    db = store._connect()
    try:
        db.execute(
            "INSERT INTO trial_receipts SELECT * FROM trial_receipts "
            "WHERE receipt_id=(SELECT MIN(receipt_id) FROM trial_receipts)"
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="duplicate receipt identity"):
        store.receipt_records()


def test_correct_primary_keys_survive_restart_and_idempotent_replay(tmp_path):
    store, path, first = seeded(tmp_path)
    db = store._connect()
    try:
        store._assert_primary_key_schema(db)
        for table, key in TABLES:
            info = list(db.execute(f"PRAGMA table_info({table})"))
            pk = [x for x in info if x["pk"]]
            assert len(pk) == 1 and pk[0]["name"] == key
    finally:
        db.close()
    reopened = TrialStore(path)
    assert reopened.ingest_many([first], sender="peer-c",
                                transport="fleet")["duplicates"] == 1
    assert reopened.pool_snapshot()["runs"] == 1
    assert len(reopened.quarantine_records()) == 1


def test_noop_missing_primary_key_detection_does_not_mutate_valid_database(tmp_path):
    store, path, first = seeded(tmp_path)
    db = store._connect()
    try:
        old_count = db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0]
    finally:
        db.close()
    assert old_count == 2
    assert len(store.receipt_records()) == old_count
    assert store.pool_snapshot()["runs"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 1
