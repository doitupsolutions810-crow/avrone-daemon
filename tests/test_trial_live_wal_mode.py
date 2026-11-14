"""Round 203: live receivers must not silently change journaling guarantees.

Trusted startup may enable WAL on an existing supported store, but subsequent
reads and writes must reject a competing writer's journal-mode drift. Check
again under BEGIN IMMEDIATE to close the connection-to-transaction interval.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="203", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-203", run_id="run-203",
        check_id="journal-mode", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    assert store.ingest_many(
        [first], sender="peer-a", transport="fleet"
    )["accepted"] == 1
    assert store.ingest_many(
        [trial(outcome="fail")], sender="peer-b", transport="fleet"
    )["quarantined"] == 1
    return store, path, first


def mode(path, change=None):
    db = sqlite3.connect(path, isolation_level=None)
    try:
        statement = "PRAGMA journal_mode" if change is None else "PRAGMA journal_mode=" + change
        return db.execute(statement).fetchone()[0].lower()
    finally:
        db.close()


def counts(path):
    db = sqlite3.connect(path)
    try:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "trials", "trial_quarantine",
                "trial_quarantined_senders", "trial_receipts",
            )
        )
    finally:
        db.close()


@pytest.mark.parametrize("bad_mode", ["delete", "truncate", "persist", "memory"])
def test_live_journal_mode_drift_denies_every_audit_and_ingest_then_recovers(
    tmp_path, bad_mode,
):
    store, path, original = seeded(tmp_path)
    before = counts(path)
    assert mode(path) == "wal"
    assert mode(path, bad_mode) == bad_mode

    for read in (
        store.pool_snapshot, store.non_pass_records,
        store.quarantine_records, store.receipt_records,
    ):
        with pytest.raises(TrialStoreError, match="WAL journal mode changed"):
            read()
    with pytest.raises(TrialStoreError, match="WAL journal mode changed"):
        store.ingest_many(
            [trial("new")], sender="peer-c", transport="fleet"
        )
    with pytest.raises(TrialStoreError, match="WAL journal mode changed"):
        store.ingest_many(
            [original], sender="peer-c", transport="fleet"
        )

    assert counts(path) == before
    # SQLite persists only WAL vs rollback-journal selection across new
    # connections; TRUNCATE, PERSIST, and MEMORY reopen in DELETE mode.
    assert mode(path) == "delete"
    assert mode(path, "wal") == "wal"
    assert store.pool_snapshot()["runs"] == 1
    assert store.ingest_many(
        [trial("new")], sender="peer-c", transport="fleet"
    )["accepted"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_wal_change_between_connection_and_begin_is_denied_without_receipt(
    tmp_path, monkeypatch,
):
    store, path, _ = seeded(tmp_path)
    original = store._connect
    opened = []
    before = counts(path)

    def swapped_mode_after_connect():
        db = original()
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        opened.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", swapped_mode_after_connect)
        with pytest.raises(TrialStoreError, match="WAL journal mode changed"):
            store.ingest_many(
                [trial("post-connection")], sender="peer-c", transport="fleet",
            )

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert counts(path) == before
    assert mode(path) == "delete"
    assert mode(path, "wal") == "wal"
    assert store.ingest_many(
        [trial("post-connection")], sender="peer-c", transport="fleet",
    )["accepted"] == 1


def test_competing_journal_ddl_cannot_run_under_sqlite_writer_lock(tmp_path):
    store, path, _ = seeded(tmp_path)
    db = store._connect()
    intruder = sqlite3.connect(path, timeout=0.02, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        store._assert_live_wal_mode(db)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            intruder.execute("PRAGMA journal_mode=DELETE")
        db.commit()
    finally:
        db.close()
        intruder.close()
    assert mode(path) == "wal"
    assert store.ingest_many(
        [trial("after-lock")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_fresh_receiver_can_restore_supported_database_nonwal_mode(tmp_path):
    store, path, _ = seeded(tmp_path)
    original_snapshot = store.pool_snapshot()
    assert mode(path, "delete") == "delete"
    with pytest.raises(TrialStoreError, match="WAL journal mode changed"):
        store.pool_snapshot()
    reopened = TrialStore(path)
    assert mode(path) == "wal"
    assert reopened.pool_snapshot() == original_snapshot
    assert reopened.ingest_many(
        [trial("restart")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_healthy_wal_checkpoint_and_idempotent_replay_after_restart(tmp_path):
    store, path, original = seeded(tmp_path)
    db = store._connect()
    try:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
    reopened = TrialStore(path)
    assert reopened.receipt_records() == store.receipt_records()
    assert reopened.ingest_many(
        [original], sender="peer-c", transport="fleet"
    )["duplicates"] == 1
    assert reopened.pool_snapshot()["runs"] == 1
