"""Round 219: receipts and quarantine evidence cannot predate their causes.

A valid nonnegative finite time is not necessarily causally valid. A duplicate
receipt is later than its first-seen trial; a source/same-ID conflict is later
than the original canonical evidence; a blocked delivery is later than the
sender's quarantine. Reject local wall-clock rollback before writing those
events, and reject forged retained timestamps during audits. Independent new
trial evidence need not obey a global wall-clock monotonicity constraint.
"""
from __future__ import annotations

import pytest

from avrone import trial_receiver
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(outcome="pass", attempt="219", system="cyclekernel"):
    return make_trial(
        system=system, node="node-219", run_id="run-219",
        check_id="causal-chronology", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path, *, alias_conflict=False):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    assert store.ingest_many([first], sender="peer-a",
                             transport="fleet")["accepted"] == 1
    conflict = (
        trial(system="CYCLEKERNEL") if alias_conflict
        else trial(outcome="fail")
    )
    assert store.ingest_many(
        [conflict], sender="peer-b", transport="fleet"
    )["quarantined"] == 1
    assert len(store.quarantine_records()) == 1
    return store, path, first


def counts(store):
    db = store._connect()
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


def origins(store):
    db = store._connect()
    try:
        first_time = db.execute("SELECT received_at FROM trials").fetchone()[0]
        quarantined_time = db.execute(
            "SELECT quarantined_at FROM trial_quarantined_senders "
            "WHERE sender='peer-b'"
        ).fetchone()[0]
        return first_time, quarantined_time
    finally:
        db.close()


@pytest.mark.parametrize("rollback", [0.001, 1.0, 3600.0])
@pytest.mark.parametrize(
    "kind", [
        "duplicate", "same-id-conflict",
        "canonical-source-alias", "blocked-sender",
    ],
)
def test_clock_rollback_refuses_causally_impossible_delivery_without_partial_write(
    tmp_path, monkeypatch, kind, rollback,
):
    store, path, first = seeded(tmp_path)
    first_time, quarantine_time = origins(store)
    prior_counts = counts(store)
    prior_receipts = store.receipt_records()
    prior_quarantine = store.quarantine_records()
    prior_pool = store.pool_snapshot()

    if kind == "duplicate":
        incoming, sender, reference = first, "peer-c", first_time
    elif kind == "same-id-conflict":
        incoming, sender, reference = trial(outcome="fail"), "peer-c", first_time
    elif kind == "canonical-source-alias":
        incoming, sender, reference = trial(system="CYCLEKERNEL"), "peer-c", first_time
    else:
        incoming, sender, reference = trial(attempt="newer"), "peer-b", quarantine_time

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: reference - rollback)
        with pytest.raises(TrialStoreError, match="clock precedes"):
            store.ingest_many([incoming], sender=sender, transport="fleet")

    assert counts(store) == prior_counts
    assert store.receipt_records() == prior_receipts
    assert store.quarantine_records() == prior_quarantine
    assert store.pool_snapshot() == prior_pool
    assert TrialStore(path).pool_snapshot() == prior_pool


