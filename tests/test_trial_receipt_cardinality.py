"""Round 220: retained receipts must have unambiguous first-delivery proofs.

Every receipt row can be authentic in isolation yet yield an impossible audit:
two accepted/non_pass_stored receipts for the same first-seen trial, or two
conflict_quarantined receipts for one sender. Both violate TrialStore's
first-seen and sender quarantine semantics, even though individual hashes,
timestamps and sender provenance may match the trusted trial evidence.
"""
from __future__ import annotations

import pytest

from avrone import trial_receiver
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="220", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-220", run_id="run-220",
        check_id="receipt-cardinality", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    skipped = trial("skipped", "skipped")
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([skipped], sender="peer-a", transport="fleet")["non_pass"] == 1
    assert store.ingest_many([trial(outcome="fail")], sender="peer-b",
                             transport="fleet")["quarantined"] == 1
    assert len(store.receipt_records()) == 3
    return store, path, first, skipped


def counts(store):
    db = store._connect()
    try:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                     for name in ("trials", "trial_receipts",
                                  "trial_quarantine", "trial_quarantined_senders"))
    finally:
        db.close()


def clone_receipts(store, *, result, copies):
    """Clone otherwise-valid retained events with distinct AUTOINCREMENT IDs."""
    db = store._connect()
    try:
        for _ in range(copies):
            db.execute(
                "INSERT INTO trial_receipts("
                "trial_id,sender,transport,bridge_run_id,result,payload_sha256,received_at"
                ") SELECT trial_id,sender,transport,bridge_run_id,result,payload_sha256,received_at "
                "FROM trial_receipts WHERE result=? ORDER BY receipt_id LIMIT 1",
                (result,),
            )
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("result", ["accepted", "non_pass_stored"])
@pytest.mark.parametrize("copies", [1, 2, 4])
def test_cloned_valid_first_seen_receipts_fail_complete_audit(
    tmp_path, result, copies,
):
    store, path, first, skipped = seeded(tmp_path)
    before = counts(store)
    clone_receipts(store, result=result, copies=copies)
    after = counts(store)
    assert after == (before[0], before[1] + copies, before[2], before[3])

    # Each retained row is valid in isolation, but the two "first" delivery
    # claims for one trial cannot both be true.
    db = store._connect()
    try:
        for row in db.execute(
            "SELECT receipt_id,trial_id,sender,transport,bridge_run_id,"
            "result,payload_sha256,received_at FROM trial_receipts "
            "WHERE result=? ORDER BY receipt_id", (result,),
        ):
            store._verify_receipt_row(db, row)
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="duplicate first-seen trial receipt"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="duplicate first-seen trial receipt"):
        TrialStore(path).receipt_records()
    assert store.pool_snapshot()["runs"] == 1
    assert len(store.quarantine_records()) == 1


@pytest.mark.parametrize("copies", [1, 2, 4])
def test_multiple_valid_first_conflict_receipts_for_same_sender_are_refused(
    tmp_path, copies,
):
    store, path, first, skipped = seeded(tmp_path)
    prior = counts(store)
    clone_receipts(store, result="conflict_quarantined", copies=copies)
    assert counts(store) == (prior[0], prior[1] + copies, prior[2], prior[3])

    db = store._connect()
    try:
        for row in db.execute(
            "SELECT receipt_id,trial_id,sender,transport,bridge_run_id,"
            "result,payload_sha256,received_at FROM trial_receipts "
            "WHERE result='conflict_quarantined' ORDER BY receipt_id"
        ):
            store._verify_receipt_row(db, row)
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="duplicate first-conflict sender receipt"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="duplicate first-conflict sender receipt"):
        TrialStore(path).receipt_records()
    assert len(store.quarantine_records()) == 1


def test_multiple_legitimate_duplicate_receipts_do_not_fail_first_seen_cardinality(
    tmp_path,
):
    store, path, first, skipped = seeded(tmp_path)
    for sender in ("peer-c", "peer-d", "peer-e"):
        assert store.ingest_many(
            [first], sender=sender, transport="fleet"
        )["duplicates"] == 1
    receipts = store.receipt_records()
    assert len([r for r in receipts if r["result"] == "accepted"]) == 1
    assert len([r for r in receipts if r["result"] == "duplicate"]) == 3
    assert len(TrialStore(path).receipt_records()) == 6


def test_first_seen_receipt_may_be_pruned_while_repeated_duplicate_receipts_remain(
    tmp_path, monkeypatch,
):
    path = tmp_path / "private" / "trials.sqlite3"
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", 2)
        store = TrialStore(path)
        first = trial()
        assert store.ingest_many([first], sender="peer-a",
                                 transport="fleet")["accepted"] == 1
        assert store.ingest_many([first], sender="peer-b",
                                 transport="fleet")["duplicates"] == 1
        assert store.ingest_many([first], sender="peer-c",
                                 transport="fleet")["duplicates"] == 1
        receipts = store.receipt_records()
        assert len(receipts) == 2
        assert all(record["result"] == "duplicate" for record in receipts)
        assert len(TrialStore(path).receipt_records()) == 2
    assert store.pool_snapshot()["runs"] == 1


def test_distinct_first_conflict_senders_are_legitimate_with_one_shared_original(
    tmp_path,
):
    store, path, first, skipped = seeded(tmp_path)
    assert store.ingest_many([trial(outcome="fail")],
                             sender="peer-c", transport="fleet")["quarantined"] == 1
    receipts = store.receipt_records()
    assert [r["sender"] for r in receipts
            if r["result"] == "conflict_quarantined"] == ["peer-b", "peer-c"]
    assert len(store.quarantine_records()) == 2
    assert len(TrialStore(path).receipt_records()) == 4


def test_multiple_blocked_deliveries_from_same_sender_are_allowed(tmp_path):
    store, path, first, skipped = seeded(tmp_path)
    for attempt in ("blocked-1", "blocked-2", "blocked-3"):
        result = store.ingest_many(
            [trial(attempt=attempt)],
            sender="peer-b", transport="fleet",
        )
        assert result["results"][0]["status"] == "sender_quarantined"
    receipts = store.receipt_records()
    assert sum(r["result"] == "sender_quarantined" for r in receipts) == 3
    assert sum(r["result"] == "conflict_quarantined" for r in receipts) == 1
    assert len(TrialStore(path).quarantine_records()) == 1
