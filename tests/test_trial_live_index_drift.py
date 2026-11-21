"""Round 200: a live receiver must refuse weakened source uniqueness on writes.

Startup verification can become stale. A different SQLite writer may remove
or replace the trusted index after initialization. Every ingest transaction
must prove the actual index definition while SQLite's writer lock is held,
including duplicates, blocked senders and conflict quarantine receipts.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


ORIGINAL_INDEX = (
    "CREATE UNIQUE INDEX trial_source_identity_key ON trials(source_identity_key)"
)
BAD_INDEXES = [
    pytest.param(None, id="removed-index"),
    pytest.param(
        "CREATE INDEX trial_source_identity_key ON trials(source_identity_key)",
        id="nonunique-index",
    ),
    pytest.param(
        "CREATE UNIQUE INDEX trial_source_identity_key ON trials(run_id)",
        id="different-column",
    ),
    pytest.param(
        "CREATE UNIQUE INDEX trial_source_identity_key "
        "ON trials(source_identity_key,run_id)",
        id="compound-index",
    ),
    pytest.param(
        "CREATE UNIQUE INDEX trial_source_identity_key "
        "ON trials(source_identity_key) WHERE outcome='pass'",
        id="partial-index",
    ),
    pytest.param(
        "CREATE UNIQUE INDEX trial_source_identity_key "
        "ON trials(lower(source_identity_key))",
        id="expression-index",
    ),
]


def envelope(attempt="r200", *, system="grokbot", outcome="pass"):
    return make_trial(
        system=system, node="node-200", run_id="run-200",
        check_id="schema-drift", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = envelope()
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([envelope(outcome="fail")],
                             sender="peer-b", transport="fleet")["quarantined"] == 1
    return store, path, first


def index_ddl(store, replacement):
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP INDEX IF EXISTS trial_source_identity_key")
        if replacement is not None:
            conn.execute(replacement)
        conn.commit()
    finally:
        conn.close()


def counts(store):
    db = store._connect()
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


@pytest.mark.parametrize("bad_index", BAD_INDEXES)
def test_live_schema_drift_refuses_all_evidence_write_statuses_and_recovers(
    tmp_path, bad_index,
):
    store, path, original = seeded(tmp_path)
    baseline = counts(store)
    original_pool = store.pool_snapshot()
    index_ddl(store, bad_index)
    assert store.pool_snapshot() == original_pool  # existing data still valid
    assert store.quarantine_records()[0]["sender"] == "peer-b"

    delivery_types = [
        ("peer-c", envelope("new"), "new-first-seen"),
        ("peer-c", original, "duplicate"),
        ("peer-c", envelope(system="GROKBOT"), "canonical-alias"),
        ("peer-b", envelope("blocked"), "blocked-sender"),
    ]
    for sender, payload, label in delivery_types:
        with pytest.raises(TrialStoreError, match="unique index") as exc:
            store.ingest_many([payload], sender=sender, transport="fleet")
        assert "index" in str(exc.value), label

    assert counts(store) == baseline
    assert store.pool_snapshot() == original_pool
    index_ddl(store, ORIGINAL_INDEX)
    result = store.ingest_many([envelope("new")], sender="peer-c", transport="fleet")
    assert result["accepted"] == 1
    assert store.ingest_many([original], sender="peer-c",
                             transport="fleet")["duplicates"] == 1
    assert store.ingest_many([envelope("blocked")], sender="peer-b",
                             transport="fleet")["results"][0]["status"] == "sender_quarantined"
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_index_swapped_between_connection_and_begin_is_denied_before_receipt(
    tmp_path, monkeypatch,
):
    store, path, _ = seeded(tmp_path)
    original = store._connect
    before = counts(store)
    changed = []

    def changed_before_begin():
        db = original()
        db.execute("DROP INDEX trial_source_identity_key")
        db.execute(
            "CREATE INDEX trial_source_identity_key ON trials(source_identity_key)"
        )
        db.commit()
        changed.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", changed_before_begin)
        with pytest.raises(TrialStoreError, match="index integrity"):
            store.ingest_many(
                [envelope("late")], sender="peer-c", transport="fleet",
            )

    assert len(changed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        changed[0].execute("SELECT 1")
    assert counts(store) == before
    index_ddl(store, ORIGINAL_INDEX)
    assert store.ingest_many(
        [envelope("late")], sender="peer-c", transport="fleet",
    )["accepted"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_index_schema_cannot_be_changed_after_ingest_writer_lock_acquired(tmp_path):
    store, path, _ = seeded(tmp_path)
    db = store._connect()
    attacker = sqlite3.connect(path, timeout=0.02, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        store._assert_source_identity_unique_index(db)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            attacker.execute("DROP INDEX trial_source_identity_key")
        db.commit()
    finally:
        db.close()
        attacker.close()
    assert store.ingest_many(
        [envelope("unaffected")], sender="peer-c", transport="fleet",
    )["accepted"] == 1


def test_fresh_receiver_can_restore_missing_index_without_losing_evidence(tmp_path):
    store, path, _ = seeded(tmp_path)
    before = store.pool_snapshot()
    index_ddl(store, None)
    with pytest.raises(TrialStoreError, match="unique index"):
        store.ingest_many(
            [envelope("not-yet")], sender="peer-c", transport="fleet"
        )
    reopened = TrialStore(path)
    assert reopened.pool_snapshot() == before
    assert reopened.ingest_many(
        [envelope("now-accepted")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_repaired_index_preserves_alias_quarantine_and_idempotent_replay(tmp_path):
    store, path, original = seeded(tmp_path)
    index_ddl(store, BAD_INDEXES[1].values[0] if hasattr(BAD_INDEXES[1], "values") else None)
    with pytest.raises(TrialStoreError, match="index integrity"):
        store.ingest_many([original], sender="peer-c", transport="fleet")
    index_ddl(store, ORIGINAL_INDEX)
    assert store.ingest_many([original], sender="peer-c",
                             transport="fleet")["duplicates"] == 1
    assert store.ingest_many([envelope(system="GROKBOT")], sender="peer-c",
                             transport="fleet")["quarantined"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 1
