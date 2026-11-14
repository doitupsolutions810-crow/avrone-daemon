"""Round 204: every retained quarantine conflict must have a distinct event ID.

The public audit intentionally omits SQLite's internal quarantine_id, but that
does not make the identifier optional: corruption must not turn two distinct
conflict events into a single ambiguous sequence number.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(outcome="pass", attempt="204"):
    return make_trial(
        system="cyclekernel", node="node-204", run_id="run-204",
        check_id="event-id", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    assert store.ingest_many([trial()], sender="peer-a", transport="fleet")["accepted"] == 1
    for sender in ("peer-b", "peer-c"):
        assert store.ingest_many(
            [trial("fail")], sender=sender, transport="fleet",
        )["quarantined"] == 1
    assert {r["sender"] for r in store.quarantine_records()} == {"peer-b", "peer-c"}
    return store, path


def change(store, sql, args=()):
    db = store._connect()
    try:
        db.execute(sql, args)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("event_id", [0, -1, -9999])
def test_invalid_integer_quarantine_event_identifier_is_rejected(
    tmp_path, event_id,
):
    store, path = seeded(tmp_path)
    change(
        store,
        "UPDATE trial_quarantine SET quarantine_id=? WHERE sender='peer-b'",
        (event_id,),
    )
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.ingest_many([trial(attempt="other")], sender="peer-b", transport="fleet")
    assert store.pool_snapshot()["runs"] == 1


def test_repeated_quarantine_id_across_two_distinct_senders_is_rejected(tmp_path):
    store, path = seeded(tmp_path)
    db = store._connect()
    try:
        # Replace the table with a same-column CTAS table with no PK.
        # Unlike the regular schema, it permits two separate event rows to
        # claim the same quarantine ID while remaining payload-valid.
        db.execute(
            "CREATE TABLE replacement_quarantine AS SELECT * FROM trial_quarantine"
        )
        db.execute("DROP TABLE trial_quarantine")
        db.execute("ALTER TABLE replacement_quarantine RENAME TO trial_quarantine")
        db.execute(
            "UPDATE trial_quarantine SET quarantine_id="
            "(SELECT MIN(quarantine_id) FROM trial_quarantine)"
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="duplicate quarantine event identity"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="primary key integrity"):
        TrialStore(path)


def test_noninteger_event_id_in_corrupted_table_is_rejected(tmp_path):
    store, _ = seeded(tmp_path)
    db = store._connect()
    try:
        db.execute(
            "CREATE TABLE replacement_quarantine AS SELECT * FROM trial_quarantine"
        )
        db.execute("DROP TABLE trial_quarantine")
        db.execute("ALTER TABLE replacement_quarantine RENAME TO trial_quarantine")
        db.execute(
            "UPDATE trial_quarantine SET quarantine_id=? WHERE sender='peer-b'",
            (sqlite3.Binary(b"bad-id"),),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()


def test_valid_two_event_audit_keeps_existing_public_field_contract(tmp_path):
    store, path = seeded(tmp_path)
    rows = store.quarantine_records()
    assert len(rows) == 2
    assert {row["sender"] for row in rows} == {"peer-b", "peer-c"}
    assert all("quarantine_id" not in row and "incoming_json" not in row for row in rows)
    reopened = TrialStore(path)
    assert reopened.quarantine_records() == rows


def test_healthy_conflict_quarantine_ids_increase_without_false_duplicates(tmp_path):
    store, _ = seeded(tmp_path)
    db = store._connect()
    try:
        identifiers = [row["quarantine_id"] for row in db.execute(
            "SELECT quarantine_id FROM trial_quarantine ORDER BY quarantine_id"
        )]
        assert identifiers == sorted(identifiers)
        assert len(set(identifiers)) == len(identifiers) == 2
    finally:
        db.close()
    result = store.ingest_many(
        [trial("fail", attempt="new")], sender="peer-d", transport="fleet",
    )
    assert result["accepted"] == 1
    assert len(store.quarantine_records()) == 2
