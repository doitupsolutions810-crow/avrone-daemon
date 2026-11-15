"""Round 217: audit handles cannot accidentally mutate persisted trial history.

A deferred BEGIN pins a consistent WAL snapshot, but SQLite permits a read
transaction to become a writer. Explicit PRAGMA query_only=ON before the first
audit query prevents future helper code from modifying evidence through the
audit connection, while separate writers continue to accept valid trials.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="217", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-217", run_id="run-217",
        check_id="readonly-audit", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    assert store.ingest_many([trial()], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([trial("skip", "skipped")], sender="peer-a",
                             transport="fleet")["non_pass"] == 1
    assert store.ingest_many([trial(outcome="fail")], sender="peer-b",
                             transport="fleet")["quarantined"] == 1
    assert len(store.quarantine_records()) == 1
    return store, path


def counts(store):
    conn = store._connect()
    try:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("trials", "trial_receipts",
                                   "trial_quarantine", "trial_quarantined_senders"))
    finally:
        conn.close()


def assert_read_only(conn, operation):
    assert conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    statements = {
        "insert": ("INSERT INTO trial_receipts("
                   "trial_id,sender,transport,result,payload_sha256,received_at)"
                   " VALUES('malicious','peer-x','fleet','accepted','fake',0)", ()),
        "update": ("UPDATE trials SET outcome='fail'", ()),
        "delete": ("DELETE FROM trial_quarantined_senders", ()),
    }
    sql, args = statements[operation]
    with pytest.raises(sqlite3.OperationalError, match="readonly|read.only"):
        conn.execute(sql, args)


@pytest.mark.parametrize("reader", [
    "receipt_records", "quarantine_records", "pool_snapshot", "non_pass_records",
])
@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
def test_each_public_audit_rejects_accidental_writes_on_read_connection(
    tmp_path, monkeypatch, reader, operation,
):
    store, path = seeded(tmp_path)
    expected = getattr(store, reader)()
    before = counts(store)
    observed = []
    original_receipt = TrialStore._verify_receipt_row
    original_conflict = TrialStore._assert_quarantine_conflict_semantics
    original_trial_rows = TrialStore._verified_trial_rows

    if reader == "receipt_records":
        def receipt_probe(self, db, row):
            if not observed:
                assert_read_only(db, operation)
                observed.append(True)
            return original_receipt(self, db, row)

        method = "_verify_receipt_row"
        replacement = receipt_probe
    elif reader == "quarantine_records":
        def quarantine_probe(self, db, row):
            if not observed:
                assert_read_only(db, operation)
                observed.append(True)
            return original_conflict(self, db, row)

        method = "_assert_quarantine_conflict_semantics"
        replacement = quarantine_probe
    else:
        def trial_probe(self, db):
            if not observed:
                assert_read_only(db, operation)
                observed.append(True)
            return original_trial_rows(self, db)

        method = "_verified_trial_rows"
        replacement = trial_probe

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, method, replacement)
        assert getattr(store, reader)() == expected

    assert observed == [True]
    assert counts(store) == before
    assert getattr(TrialStore(path), reader)() == expected


def test_audit_query_only_mode_does_not_affect_new_writable_connections(
    tmp_path,
):
    store, path = seeded(tmp_path)
    store.receipt_records()
    store.quarantine_records()
    store.pool_snapshot()
    store.non_pass_records()
    db = store._connect()
    try:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert not db.in_transaction
    finally:
        db.close()

    writer = TrialStore(path)
    assert writer.ingest_many([trial("new")], sender="peer-c",
                              transport="fleet")["accepted"] == 1
    assert len(store.receipt_records()) == 4
    assert store.pool_snapshot()["runs"] == 2


def test_readonly_audit_rejects_write_and_closes_connection_after_probe_failure(
    tmp_path, monkeypatch,
):
    store, path = seeded(tmp_path)
    real_connect = store._connect
    observed_connections = []
    original = TrialStore._verify_receipt_row

    def capture_connection():
        db = real_connect()
        observed_connections.append(db)
        return db

    def deny_and_raise(self, db, row):
        assert_read_only(db, "update")
        raise RuntimeError("deliberate audit interruption")

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", capture_connection)
        ctx.setattr(TrialStore, "_verify_receipt_row", deny_and_raise)
        with pytest.raises(RuntimeError, match="deliberate audit interruption"):
            store.receipt_records()

    assert len(observed_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed_connections[0].execute("SELECT 1")
    assert TrialStore(path).ingest_many(
        [trial("after-interruption")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_readonly_receipt_audit_stays_on_original_snapshot_while_writer_commits(
    tmp_path, monkeypatch,
):
    store, path = seeded(tmp_path)
    writer = TrialStore(path)
    expected = store.receipt_records()
    original = TrialStore._verify_receipt_row
    observed = []

    def inject_write_on_independent_connection(self, db, row):
        assert_read_only(db, "insert")
        if not observed:
            observed.append(True)
            assert writer.ingest_many(
                [trial("concurrent")], sender="peer-c", transport="fleet"
            )["accepted"] == 1
        return original(self, db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_verify_receipt_row",
                    inject_write_on_independent_connection)
        assert store.receipt_records() == expected

    assert observed == [True]
    assert len(store.receipt_records()) == len(expected) + 1
    assert TrialStore(path).pool_snapshot()["runs"] == 2
