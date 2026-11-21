"""Round 218: query-only audit connections deny all SQL bypass attempts.

SQLite PRAGMA query_only by itself can be turned OFF by a helper with access
to the same connection. The SQLite authorizer remains active after BEGIN and
forbids that PRAGMA, DML, DDL, ATTACH, SAVEPOINT and COMMIT/ROLLBACK. SELECT
and audit provenance lookups remain functional while independent writers
continue committing to WAL.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="218", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-218", run_id="run-218",
        check_id="sql-authorizer", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seed(tmp_path):
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
    db = store._connect()
    try:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "trials", "trial_receipts",
                "trial_quarantine", "trial_quarantined_senders",
            )
        )
    finally:
        db.close()


DENIED_SQL = [
    pytest.param("PRAGMA query_only=OFF", id="disable-readonly"),
    pytest.param("PRAGMA writable_schema=ON", id="enable-schema-rewrite"),
    pytest.param("PRAGMA journal_mode=DELETE", id="change-journal-mode"),
    pytest.param("INSERT INTO trial_receipts("
                 "trial_id,sender,transport,result,payload_sha256,received_at)"
                 " VALUES('forged','peer-x','fleet','accepted','fake',0)",
                 id="insert-forged-receipt"),
    pytest.param("UPDATE trials SET outcome='fail'", id="change-trial-outcome"),
    pytest.param("DELETE FROM trial_quarantined_senders", id="delete-quarantine"),
    pytest.param("CREATE TABLE injected_audit_table(x)", id="create-table"),
    pytest.param("DROP TABLE trial_quarantine", id="drop-quarantine-table"),
    pytest.param("ATTACH DATABASE ':memory:' AS injected_db", id="attach-db"),
    pytest.param("SAVEPOINT injected_point", id="savepoint"),
    pytest.param("COMMIT", id="end-snapshot"),
    pytest.param("ROLLBACK", id="restart-snapshot"),
    pytest.param("SELECT load_extension('untrusted-extension')", id="dangerous-function"),
]


def verify_sql_is_denied(db, statement):
    assert db.in_transaction
    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 2
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(statement)
    assert db.in_transaction
    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 2


@pytest.mark.parametrize("statement", DENIED_SQL)
@pytest.mark.parametrize(
    "reader",
    ["receipt_records", "quarantine_records", "pool_snapshot", "non_pass_records"],
)
def test_audit_authorizer_denies_query_only_bypass_and_mutating_sql(
    tmp_path, monkeypatch, statement, reader,
):
    store, path = seed(tmp_path)
    expected = getattr(store, reader)()
    before = counts(store)
    observations = []
    receipt_hook = TrialStore._verify_receipt_row
    quarantine_hook = TrialStore._assert_quarantine_conflict_semantics
    trial_hook = TrialStore._verified_trial_rows

    if reader == "receipt_records":
        def hook(self, db, row):
            if not observations:
                verify_sql_is_denied(db, statement)
                observations.append(True)
            return receipt_hook(self, db, row)
        method = "_verify_receipt_row"
    elif reader == "quarantine_records":
        def hook(self, db, row):
            if not observations:
                verify_sql_is_denied(db, statement)
                observations.append(True)
            return quarantine_hook(self, db, row)
        method = "_assert_quarantine_conflict_semantics"
    else:
        def hook(self, db):
            if not observations:
                verify_sql_is_denied(db, statement)
                observations.append(True)
            return trial_hook(self, db)
        method = "_verified_trial_rows"

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, method, hook)
        assert getattr(store, reader)() == expected

    assert observations == [True]
    assert counts(store) == before
    assert getattr(TrialStore(path), reader)() == expected


@pytest.mark.parametrize("reader", [
    "receipt_records", "quarantine_records", "pool_snapshot", "non_pass_records",
])
def test_sql_authorizer_does_not_block_clean_reads_or_writer_after_audit(
    tmp_path, reader,
):
    store, path = seed(tmp_path)
    expected = getattr(store, reader)()
    assert getattr(TrialStore(path), reader)() == expected
    writer = TrialStore(path)
    assert writer.ingest_many(
        [trial("fresh")], sender="peer-c", transport="fleet"
    )["accepted"] == 1
    assert len(store.receipt_records()) == 4
    assert store.pool_snapshot()["runs"] == 2


@pytest.mark.parametrize("reader", ["receipt_records", "quarantine_records"])
def test_authorizer_denied_commit_does_not_leak_connection_after_failure(
    tmp_path, monkeypatch, reader,
):
    store, path = seed(tmp_path)
    connections = []
    real_connect = store._connect
    real_receipt = TrialStore._verify_receipt_row
    real_conflict = TrialStore._assert_quarantine_conflict_semantics

    def tracked_connection():
        db = real_connect()
        connections.append(db)
        return db

    def interrupted_receipt(self, db, row):
        verify_sql_is_denied(db, "COMMIT")
        raise RuntimeError("stop after rejecting commit")

    def interrupted_conflict(self, db, row):
        verify_sql_is_denied(db, "ROLLBACK")
        raise RuntimeError("stop after rejecting rollback")

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", tracked_connection)
        ctx.setattr(
            TrialStore,
            "_verify_receipt_row" if reader == "receipt_records"
            else "_assert_quarantine_conflict_semantics",
            interrupted_receipt if reader == "receipt_records"
            else interrupted_conflict,
        )
        with pytest.raises(RuntimeError, match="stop after rejecting"):
            getattr(store, reader)()

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert TrialStore(path).ingest_many(
        [trial("next")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_authorizer_denial_does_not_leak_to_new_sqlite_connections(tmp_path):
    store, path = seed(tmp_path)
    store.receipt_records()
    db = store._connect()
    try:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        db.execute("CREATE TEMP TABLE benign_tmp(x)")
        db.execute("INSERT INTO benign_tmp VALUES(1)")
        assert db.execute("SELECT * FROM benign_tmp").fetchone()[0] == 1
    finally:
        db.close()
    assert TrialStore(path).pool_snapshot()["runs"] == 1
