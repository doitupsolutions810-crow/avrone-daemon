"""Round 204: required SQLite trigger names must implement the expected rules.

CREATE TRIGGER IF NOT EXISTS does not prove a preexisting trigger is genuine.
A privileged SQL writer may replace one of the two named source-key guards
with a no-op, weakened predicate or trigger on a different operation. Verify
the actual sqlite_master SQL before reads and under the ingest writer lock.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import (
    TrialStore, TrialStoreError, _REQUIRED_SOURCE_IDENTITY_TRIGGERS,
)


INSERT_NAME = "trial_source_identity_key_required_insert"
UPDATE_NAME = "trial_source_identity_key_required_update"


def envelope(attempt="204", outcome="pass"):
    return make_trial(
        system="grokbot", node="node-204", run_id="run-204",
        check_id="schema-trigger", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    original = envelope()
    assert store.ingest_many(
        [original], sender="peer-a", transport="fleet",
    )["accepted"] == 1
    assert store.ingest_many(
        [envelope(outcome="fail")], sender="peer-b", transport="fleet",
    )["quarantined"] == 1
    return store, path, original


def patch_trigger(store, name, replacement):
    db = store._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP TRIGGER " + name)
        if replacement is not None:
            db.execute(replacement)
        db.commit()
    finally:
        db.close()


def restore_trigger(path, name):
    db = sqlite3.connect(path)
    try:
        db.execute(_REQUIRED_SOURCE_IDENTITY_TRIGGERS[name])
        db.commit()
    finally:
        db.close()


def counts(path):
    db = sqlite3.connect(path)
    try:
        return tuple(db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in (
            "trials", "trial_quarantine", "trial_quarantined_senders",
            "trial_receipts",
        ))
    finally:
        db.close()


@pytest.mark.parametrize("name", [INSERT_NAME, UPDATE_NAME], ids=["insert", "update"])
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("missing", id="deleted"),
        pytest.param("noop", id="empty-condition"),
        pytest.param("wrong_table", id="different-table"),
        pytest.param("always_allow", id="never-fires"),
        pytest.param("weakened", id="missing-bounds"),
    ],
)
def test_removed_or_spoofed_named_triggers_fail_closed_and_recover(
    tmp_path, name, mode,
):
    store, path, original = seeded(tmp_path)
    before = counts(path)
    if mode == "missing":
        replacement = None
    elif mode == "noop":
        action = "INSERT" if name == INSERT_NAME else "UPDATE OF source_identity_key"
        replacement = (
            f"CREATE TRIGGER {name} BEFORE {action} ON trials "
            "BEGIN SELECT 1; END"
        )
    elif mode == "wrong_table":
        action = "INSERT" if name == INSERT_NAME else "UPDATE OF sender"
        replacement = (
            f"CREATE TRIGGER {name} BEFORE {action} ON trial_receipts "
            "BEGIN SELECT 1; END"
        )
    elif mode == "always_allow":
        action = "INSERT" if name == INSERT_NAME else "UPDATE OF source_identity_key"
        replacement = (
            f"CREATE TRIGGER {name} BEFORE {action} ON trials WHEN 0 "
            "BEGIN SELECT RAISE(ABORT, 'invalid source_identity_key'); END"
        )
    else:
        replacement = _REQUIRED_SOURCE_IDENTITY_TRIGGERS[name].replace(
            "OR length(NEW.source_identity_key) != 64", "OR 0"
        )

    patch_trigger(store, name, replacement)
    for reader in (
        store.pool_snapshot, store.non_pass_records,
        store.quarantine_records, store.receipt_records,
    ):
        with pytest.raises(TrialStoreError, match="required trigger"):
            reader()
    with pytest.raises(TrialStoreError, match="required trigger"):
        store.ingest_many(
            [envelope("new")], sender="peer-c", transport="fleet",
        )
    with pytest.raises(TrialStoreError, match="required trigger"):
        store.ingest_many([original], sender="peer-c", transport="fleet")
    assert counts(path) == before

    if replacement is not None:
        raw = sqlite3.connect(path)
        try:
            raw.execute("DROP TRIGGER " + name)
            raw.commit()
        finally:
            raw.close()
    restore_trigger(path, name)
    assert store.pool_snapshot()["runs"] == 1
    assert store.ingest_many(
        [envelope("new")], sender="peer-c", transport="fleet"
    )["accepted"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 2


@pytest.mark.parametrize("name", [INSERT_NAME, UPDATE_NAME], ids=["insert", "update"])
def test_forged_named_trigger_is_refused_on_fresh_startup(tmp_path, name):
    store, path, original = seeded(tmp_path)
    patch_trigger(
        store, name,
        f"CREATE TRIGGER {name} BEFORE INSERT ON trials BEGIN SELECT 1; END",
    )
    with pytest.raises(TrialStoreError, match="required trigger"):
        TrialStore(path)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
        raw.execute("DROP TRIGGER " + name)
        raw.commit()
    finally:
        raw.close()
    assert TrialStore(path).pool_snapshot()["runs"] == 1


def test_triggers_changed_after_connection_are_checked_inside_ingest_lock(
    tmp_path, monkeypatch,
):
    store, path, _ = seeded(tmp_path)
    before = counts(path)
    connect = store._connect
    opened = []

    def inject_after_connect():
        db = connect()
        db.execute("DROP TRIGGER " + INSERT_NAME)
        db.execute(
            f"CREATE TRIGGER {INSERT_NAME} BEFORE INSERT ON trials "
            "BEGIN SELECT 1; END"
        )
        db.commit()
        opened.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", inject_after_connect)
        with pytest.raises(TrialStoreError, match="required trigger"):
            store.ingest_many(
                [envelope("race")], sender="peer-c", transport="fleet",
            )
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert counts(path) == before
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP TRIGGER " + INSERT_NAME)
        raw.commit()
    finally:
        raw.close()
    restore_trigger(path, INSERT_NAME)
    assert store.ingest_many(
        [envelope("race")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_competing_trigger_ddl_cannot_pass_sqlite_writer_lock(tmp_path):
    store, path, _ = seeded(tmp_path)
    db = store._connect()
    other = sqlite3.connect(path, timeout=0.02, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        store._assert_source_identity_required_triggers(db)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("DROP TRIGGER " + INSERT_NAME)
        db.commit()
    finally:
        db.close()
        other.close()
    assert store.ingest_many(
        [envelope("post-lock")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


@pytest.mark.parametrize("name", [INSERT_NAME, UPDATE_NAME], ids=["insert", "update"])
def test_missing_named_triggers_are_restored_on_fresh_trusted_startup(tmp_path, name):
    store, path, _ = seeded(tmp_path)
    patch_trigger(store, name, None)
    with pytest.raises(TrialStoreError, match="required trigger"):
        store.pool_snapshot()
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 1
    assert reopened.ingest_many(
        [envelope("restored")], sender="peer-c", transport="fleet"
    )["accepted"] == 1


def test_legitimate_first_seen_duplicate_conflict_and_blocked_sender_are_unchanged(tmp_path):
    store, path, first = seeded(tmp_path)
    assert store.ingest_many(
        [first], sender="peer-c", transport="fleet"
    )["duplicates"] == 1
    assert store.ingest_many(
        [envelope("other")], sender="peer-c", transport="fleet"
    )["accepted"] == 1
    assert store.ingest_many(
        [envelope("blocked")], sender="peer-b", transport="fleet"
    )["results"][0]["status"] == "sender_quarantined"
    assert len(store.quarantine_records()) == 1
    assert len(store.receipt_records()) == 5
    assert TrialStore(path).pool_snapshot()["runs"] == 2
