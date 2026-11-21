"""Round 192: quarantine sender markers must agree with source conflicts.

The marker controls authorization for future evidence deliveries. If it is
forged, missing, or stale, fail closed without minting new receipts or treating
a wrongfully blocked sender as evidence of a real conflict.
"""
from __future__ import annotations

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def _trial(attempt="base", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-192",
        check_id="sender-block", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def _store(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = _trial()
    rejected = _trial(outcome="fail")
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    result = store.ingest_many([rejected], sender="peer-b", transport="fleet")
    assert result["results"][0]["status"] == "conflict_quarantined"
    assert len(store.quarantine_records()) == 1
    return store, path


def _sql(store, query, parameters=()):
    db = store._connect()
    try:
        db.execute(query, parameters)
        db.commit()
    finally:
        db.close()


def _receipt_count(store):
    db = store._connect()
    try:
        return db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0]
    finally:
        db.close()


def _assert_both_operations_reject(store, sender="peer-b"):
    before = _receipt_count(store)
    with pytest.raises(TrialStoreError, match="sender quarantine state"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="sender quarantine state"):
        store.ingest_many([_trial("new")], sender=sender, transport="fleet")
    assert _receipt_count(store) == before


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("first_trial_id", "f" * 64, id="wrong-first-id"),
        pytest.param("reason", "forged conflict reason", id="wrong-reason"),
        pytest.param("quarantined_at", -1.0, id="wrong-time"),
        pytest.param("sender", "peer-c", id="wrong-sender"),
    ],
)
def test_tampered_quarantine_marker_cannot_authorize_or_deny_delivery(
    tmp_path, field, value,
):
    store, path = _store(tmp_path)
    _sql(store, f"UPDATE trial_quarantined_senders SET {field}=?", (value,))
    _assert_both_operations_reject(store)
    reopened = TrialStore(path)
    with pytest.raises(TrialStoreError, match="sender quarantine state"):
        reopened.quarantine_records()
    assert reopened.pool_snapshot()["runs"] == 1


def test_deleted_sender_marker_cannot_silently_allow_quarantined_sender(tmp_path):
    store, path = _store(tmp_path)
    _sql(store, "DELETE FROM trial_quarantined_senders WHERE sender='peer-b'")
    _assert_both_operations_reject(store)
    assert TrialStore(path).pool_snapshot()["runs"] == 1


def test_deleted_conflict_row_cannot_silently_leave_innocent_sender_blocked(tmp_path):
    store, _ = _store(tmp_path)
    _sql(store, "DELETE FROM trial_quarantine WHERE sender='peer-b'")
    _assert_both_operations_reject(store)


def test_forged_quarantine_marker_for_innocent_sender_is_refused(tmp_path):
    store, _ = _store(tmp_path)
    first_id = _trial()["trial_id"]
    _sql(store,
        "INSERT INTO trial_quarantined_senders(sender,first_trial_id,reason,quarantined_at) "
        "VALUES(?,?,?,?)",
        ("peer-c", first_id, "forged marker", 1.0),
    )
    _assert_both_operations_reject(store, sender="peer-c")
    # An independent peer's valid evidence path remains available; the
    # consistency guard is scoped to the sender on ingress.
    fresh = store.ingest_many([_trial("unrelated")], sender="peer-d", transport="fleet")
    assert fresh["accepted"] == 1


def test_duplicate_conflict_row_for_one_sender_is_treated_as_corrupt(tmp_path):
    store, _ = _store(tmp_path)
    _sql(store,
        "INSERT INTO trial_quarantine("
        "trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,"
        "incoming_json,received_at) "
        "SELECT trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,"
        "incoming_json,received_at FROM trial_quarantine LIMIT 1"
    )
    _assert_both_operations_reject(store)


def test_damaged_conflict_digest_invalidates_sender_marker_before_receipt(tmp_path):
    store, _ = _store(tmp_path)
    _sql(store, "UPDATE trial_quarantine SET incoming_sha256=?", ("f" * 64,))
    before = _receipt_count(store)
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.ingest_many([_trial("new")], sender="peer-b", transport="fleet")
    assert _receipt_count(store) == before


def test_two_independent_quarantined_senders_survive_reopen(tmp_path):
    store, path = _store(tmp_path)
    assert store.ingest_many([_trial("second")], sender="peer-a", transport="fleet")["accepted"] == 1
    result = store.ingest_many([_trial("second", "fail")], sender="peer-c", transport="fleet")
    assert result["quarantined"] == 1
    expected = store.quarantine_records()
    assert {r["sender"] for r in expected} == {"peer-b", "peer-c"}

    reopened = TrialStore(path)
    assert reopened.quarantine_records() == expected
    for sender in ("peer-b", "peer-c"):
        response = reopened.ingest_many([_trial(sender)], sender=sender, transport="fleet")
        assert response["results"][0]["status"] == "sender_quarantined"
    assert reopened.quarantine_records() == expected


def test_clean_nonquarantined_sender_and_replay_remain_unchanged(tmp_path):
    store, _ = _store(tmp_path)
    before = store.quarantine_records()
    third = _trial("third", "pass")
    first = store.ingest_many([third], sender="peer-c", transport="fleet")
    repeat = store.ingest_many([third], sender="peer-c", transport="fleet")
    assert first["accepted"] == 1 and repeat["duplicates"] == 1
    assert store.quarantine_records() == before
    assert store.pool_snapshot()["runs"] == 2
