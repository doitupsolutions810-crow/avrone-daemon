"""Round 222: retained receipt sequence order must reflect causal transitions.

An attacker can swap INTEGER PRIMARY KEY event IDs while preserving entirely
valid receipt bodies, digests, sender markers and timestamps. Per-row audit and
AUTOINCREMENT schema/sequence attestations still pass, yet a duplicate can
appear earlier than a retained first-seen receipt, or a blocked sender's
delivery earlier than that sender's retained first conflict. These checks only
enforce order when both events are still inside the retention window.
"""
from __future__ import annotations

import pytest

from avrone import trial_receiver
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="222", outcome="pass", system="cyclekernel"):
    return make_trial(
        system=system, node="node-222", run_id="run-222",
        check_id="event-order", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def store_at(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    return TrialStore(path), path


def swap_receipt_event_ids(store, first_status, second_status):
    db = store._connect()
    try:
        first = db.execute(
            "SELECT receipt_id FROM trial_receipts WHERE result=?",
            (first_status,),
        ).fetchone()[0]
        second = db.execute(
            "SELECT receipt_id FROM trial_receipts WHERE result=?",
            (second_status,),
        ).fetchone()[0]
        assert first < second
        temporary = max(first, second) + 100
        db.execute(
            "UPDATE trial_receipts SET receipt_id=? WHERE receipt_id=?",
            (temporary, first),
        )
        db.execute(
            "UPDATE trial_receipts SET receipt_id=? WHERE receipt_id=?",
            (first, second),
        )
        db.execute(
            "UPDATE trial_receipts SET receipt_id=? WHERE receipt_id=?",
            (second, temporary),
        )
        db.commit()
        # Table schema and sqlite_sequence remain fully valid. This is a
        # cross-row logical inconsistency, not a missing/forged PK.
        store._assert_primary_key_schema(db)
        store._assert_event_sequence_schema(db)
        return first, second
    finally:
        db.close()


@pytest.mark.parametrize("outcome", ["pass", "fail", "skipped"])
def test_duplicate_receipt_id_before_retained_first_seen_id_is_rejected(
    tmp_path, outcome,
):
    store, path = store_at(tmp_path)
    first = trial(outcome=outcome)
    first_status = "non_pass_stored" if outcome == "skipped" else "accepted"
    assert store.ingest_many([first], sender="peer-a",
                             transport="fleet")["results"][0]["status"] == first_status
    assert store.ingest_many([first], sender="peer-b",
                             transport="fleet")["duplicates"] == 1
    assert len(store.receipt_records()) == 2

    swap_receipt_event_ids(store, first_status, "duplicate")
    # Every row still passes its individual provenance integrity check.
    db = store._connect()
    try:
        for row in db.execute(
            "SELECT receipt_id,trial_id,sender,transport,bridge_run_id,"
            "result,payload_sha256,received_at FROM trial_receipts "
            "ORDER BY receipt_id"
        ):
            store._verify_receipt_row(db, row)
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="duplicate receipt precedes first-seen"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="duplicate receipt precedes first-seen"):
        TrialStore(path).receipt_records()
    assert store.pool_snapshot()["runs"] == (0 if outcome == "skipped" else 1)


@pytest.mark.parametrize("alias", [False, True],
                         ids=["same-id-conflict", "source-alias-conflict"])
def test_blocked_receipt_id_before_retained_first_conflict_id_is_rejected(
    tmp_path, alias,
):
    store, path = store_at(tmp_path)
    root = trial()
    assert store.ingest_many([root], sender="peer-a",
                             transport="fleet")["accepted"] == 1
    conflict = (
        trial(system="CYCLEKERNEL") if alias else trial(outcome="fail")
    )
    assert store.ingest_many([conflict], sender="peer-b",
                             transport="fleet")["quarantined"] == 1
    assert store.ingest_many([trial(attempt="later")], sender="peer-b",
                             transport="fleet"
                             )["results"][0]["status"] == "sender_quarantined"
    assert len(store.receipt_records()) == 3

    swap_receipt_event_ids(
        store, "conflict_quarantined", "sender_quarantined"
    )
    db = store._connect()
    try:
        for row in db.execute(
            "SELECT receipt_id,trial_id,sender,transport,bridge_run_id,"
            "result,payload_sha256,received_at FROM trial_receipts "
            "ORDER BY receipt_id"
        ):
            store._verify_receipt_row(db, row)
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="blocked receipt precedes first-conflict"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="blocked receipt precedes first-conflict"):
        TrialStore(path).receipt_records()
    assert len(store.quarantine_records()) == 1


@pytest.mark.parametrize("cap", [2, 4])
def test_pruned_first_seen_receipt_does_not_make_retained_duplicates_invalid(
    tmp_path, monkeypatch, cap,
):
    path = tmp_path / "private" / "trials.sqlite3"
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", cap)
        store = TrialStore(path)
        first = trial()
        assert store.ingest_many([first], sender="peer-a",
                                 transport="fleet")["accepted"] == 1
        for n in range(cap + 2):
            assert store.ingest_many([first], sender=f"peer-repeat-{n}",
                                     transport="fleet")["duplicates"] == 1
        records = store.receipt_records()
        assert len(records) == cap
        assert all(r["result"] == "duplicate" for r in records)
        assert TrialStore(path).receipt_records() == records


def test_pruned_first_conflict_receipt_allows_retained_blocked_receipts(
    tmp_path, monkeypatch,
):
    path = tmp_path / "private" / "trials.sqlite3"
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", 2)
        store = TrialStore(path)
        assert store.ingest_many([trial()], sender="peer-a",
                                 transport="fleet")["accepted"] == 1
        assert store.ingest_many([trial(outcome="fail")], sender="peer-b",
                                 transport="fleet")["quarantined"] == 1
        for n in range(3):
            assert store.ingest_many([trial(attempt=f"blocked-{n}")],
                                     sender="peer-b", transport="fleet"
                                     )["results"][0]["status"] == "sender_quarantined"
        records = store.receipt_records()
        assert len(records) == 2
        assert all(r["result"] == "sender_quarantined" for r in records)
        assert TrialStore(path).receipt_records() == records
        assert len(store.quarantine_records()) == 1


def test_ordered_replay_and_quarantine_history_remain_healthy(tmp_path):
    store, path = store_at(tmp_path)
    first = trial()
    assert store.ingest_many([first], sender="peer-a",
                             transport="fleet")["accepted"] == 1
    assert store.ingest_many([first], sender="peer-b",
                             transport="fleet")["duplicates"] == 1
    assert store.ingest_many([trial(outcome="fail")], sender="peer-c",
                             transport="fleet")["quarantined"] == 1
    assert store.ingest_many([trial(attempt="blocked")], sender="peer-c",
                             transport="fleet"
                             )["results"][0]["status"] == "sender_quarantined"
    assert [r["result"] for r in store.receipt_records()] == [
        "accepted", "duplicate", "conflict_quarantined", "sender_quarantined"
    ]
    assert TrialStore(path).receipt_records() == store.receipt_records()
