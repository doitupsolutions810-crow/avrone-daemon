"""Round 206: evidence audits must use one consistent SQLite read snapshot.

In WAL mode another writer can commit while an audit is checking linked
receipts, quarantine conflict evidence and first-seen trial records. Separate
autocommit SELECTs can observe incompatible database generations; a deferred
read transaction pins all checks to one generation.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="206", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-206", run_id="run-206",
        check_id="consistent-read", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    assert store.ingest_many(
        [first], sender="peer-a", transport="fleet",
    )["accepted"] == 1
    assert store.ingest_many(
        [trial(outcome="fail")], sender="peer-b", transport="fleet",
    )["quarantined"] == 1
    return store, path, first


def external_sql(path, statement, args=()):
    db = sqlite3.connect(path, timeout=3.0, isolation_level=None)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        db.execute(statement, args)
    finally:
        db.close()


def test_quarantine_audit_uses_one_snapshot_while_original_is_modified(
    tmp_path, monkeypatch,
):
    store, path, original = seeded(tmp_path)
    first_read = store.quarantine_records()
    actual = store._assert_quarantine_conflict_semantics
    ran = []

    def change_backing_trial_mid_audit(db, row):
        if not ran:
            # This commit occurs *after* SELECT from trial_quarantine but
            # before the backing original trial is looked up.
            external_sql(
                path, "UPDATE trials SET outcome='fail' WHERE trial_id=?",
                (original["trial_id"],),
            )
            ran.append(True)
        return actual(db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_assert_quarantine_conflict_semantics",
                    change_backing_trial_mid_audit)
        audited = store.quarantine_records()
    assert ran == [True]
    assert audited == first_read

    # The next independent audit must see and refuse the now-invalid row.
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.quarantine_records()


def test_receipt_audit_keeps_first_seen_payload_stable_during_concurrent_write(
    tmp_path, monkeypatch,
):
    store, path, first = seeded(tmp_path)
    baseline = store.receipt_records()
    actual = store._verify_receipt_row
    fired = []

    def alter_trial_during_receipt_scan(db, row):
        if not fired:
            external_sql(
                path, "UPDATE trials SET outcome='fail' WHERE trial_id=?",
                (first["trial_id"],),
            )
            fired.append(True)
        return actual(db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_verify_receipt_row", alter_trial_during_receipt_scan)
        seen = store.receipt_records()
    assert fired == [True]
    assert seen == baseline
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


@pytest.mark.parametrize("reader", ["pool_snapshot", "non_pass_records"])
def test_trial_audit_excludes_newly_committed_attempt_until_next_snapshot(
    tmp_path, monkeypatch, reader,
):
    store, path, first = seeded(tmp_path)
    actual = store._verify_stored_trial
    fired = []

    def insert_new_attempt_mid_read(row):
        if not fired:
            accepted = store.ingest_many(
                [trial("later")], sender="peer-c", transport="fleet",
            )
            assert accepted["accepted"] == 1
            fired.append(True)
        return actual(row)

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_verify_stored_trial", insert_new_attempt_mid_read)
        result = getattr(store, reader)()

    assert fired == [True]
    if reader == "pool_snapshot":
        assert result["runs"] == 1
        assert store.pool_snapshot()["runs"] == 2
    else:
        assert result == []
        assert store.non_pass_records() == []
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_version_drift_after_connection_blocks_read_before_reporting(
    tmp_path, monkeypatch,
):
    store, path, _ = seeded(tmp_path)
    actual = store._connect
    opened = []

    def change_after_open():
        db = actual()
        db.execute("PRAGMA user_version=99")
        opened.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", change_after_open)
        with pytest.raises(TrialStoreError, match="schema version changed"):
            store.receipt_records()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    external_sql(path, "PRAGMA user_version=2")
    assert len(store.receipt_records()) == 2


def test_wal_drift_after_connection_blocks_quarantine_read(tmp_path, monkeypatch):
    store, path, _ = seeded(tmp_path)
    actual = store._connect
    opened = []

    def downgrade_after_open():
        db = actual()
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        opened.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", downgrade_after_open)
        with pytest.raises(TrialStoreError, match="WAL journal mode changed"):
            store.quarantine_records()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    finally:
        raw.close()
    assert len(store.quarantine_records()) == 1


def test_temp_trigger_injected_after_connect_blocks_audit_under_read_snapshot(
    tmp_path, monkeypatch,
):
    store, _, _ = seeded(tmp_path)
    actual = store._connect

    def add_temp_trigger_after_open():
        db = actual()
        db.execute(
            "CREATE TEMP TRIGGER rogue_after_open BEFORE INSERT ON main.trials "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", add_temp_trigger_after_open)
        with pytest.raises(TrialStoreError, match="unexpected evidence trigger"):
            store.pool_snapshot()
    assert store.pool_snapshot()["runs"] == 1


def test_read_transaction_is_closed_after_validation_failure(tmp_path):
    store, path, original = seeded(tmp_path)
    external_sql(
        path, "UPDATE trials SET outcome='fail' WHERE trial_id=?",
        (original["trial_id"],),
    )
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()
    # A failed read cannot hold a dangling SQLite snapshot or block DDL.
    external_sql(path, "CREATE TABLE clean_after_failure(label TEXT)")
    external_sql(path, "DROP TABLE clean_after_failure")


def test_audit_reads_begin_transaction_before_first_evidence_scan(
    tmp_path, monkeypatch,
):
    store, _, _ = seeded(tmp_path)
    actual = store._connect
    logged = []

    def inspect_sql():
        db = actual()
        db.set_trace_callback(logged.append)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", inspect_sql)
        assert store.pool_snapshot()["runs"] == 1
        assert len(store.quarantine_records()) == 1
        assert len(store.receipt_records()) == 2
        assert store.non_pass_records() == []

    statements = [s.strip().upper() for s in logged]
    assert statements.count("BEGIN") == 4
    for idx, statement in enumerate(statements):
        if "FROM TRIALS ORDER BY TRIAL_ID LIMIT" in statement:
            assert "BEGIN" in statements[:idx]
        if "FROM TRIAL_QUARANTINE" in statement:
            assert "BEGIN" in statements[:idx]
        if "FROM TRIAL_RECEIPTS ORDER BY RECEIPT_ID LIMIT" in statement:
            assert "BEGIN" in statements[:idx]
