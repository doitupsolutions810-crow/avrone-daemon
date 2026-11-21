"""Round 210: a malformed SQLite BLOB must be bounded in SQL, not Python.

LIMIT max+1 bounds result *count* but not the size of an individual row. A
corrupted database may store a huge canonical_json or quarantine incoming_json
BLOB. All public reads, duplicate/alias ingests, and conflict proof paths must
project oversized content as NULL before sqlite3 builds a Python bytes object.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from avrone.trial_contract import MAX_ENVELOPE_BYTES, make_trial
from avrone.trial_receiver import (
    BOUNDED_TRIAL_COLUMNS,
    BOUNDED_QUARANTINE_COLUMNS,
    TrialStore, TrialStoreError,
)


def envelope(outcome="pass", attempt="210", system="cyclekernel"):
    return make_trial(
        system=system, node="node-210", run_id="run-210",
        check_id="sql-bounded-blob", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = envelope()
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many(
        [envelope(outcome="fail")], sender="peer-b", transport="fleet"
    )["quarantined"] == 1
    assert len(store.quarantine_records()) == 1
    return store, path, first


def mutate(store, table):
    assert table in {"trials", "trial_quarantine"}
    column = "canonical_json" if table == "trials" else "incoming_json"
    db = store._connect()
    try:
        db.execute(
            f"UPDATE {table} SET {column}=zeroblob(?)",
            (MAX_ENVELOPE_BYTES + 1,),
        )
        db.commit()
    finally:
        db.close()


def counts(store):
    db = store._connect()
    try:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                     for name in (
                         "trials", "trial_receipts", "trial_quarantine",
                         "trial_quarantined_senders",
                     ))
    finally:
        db.close()


def test_trial_projection_suppresses_oversized_blob_before_fetch(tmp_path):
    store, _, first = seeded(tmp_path)
    mutate(store, "trials")
    db = store._connect()
    try:
        row = db.execute(
            f"SELECT {BOUNDED_TRIAL_COLUMNS} FROM trials WHERE trial_id=?",
            (first["trial_id"],),
        ).fetchone()
        assert row["canonical_json"] is None
        assert row["trial_id"] == first["trial_id"]
        assert len(row["payload_sha256"]) == 64
    finally:
        db.close()


def test_quarantine_projection_suppresses_oversized_blob_before_fetch(tmp_path):
    store, _, _ = seeded(tmp_path)
    mutate(store, "trial_quarantine")
    db = store._connect()
    try:
        row = db.execute(
            f"SELECT {BOUNDED_QUARANTINE_COLUMNS} FROM trial_quarantine"
        ).fetchone()
        assert row["incoming_json"] is None
        assert row["sender"] == "peer-b"
        assert len(row["incoming_sha256"]) == 64
    finally:
        db.close()


@pytest.mark.parametrize("reader", ["pool_snapshot", "non_pass_records", "receipt_records"])
def test_oversized_first_seen_payload_fails_all_audits_without_partial_result(
    tmp_path, reader,
):
    store, path, _ = seeded(tmp_path)
    before = counts(store)
    mutate(store, "trials")
    with pytest.raises(TrialStoreError, match="integrity"):
        getattr(store, reader)()
    assert counts(store) == before
    with pytest.raises(TrialStoreError, match="integrity"):
        getattr(TrialStore(path), reader)()


@pytest.mark.parametrize("delivery", ["duplicate", "canonical-source-alias"])
def test_oversized_existing_trial_rejected_before_duplicate_or_alias_receipt(
    tmp_path, delivery,
):
    store, _, first = seeded(tmp_path)
    mutate(store, "trials")
    before = counts(store)
    payload = first if delivery == "duplicate" else envelope(system="CYCLEKERNEL")
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.ingest_many([payload], sender="peer-c", transport="fleet")
    assert counts(store) == before


@pytest.mark.parametrize("reader", ["quarantine_records", "receipt_records"])
def test_oversized_original_blocks_quarantine_proof_read(tmp_path, reader):
    store, _, _ = seeded(tmp_path)
    mutate(store, "trials")
    with pytest.raises(TrialStoreError, match="integrity"):
        getattr(store, reader)()


@pytest.mark.parametrize("reader", ["quarantine_records", "receipt_records"])
def test_oversized_conflict_payload_refused_during_complete_audit(
    tmp_path, reader,
):
    store, path, _ = seeded(tmp_path)
    mutate(store, "trial_quarantine")
    with pytest.raises(TrialStoreError, match="integrity"):
        getattr(store, reader)()
    with pytest.raises(TrialStoreError, match="integrity"):
        getattr(TrialStore(path), reader)()


def test_oversized_conflict_prevents_blocked_sender_from_receiving_new_receipt(tmp_path):
    store, _, _ = seeded(tmp_path)
    mutate(store, "trial_quarantine")
    before = counts(store)
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.ingest_many(
            [envelope(attempt="later")], sender="peer-b", transport="fleet"
        )
    assert counts(store) == before


def test_clean_canonical_and_conflict_data_remain_valid_with_sql_projection(tmp_path):
    store, path, first = seeded(tmp_path)
    assert store.pool_snapshot()["runs"] == 1
    assert len(store.receipt_records()) == 2
    assert store.ingest_many(
        [first], sender="peer-c", transport="fleet"
    )["duplicates"] == 1
    assert store.ingest_many(
        [envelope(system="CYCLEKERNEL")], sender="peer-d", transport="fleet"
    )["quarantined"] == 1
    assert len(store.quarantine_records()) == 2
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 1
    assert len(reopened.receipt_records()) == 4
