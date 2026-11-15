"""Round 216: all public evidence audits use one stable WAL read snapshot.

A receipt is checked against several tables and multiple SQLite SELECTs.
Without an explicit read transaction, concurrent committed ingestion could
make a single audit combine rows from different database states. Read-only
BEGIN pins one snapshot after the first SELECT, without blocking WAL writers.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="216", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-216", run_id="run-216",
        check_id="read-snapshot", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    assert store.ingest_many([trial()], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([trial("skip", "skipped")], sender="peer-a",
                             transport="fleet")["non_pass"] == 1
    return store, path


@pytest.mark.parametrize(
    "reader,outcome",
    [("pool_snapshot", "pass"), ("non_pass_records", "skipped")],
)
def test_trial_audits_preserve_one_snapshot_across_concurrent_new_ingest(
    tmp_path, monkeypatch, reader, outcome,
):
    store, path = seeded(tmp_path)
    writer = TrialStore(path)
    expected = getattr(store, reader)()
    original = TrialStore._verify_stored_trial
    triggered = []

    def inject_after_first_trial_row(row):
        if not triggered:
            triggered.append(True)
            response = writer.ingest_many(
                [trial("concurrent", outcome)],
                sender="peer-c", transport="fleet",
            )
            assert response["accepted"] + response["non_pass"] == 1
        return original(row)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_verify_stored_trial",
                    staticmethod(inject_after_first_trial_row))
        assert getattr(store, reader)() == expected

    assert triggered == [True]
    assert getattr(store, reader)() != expected
    # Only pass/fail trials enter the pooled orientation. Skipped evidence
    # remains visible in non_pass_records but does not increase pool runs.
    expected_runs = 2 if reader == "pool_snapshot" else 1
    assert TrialStore(path).pool_snapshot()["runs"] == expected_runs


def test_receipt_audit_keeps_original_snapshot_while_writer_appends(
    tmp_path, monkeypatch,
):
    store, path = seeded(tmp_path)
    writer = TrialStore(path)
    expected = store.receipt_records()
    original = TrialStore._verify_receipt_row
    injected = []

    def inject_between_receipt_and_trial_lookup(self, db, row):
        assert db.in_transaction, "receipt audit must pin a read transaction"
        if not injected:
            injected.append(True)
            assert writer.ingest_many(
                [trial("new-concurrent")], sender="peer-c", transport="fleet"
            )["accepted"] == 1
        return original(self, db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_verify_receipt_row",
                    inject_between_receipt_and_trial_lookup)
        assert store.receipt_records() == expected

    assert injected == [True]
    assert len(store.receipt_records()) == len(expected) + 1
    assert len(TrialStore(path).receipt_records()) == 3


def test_quarantine_audit_does_not_mix_conflicts_from_parallel_writer(
    tmp_path, monkeypatch,
):
    store, path = seeded(tmp_path)
    first = trial()
    second = trial("skip", "skipped")
    assert store.ingest_many([trial(outcome="fail")],
                             sender="peer-b", transport="fleet")["quarantined"] == 1
    expected = store.quarantine_records()
    assert len(expected) == 1
    writer = TrialStore(path)
    original = TrialStore._assert_quarantine_conflict_semantics
    fired = []

    def inject_between_conflict_and_original(self, db, row):
        assert db.in_transaction, "quarantine audit must pin a read transaction"
        if not fired:
            fired.append(True)
            assert writer.ingest_many([trial("skip", "fail")],
                                      sender="peer-c", transport="fleet")["quarantined"] == 1
        return original(self, db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_assert_quarantine_conflict_semantics",
                    inject_between_conflict_and_original)
        assert store.quarantine_records() == expected

    assert fired == [True]
    assert len(store.quarantine_records()) == 2
    assert len(TrialStore(path).quarantine_records()) == 2


@pytest.mark.parametrize("reader", ["receipt_records", "quarantine_records"])
def test_sequence_schema_attestation_runs_inside_audit_transaction(
    tmp_path, monkeypatch, reader,
):
    store, path = seeded(tmp_path)
    original = TrialStore._assert_event_sequence_schema
    observed = []

    def checked(db):
        observed.append(db.in_transaction)
        return original(db)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_assert_event_sequence_schema",
                    staticmethod(checked))
        getattr(store, reader)()

    assert observed == [True]
    assert TrialStore(path).pool_snapshot()["runs"] == 1


@pytest.mark.parametrize(
    "reader", ["receipt_records", "quarantine_records",
               "pool_snapshot", "non_pass_records"],
)
def test_all_public_audit_methods_close_read_transaction_and_release_wal_lock(
    tmp_path, monkeypatch, reader,
):
    store, path = seeded(tmp_path)
    original_connect = store._connect
    opened = []

    def capture_connection():
        db = original_connect()
        opened.append(db)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", capture_connection)
        getattr(store, reader)()

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    writer = TrialStore(path)
    assert writer.ingest_many(
        [trial("after-read")], sender="peer-d", transport="fleet"
    )["accepted"] == 1


def test_corrupt_receipt_failure_closes_reader_and_writer_can_still_progress(
    tmp_path, monkeypatch,
):
    store, path = seeded(tmp_path)
    db = store._connect()
    try:
        db.execute(
            "UPDATE trial_receipts SET payload_sha256='invalid' "
            "WHERE receipt_id=(SELECT MIN(receipt_id) FROM trial_receipts)"
        )
        db.commit()
    finally:
        db.close()
    original_connect = store._connect
    observed = []

    def capture():
        conn = original_connect()
        observed.append(conn)
        return conn

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_connect", capture)
        with pytest.raises(TrialStoreError, match="receipt integrity"):
            store.receipt_records()

    assert len(observed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed[0].execute("SELECT 1")
    assert TrialStore(path).ingest_many(
        [trial("after-failed-audit")], sender="peer-e", transport="fleet"
    )["accepted"] == 1

@pytest.mark.parametrize("cap,new_trials", [(2, 1), (3, 2)])
def test_receipt_audit_snapshot_survives_concurrent_pruning_of_earliest_event(
    tmp_path, monkeypatch, cap, new_trials,
):
    store, path = seeded(tmp_path)
    writer = TrialStore(path)
    from avrone import trial_receiver
    original = TrialStore._verify_receipt_row
    observed = []

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", cap)
        expected = store.receipt_records()
        assert len(expected) == 2

        def prune_during_read(self, db, row):
            assert db.in_transaction
            if not observed:
                observed.append(True)
                payloads = [trial(f"prune-{index}") for index in range(new_trials)]
                result = writer.ingest_many(
                    payloads, sender="peer-c", transport="fleet"
                )
                assert result["accepted"] == new_trials
            return original(self, db, row)

        ctx.setattr(TrialStore, "_verify_receipt_row", prune_during_read)
        # The original receipt rows have been pruned by the writer, but
        # they remain a self-consistent view for this active reader.
        assert store.receipt_records() == expected
        assert observed == [True]
        newest = store.receipt_records()
        assert len(newest) == cap
        assert expected[0]["receipt_id"] not in {
            row["receipt_id"] for row in newest
        }

    assert len(TrialStore(path).receipt_records()) == cap


def test_receipt_audit_snapshot_ignores_concurrent_tampering_until_next_read(
    tmp_path, monkeypatch,
):
    store, path = seeded(tmp_path)
    expected = store.receipt_records()
    original = TrialStore._verify_receipt_row
    tampered = []

    def corrupt_other_retained_receipt(self, db, row):
        assert db.in_transaction
        if not tampered:
            tampered.append(True)
            writer = TrialStore(path)
            conn = writer._connect()
            try:
                conn.execute(
                    "UPDATE trial_receipts SET payload_sha256='invalid' "
                    "WHERE receipt_id=(SELECT MAX(receipt_id) FROM trial_receipts)"
                )
                conn.commit()
            finally:
                conn.close()
        return original(self, db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_verify_receipt_row",
                    corrupt_other_retained_receipt)
        assert store.receipt_records() == expected

    assert tampered == [True]
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()


def test_quarantine_snapshot_uses_original_marker_during_concurrent_marker_loss(
    tmp_path, monkeypatch,
):
    store, path = seeded(tmp_path)
    assert store.ingest_many([trial(outcome="fail")],
                             sender="peer-b", transport="fleet")["quarantined"] == 1
    expected = store.quarantine_records()
    original = TrialStore._assert_quarantine_conflict_semantics
    deleted = []

    def delete_marker_during_conflict_audit(self, db, row):
        assert db.in_transaction
        if not deleted:
            deleted.append(True)
            writer = TrialStore(path)
            conn = writer._connect()
            try:
                conn.execute(
                    "DELETE FROM trial_quarantined_senders WHERE sender='peer-b'"
                )
                conn.commit()
            finally:
                conn.close()
        return original(self, db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_assert_quarantine_conflict_semantics",
                    delete_marker_during_conflict_audit)
        assert store.quarantine_records() == expected

    assert deleted == [True]
    with pytest.raises(TrialStoreError, match="sender quarantine state"):
        store.quarantine_records()
