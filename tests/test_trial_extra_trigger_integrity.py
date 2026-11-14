"""Round 205: reject unauthorized persistent/TEMP triggers on evidence tables.

A valid source-key index and both required triggers are not sufficient when
an additional BEFORE/AFTER trigger can suppress, rewrite, or delete a receipt
or canonical trial during an otherwise authorized SQLite transaction.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def envelope(attempt="205", outcome="pass"):
    return make_trial(
        system="grokbot", node="node-205", run_id="run-205",
        check_id="extra-trigger", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trial.sqlite3"
    store = TrialStore(path)
    first = envelope()
    assert store.ingest_many(
        [first], sender="peer-a", transport="fleet",
    )["accepted"] == 1
    assert store.ingest_many(
        [envelope(outcome="fail")], sender="peer-b", transport="fleet"
    )["quarantined"] == 1
    assert len(store.quarantine_records()) == 1
    return store, path, first


def counts(path):
    db = sqlite3.connect(path)
    try:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "trials", "trial_receipts", "trial_quarantine",
                "trial_quarantined_senders",
            )
        )
    finally:
        db.close()


def run_ddl(path, sql):
    db = sqlite3.connect(path)
    try:
        db.execute(sql)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "trigger",
    [
        pytest.param(
            "CREATE TRIGGER malicious AFTER INSERT ON trials "
            "BEGIN UPDATE trials SET outcome='fail' WHERE trial_id=NEW.trial_id; END",
            id="modify-first-seen-outcome",
        ),
        pytest.param(
            "CREATE TRIGGER malicious BEFORE INSERT ON trials "
            "BEGIN SELECT RAISE(IGNORE); END",
            id="ignore-first-seen-insert",
        ),
        pytest.param(
            "CREATE TRIGGER malicious AFTER UPDATE ON trials "
            "BEGIN DELETE FROM trials WHERE trial_id=NEW.trial_id; END",
            id="delete-on-trial-update",
        ),
        pytest.param(
            "CREATE TRIGGER malicious AFTER INSERT ON trial_receipts "
            "BEGIN UPDATE trial_receipts SET result='duplicate' "
            "WHERE receipt_id=NEW.receipt_id; END",
            id="forge-receipt-result",
        ),
        pytest.param(
            "CREATE TRIGGER malicious AFTER INSERT ON trial_quarantine "
            "BEGIN DELETE FROM trial_quarantine "
            "WHERE quarantine_id=NEW.quarantine_id; END",
            id="erase-conflict",
        ),
        pytest.param(
            "CREATE TRIGGER malicious AFTER INSERT ON trial_quarantined_senders "
            "BEGIN DELETE FROM trial_quarantined_senders WHERE sender=NEW.sender; END",
            id="erase-block-marker",
        ),
    ],
)
def test_extra_evidence_trigger_fails_closed_on_reads_and_ingest_then_recovers(
    tmp_path, trigger,
):
    store, path, original = seeded(tmp_path)
    baseline = counts(path)
    run_ddl(path, trigger)
    for reader in (
        store.pool_snapshot, store.non_pass_records,
        store.quarantine_records, store.receipt_records,
    ):
        with pytest.raises(TrialStoreError, match="unexpected evidence trigger"):
            reader()
    with pytest.raises(TrialStoreError, match="unexpected evidence trigger"):
        store.ingest_many(
            [envelope("new")], sender="peer-c", transport="fleet",
        )
    with pytest.raises(TrialStoreError, match="unexpected evidence trigger"):
        store.ingest_many([original], sender="peer-c", transport="fleet")
    with pytest.raises(TrialStoreError, match="unexpected evidence trigger"):
        TrialStore(path)

    assert counts(path) == baseline
    run_ddl(path, "DROP TRIGGER malicious")
    assert store.pool_snapshot()["runs"] == 1
    assert store.ingest_many(
        [envelope("new")], sender="peer-c", transport="fleet",
    )["accepted"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_unexpected_temp_trigger_is_rejected_and_connection_closed(
    tmp_path, monkeypatch,
):
    store, path, _ = seeded(tmp_path)
    before = counts(path)
    from avrone import trial_receiver
    real_connect = trial_receiver.sqlite3.connect
    opened = []

    def planted_temp_trigger(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.execute(
            "CREATE TEMP TRIGGER malicious_temp BEFORE INSERT ON main.trials "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        opened.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.sqlite3, "connect", planted_temp_trigger)
        with pytest.raises(TrialStoreError, match="unexpected evidence trigger"):
            store.pool_snapshot()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert counts(path) == before
    assert store.pool_snapshot()["runs"] == 1


def test_unrelated_application_tables_and_triggers_remain_allowed(tmp_path):
    store, path, _ = seeded(tmp_path)
    run_ddl(path, "CREATE TABLE unrelated_events(value TEXT NOT NULL)")
    run_ddl(
        path,
        "CREATE TRIGGER unrelated_guard BEFORE INSERT ON unrelated_events "
        "WHEN NEW.value='' BEGIN SELECT RAISE(ABORT, 'blank'); END",
    )
    db = sqlite3.connect(path)
    try:
        db.execute("INSERT INTO unrelated_events(value) VALUES ('okay')")
        with pytest.raises(sqlite3.IntegrityError, match="blank"):
            db.execute("INSERT INTO unrelated_events(value) VALUES ('')")
        db.commit()
    finally:
        db.close()
    assert store.pool_snapshot()["runs"] == 1
    assert store.ingest_many(
        [envelope("allowed")], sender="peer-c", transport="fleet",
    )["accepted"] == 1


def test_rogue_trigger_cannot_be_added_between_locked_check_and_ingest(tmp_path):
    store, path, _ = seeded(tmp_path)
    protected = store._connect()
    competing = sqlite3.connect(path, timeout=0.02, isolation_level=None)
    try:
        protected.execute("BEGIN IMMEDIATE")
        store._assert_source_identity_required_triggers(protected)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            competing.execute(
                "CREATE TRIGGER malicious AFTER INSERT ON trials "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        protected.commit()
    finally:
        protected.close()
        competing.close()
    assert store.ingest_many(
        [envelope("after-lock")], sender="peer-c", transport="fleet",
    )["accepted"] == 1


def test_normal_replay_and_conflict_survive_clean_restart(tmp_path):
    store, path, first = seeded(tmp_path)
    assert store.ingest_many(
        [first], sender="peer-c", transport="fleet"
    )["duplicates"] == 1
    assert store.ingest_many(
        [envelope("blocked")], sender="peer-b", transport="fleet"
    )["results"][0]["status"] == "sender_quarantined"
    expected = store.pool_snapshot()
    reopened = TrialStore(path)
    assert reopened.pool_snapshot() == expected
    assert len(reopened.quarantine_records()) == 1
    assert len(reopened.receipt_records()) == 4
