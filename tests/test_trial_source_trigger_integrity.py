"""Round 201: check actual SQLite source-identity guard definitions, not names.

CREATE TRIGGER IF NOT EXISTS is not an attestation: an attacker can plant a
same-name no-op trigger, which SQLite then preserves. A running receiver must
validate both definitions under the same write lock as every trial receipt.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import (
    SOURCE_IDENTITY_INSERT_TRIGGER_SQL,
    SOURCE_IDENTITY_UPDATE_TRIGGER_SQL,
    TrialStore,
    TrialStoreError,
)

TRIGGERS = {
    "insert": (
        "trial_source_identity_key_required_insert",
        SOURCE_IDENTITY_INSERT_TRIGGER_SQL,
    ),
    "update": (
        "trial_source_identity_key_required_update",
        SOURCE_IDENTITY_UPDATE_TRIGGER_SQL,
    ),
}


def trial(attempt="201", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-201",
        check_id="sql-triggers", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    original = trial()
    assert store.ingest_many([original], sender="peer-a", transport="fleet")["accepted"] == 1
    return store, path, original


def tamper(store, name, replacement=None):
    db = store._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"DROP TRIGGER {name}")
        if replacement:
            db.execute(replacement)
        db.commit()
    finally:
        db.close()


def restore(store, definition):
    db = store._connect()
    try:
        db.execute(definition)
        db.commit()
    finally:
        db.close()


def totals(store):
    db = store._connect()
    try:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
            "trials", "trial_quarantine", "trial_quarantined_senders", "trial_receipts",
        ))
    finally:
        db.close()


@pytest.mark.parametrize("which", ["insert", "update"])
def test_missing_source_guard_denies_running_receiver_before_any_receipt(tmp_path, which):
    store, path, original = seeded(tmp_path)
    name, definition = TRIGGERS[which]
    before = totals(store)
    tamper(store, name)
    assert store.pool_snapshot()["runs"] == 1
    for payload, sender in (
        (trial("new"), "peer-b"),
        (original, "peer-b"),
        (trial(outcome="fail"), "peer-b"),
    ):
        with pytest.raises(TrialStoreError, match="trigger integrity"):
            store.ingest_many([payload], sender=sender, transport="fleet")
    assert totals(store) == before
    # Fresh initialization is allowed to restore a genuinely *missing*
    # trusted trigger, unlike a same-name forged/no-op replacement.
    repaired = TrialStore(path)
    db = repaired._connect()
    try:
        repaired._assert_source_identity_triggers(db)
    finally:
        db.close()
    assert store.ingest_many([trial("new")], sender="peer-b", transport="fleet")["accepted"] == 1
    assert repaired.pool_snapshot()["runs"] == 2


@pytest.mark.parametrize("which", ["insert", "update"])
def test_same_name_noop_trigger_rejected_during_restart_and_ingest(tmp_path, which):
    store, path, original = seeded(tmp_path)
    name, definition = TRIGGERS[which]
    fake = f"CREATE TRIGGER {name} BEFORE INSERT ON trials BEGIN SELECT 1; END"
    tamper(store, name, fake)
    with pytest.raises(TrialStoreError, match="trigger integrity"):
        store.ingest_many([original], sender="peer-b", transport="fleet")
    with pytest.raises(TrialStoreError, match="trigger integrity"):
        TrialStore(path)
    db = store._connect()
    try:
        db.execute(f"DROP TRIGGER {name}")
        db.commit()
    finally:
        db.close()
    restore(store, definition)
    assert store.ingest_many([original], sender="peer-b", transport="fleet")["duplicates"] == 1


@pytest.mark.parametrize("which", ["insert", "update"])
def test_forged_trigger_with_weaker_condition_is_rejected(tmp_path, which):
    store, _, _ = seeded(tmp_path)
    name, definition = TRIGGERS[which]
    fake = definition.replace(
        "OR NEW.source_identity_key GLOB '*[^0-9a-f]*'",
        "OR NEW.source_identity_key GLOB '*[^0-9a-f]*' AND 0",
    )
    tamper(store, name, fake)
    with pytest.raises(TrialStoreError, match="trigger integrity"):
        store.ingest_many([trial("weak")], sender="peer-b", transport="fleet")


def test_clean_legacy_trigger_definitions_verified_across_restart(tmp_path):
    store, path, original = seeded(tmp_path)
    db = store._connect()
    try:
        store._assert_source_identity_triggers(db)
        expected = set(name for name, _ in TRIGGERS.values())
        names = set(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ))
        assert expected <= names
    finally:
        db.close()
    assert TrialStore(path).ingest_many(
        [original], sender="peer-b", transport="fleet"
    )["duplicates"] == 1


def test_trigger_swap_after_connect_before_writer_lock_is_denied(tmp_path, monkeypatch):
    store, _, original = seeded(tmp_path)
    real_connect = store._connect
    connections = []
    name, definition = TRIGGERS["insert"]

    def swapped_connect():
        db = real_connect()
        db.execute(f"DROP TRIGGER {name}")
        db.commit()
        connections.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", swapped_connect)
        with pytest.raises(TrialStoreError, match="trigger integrity"):
            store.ingest_many([original], sender="peer-b", transport="fleet")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    restore(store, definition)
    assert store.ingest_many([original], sender="peer-b", transport="fleet")["duplicates"] == 1


def test_writer_lock_blocks_concurrent_trigger_replacement(tmp_path):
    store, path, _ = seeded(tmp_path)
    owner = store._connect()
    other = sqlite3.connect(path, isolation_level=None, timeout=0.02)
    try:
        owner.execute("BEGIN IMMEDIATE")
        store._assert_source_identity_triggers(owner)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("DROP TRIGGER trial_source_identity_key_required_insert")
        owner.commit()
    finally:
        owner.close()
        other.close()
    assert store.ingest_many([trial("healthy")], sender="peer-b", transport="fleet")["accepted"] == 1


def test_noop_trigger_cannot_disguise_invalid_source_key_on_reopen(tmp_path):
    store, path, _ = seeded(tmp_path)
    name, _ = TRIGGERS["update"]
    fake = f"CREATE TRIGGER {name} BEFORE UPDATE ON trials BEGIN SELECT 1; END"
    tamper(store, name, fake)
    db = store._connect()
    try:
        # New-schema trials has a NOT NULL column: corrupt with malformed
        # non-null text to exercise the removed UPDATE guard itself.
        db.execute("UPDATE trials SET source_identity_key='malformed'")
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="trigger integrity"):
        store.ingest_many([trial("later")], sender="peer-b", transport="fleet")
    with pytest.raises(TrialStoreError, match="trigger integrity"):
        TrialStore(path)