@pytest.mark.parametrize("status", ["duplicate", "sender_quarantined"])
@pytest.mark.parametrize("rollback", [1.0, 3600.0])
def test_tampered_receipt_timestamp_before_cause_is_rejected_on_audit(
    tmp_path, rollback, status,
):
    store, path, first = seeded(tmp_path)
    first_time, quarantine_time = origins(store)
    now = max(first_time, quarantine_time) + 1
    with pytest.MonkeyPatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: now)
        if status == "duplicate":
            assert store.ingest_many([first], sender="peer-c",
                                     transport="fleet")["duplicates"] == 1
        else:
            assert store.ingest_many([trial(attempt="blocked")],
                                     sender="peer-b", transport="fleet"
                                     )["results"][0]["status"] == "sender_quarantined"

    cause = first_time if status == "duplicate" else quarantine_time
    db = store._connect()
    try:
        db.execute(
            "UPDATE trial_receipts SET received_at=? WHERE result=?",
            (cause - rollback, status),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        TrialStore(path).receipt_records()
    assert store.pool_snapshot()["runs"] == 1


@pytest.mark.parametrize("alias_conflict", [False, True],
                         ids=["same-trial-id", "canonical-source-alias"])
def test_coherently_backdated_conflict_receipt_marker_and_payload_are_refused(
    tmp_path, alias_conflict,
):
    store, path, first = seeded(tmp_path, alias_conflict=alias_conflict)
    first_time, _ = origins(store)
    before_counts = counts(store)
    backdate = first_time - 5

    db = store._connect()
    try:
        # Keep the three conflict provenance fields perfectly consistent,
        # so old equality checks would not catch the forged chronology.
        db.execute("UPDATE trial_quarantine SET received_at=?", (backdate,))
        db.execute(
            "UPDATE trial_quarantined_senders SET quarantined_at=?", (backdate,)
        )
        db.execute(
            "UPDATE trial_receipts SET received_at=? "
            "WHERE result='conflict_quarantined'", (backdate,)
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="quarantine precedes first-seen"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="receipt integrity"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="quarantine precedes first-seen"):
        store.ingest_many([trial(attempt="another")], sender="peer-b",
                          transport="fleet")
    assert counts(store) == before_counts


@pytest.mark.parametrize("status", ["duplicate", "sender_quarantined"])
def test_equal_causal_timestamp_is_accepted_without_false_rollback(
    tmp_path, monkeypatch, status,
):
    store, path, first = seeded(tmp_path)
    first_time, quarantine_time = origins(store)
    cause = first_time if status == "duplicate" else quarantine_time
    incoming = first if status == "duplicate" else trial(attempt="blocked")
    sender = "peer-c" if status == "duplicate" else "peer-b"
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: cause)
        response = store.ingest_many(
            [incoming], sender=sender, transport="fleet"
        )

    assert response["results"][0]["status"] == status
    assert store.receipt_records()[-1]["received_at"] == cause
    assert len(TrialStore(path).receipt_records()) == 3


def test_independent_first_seen_trial_may_precede_other_trial_wall_clock(
    tmp_path, monkeypatch,
):
    store, path, first = seeded(tmp_path)
    first_time, quarantine_time = origins(store)
    before = counts(store)
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: first_time - 10)
        response = store.ingest_many(
            [trial(attempt="independent")], sender="peer-c", transport="fleet"
        )
    assert response["accepted"] == 1
    assert counts(store) == (before[0] + 1, before[1] + 1, before[2], before[3])
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_valid_forward_chronology_preserves_duplicate_conflict_and_blocked_audit(
    tmp_path, monkeypatch,
):
    store, path, first = seeded(tmp_path)
    first_time, quarantine_time = origins(store)
    forward = max(first_time, quarantine_time) + 20
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: forward)
        assert store.ingest_many([first], sender="peer-c",
                                 transport="fleet")["duplicates"] == 1
        assert store.ingest_many([trial(outcome="fail")], sender="peer-d",
                                 transport="fleet")["quarantined"] == 1
        assert store.ingest_many([trial(attempt="blocked")], sender="peer-b",
                                 transport="fleet"
                                 )["results"][0]["status"] == "sender_quarantined"
    assert len(store.quarantine_records()) == 2
    assert [row["result"] for row in store.receipt_records()] == [
        "accepted", "conflict_quarantined", "duplicate",
        "conflict_quarantined", "sender_quarantined",
    ]
    assert len(TrialStore(path).quarantine_records()) == 2

@pytest.mark.parametrize(
    "kind", ["same-id-conflict", "canonical-source-alias"],
)
def test_equal_original_timestamp_is_valid_for_distinct_conflict_sender(
    tmp_path, monkeypatch, kind,
):
    store, path, first = seeded(tmp_path)
    first_time, _ = origins(store)
    incoming = (
        trial(outcome="fail") if kind == "same-id-conflict"
        else trial(system="CYCLEKERNEL")
    )
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: first_time)
        response = store.ingest_many(
            [incoming], sender="peer-c", transport="fleet"
        )

    assert response["quarantined"] == 1
    assert store.quarantine_records()[-1]["received_at"] == first_time
    assert store.receipt_records()[-1]["received_at"] == first_time
    assert len(TrialStore(path).quarantine_records()) == 2
