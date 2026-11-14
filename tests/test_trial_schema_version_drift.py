"""Round 202: enforce the live SQLite schema version on every read and write.

Initialization may migrate supported old schemas. Once a receiver has
initialized, unexpected user_version changes must never be trusted, including
changes injected after the connection-level version check but before BEGIN
IMMEDIATE. No automatic downgrade or silent evidence-history replacement.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import DB_SCHEMA_VERSION, TrialStore, TrialStoreError


def trial(attempt="r202", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-202", run_id="run-202",
        check_id="schema-version", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many(
        [trial(outcome="fail")], sender="peer-b", transport="fleet"
    )["quarantined"] == 1
    assert DB_SCHEMA_VERSION == 2
    return store, path, first


def version(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()


def rewrite_version(path, new_version):
    db = sqlite3.connect(path)
    try:
        db.execute(f"PRAGMA user_version={new_version}")
        db.commit()
    finally:
        db.close()


def counts(path):
    db = sqlite3.connect(path)
    try:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "trials", "trial_quarantine", "trial_quarantined_senders",
                "trial_receipts",
            )
        )
    finally:
        db.close()


@pytest.mark.parametrize(
    "altered_version", [
        pytest.param(0, id="legacy-zero"),
        pytest.param(1, id="legacy-one"),
        pytest.param(3, id="next-unsupported"),
        pytest.param(99, id="future-schema"),
        pytest.param(-1, id="invalid-negative"),
    ],
)
def test_live_version_drift_denies_all_audit_and_ingest_paths_then_recovers(
    tmp_path, altered_version,
):
    store, path, first = seeded(tmp_path)
    before = counts(path)
    rewrite_version(path, altered_version)

    for reader in (
        store.pool_snapshot, store.non_pass_records,
        store.quarantine_records, store.receipt_records,
    ):
        with pytest.raises(TrialStoreError, match="schema version changed"):
            reader()
    with pytest.raises(TrialStoreError, match="schema version changed"):
        store.ingest_many([trial("new")], sender="peer-c", transport="fleet")
    with pytest.raises(TrialStoreError, match="schema version changed"):
        store.ingest_many([first], sender="peer-c", transport="fleet")

    assert version(path) == altered_version
    assert counts(path) == before
    rewrite_version(path, DB_SCHEMA_VERSION)
    assert store.pool_snapshot()["runs"] == 1
    assert store.ingest_many(
        [trial("new")], sender="peer-c", transport="fleet",
    )["accepted"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_version_change_after_connect_is_rechecked_under_ingest_lock(
    tmp_path, monkeypatch,
):
    store, path, first = seeded(tmp_path)
    original_connect = store._connect
    before = counts(path)
    opened = []

    def drift_after_connect():
        db = original_connect()
        # The connection's initial version check has already succeeded.
        # Inject external equivalent write before BEGIN IMMEDIATE.
        db.execute("PRAGMA user_version=99")
        opened.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", drift_after_connect)
        with pytest.raises(TrialStoreError, match="schema version changed"):
            store.ingest_many(
                [trial("race")], sender="peer-c", transport="fleet",
            )
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert version(path) == 99
    assert counts(path) == before
    rewrite_version(path, DB_SCHEMA_VERSION)
    assert store.ingest_many(
        [trial("race")], sender="peer-c", transport="fleet",
    )["accepted"] == 1


def test_concurrent_schema_version_writer_is_blocked_by_receiver_transaction(
    tmp_path,
):
    store, path, _ = seeded(tmp_path)
    db = store._connect()
    attacker = sqlite3.connect(path, timeout=0.02, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        store._assert_live_schema_version(db)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            attacker.execute("PRAGMA user_version=11")
        db.commit()
    finally:
        db.close()
        attacker.close()

    assert version(path) == DB_SCHEMA_VERSION
    assert store.ingest_many(
        [trial("post-lock")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_fresh_receiver_refuses_future_schema_version_without_downgrade(tmp_path):
    _, path, _ = seeded(tmp_path)
    before = counts(path)
    rewrite_version(path, 100)
    with pytest.raises(TrialStoreError, match="newer"):
        TrialStore(path)
    assert version(path) == 100
    assert counts(path) == before


def test_healthy_version_persists_across_wal_checkpoint_and_restart(tmp_path):
    store, path, first = seeded(tmp_path)
    before = store.pool_snapshot()
    db = store._connect()
    try:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
    reopened = TrialStore(path)
    assert reopened.pool_snapshot() == before
    assert version(path) == DB_SCHEMA_VERSION
    assert reopened.ingest_many(
        [first], sender="peer-c", transport="fleet"
    )["duplicates"] == 1
