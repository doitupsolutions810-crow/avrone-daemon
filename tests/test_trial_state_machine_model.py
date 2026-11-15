"""Round 221: deterministic state-machine model against real SQLite evidence.

The oracle is independent of database rows: it records the expected outcomes
of a sequence of ingests, conflicts, blocked senders, and restarts, then
compares all public evidence surfaces after every step. Tiny receipt windows
exercise pruning, while different interleavings reveal interactions between
cardinality, sender quarantine, causal timestamps and read-only audits.
"""
from __future__ import annotations

import itertools
import random

import pytest

from avrone import trial_receiver
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore


def trial(attempt, outcome="pass", system="cyclekernel"):
    return make_trial(
        system=system, node="node-model", run_id="run-model",
        check_id="state-machine", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def other_outcome(outcome):
    return "fail" if outcome == "pass" else "pass"


@pytest.mark.parametrize("seed", [1, 13, 41, 97])
@pytest.mark.parametrize("receipt_cap", [5, 11])
def test_seeded_ingest_model_remains_consistent_through_pruning_and_restart(
    tmp_path, monkeypatch, seed, receipt_cap,
):
    rng = random.Random(seed)
    path = tmp_path / "private" / "trials.sqlite3"
    counter = itertools.count(1_700_000_000)
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", receipt_cap)
        ctx.setattr(trial_receiver.time, "time", lambda: next(counter))
        store = TrialStore(path)
        originals = []
        blocked_senders = []
        expected_receipts = []
        expected_non_pass = 0
        expected_pool = 0
        next_attempt = 0
        events = 0

        for step in range(60):
            # Do not use model bookkeeping to decide status after seeing
            # the receiver result. Compute the expected transition first.
            choices = ["new", "new", "duplicate", "same-id-conflict",
                       "source-alias-conflict", "restart"]
            if blocked_senders:
                choices += ["blocked", "blocked"]
            if not originals:
                action = "new"
            else:
                action = rng.choice(choices)

            if action == "restart":
                store = TrialStore(path)
                assert [r["result"] for r in store.receipt_records()] == (
                    expected_receipts[-receipt_cap:]
                )
                continue

            if action == "new":
                next_attempt += 1
                outcome = rng.choice(("pass", "fail", "skipped"))
                envelope = trial(f"attempt-{next_attempt}", outcome)
                originals.append(envelope)
                sender = "peer-main"
                expected = "non_pass_stored" if outcome == "skipped" else "accepted"
                if outcome == "skipped":
                    expected_non_pass += 1
                else:
                    expected_pool += 1
            elif action == "duplicate":
                envelope = rng.choice(originals)
                sender = "peer-replay"
                expected = "duplicate"
            elif action in {"same-id-conflict", "source-alias-conflict"}:
                original = rng.choice(originals)
                if action == "same-id-conflict":
                    envelope = trial(
                        original["attempt_id"], other_outcome(original["outcome"]),
                    )
                else:
                    envelope = trial(
                        original["attempt_id"], original["outcome"],
                        system="CYCLEKERNEL",
                    )
                sender = f"peer-conflict-{len(blocked_senders) + 1}"
                blocked_senders.append(sender)
                expected = "conflict_quarantined"
            else:
                sender = rng.choice(blocked_senders)
                next_attempt += 1
                envelope = trial(f"blocked-{next_attempt}")
                expected = "sender_quarantined"

            response = store.ingest_many(
                [envelope], sender=sender, transport="fleet"
            )
            actual = response["results"][0]["status"]
            assert actual == expected, (
                f"seed={seed}, step={step}, action={action}, "
                f"sender={sender}, expected={expected}, actual={actual}"
            )
            expected_receipts.append(expected)
            events += 1

            # The bounded receipt window may not contain the first-seen
            # receipt anymore. Repeated legitimate duplicates and blocked
            # deliveries must still pass retained cardinality validation.
            receipt_records = store.receipt_records()
            assert [r["result"] for r in receipt_records] == (
                expected_receipts[-receipt_cap:]
            ), (seed, step, "receipts")

            quarantines = store.quarantine_records()
            assert {row["sender"] for row in quarantines} == set(blocked_senders), (
                seed, step, "quarantine"
            )
            assert len(quarantines) == len(blocked_senders)
            assert len(store.non_pass_records()) == expected_non_pass
            assert store.pool_snapshot()["runs"] == expected_pool

        reopened = TrialStore(path)
        assert [r["result"] for r in reopened.receipt_records()] == (
            expected_receipts[-receipt_cap:]
        )
        assert len(reopened.quarantine_records()) == len(blocked_senders)
        assert reopened.pool_snapshot()["runs"] == expected_pool
        assert len(reopened.non_pass_records()) == expected_non_pass
        db = reopened._connect()
        try:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("PRAGMA query_only").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM trial_receipts").fetchone()[0] == (
                min(events, receipt_cap)
            )
        finally:
            db.close()


@pytest.mark.parametrize("outcome", ["pass", "fail", "skipped"])
def test_single_sender_batch_orders_first_seen_duplicate_conflict_then_quarantine(
    tmp_path, outcome,
):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    initial = trial("single-batch", outcome)
    conflicting = trial("single-batch", other_outcome(outcome))
    response = store.ingest_many(
        [initial, initial, conflicting],
        sender="peer-batch", transport="fleet",
    )
    first_status = "non_pass_stored" if outcome == "skipped" else "accepted"
    assert [r["status"] for r in response["results"]] == [
        first_status, "duplicate", "conflict_quarantined"
    ]
    assert response["status"] == "partial"
    assert len(store.quarantine_records()) == 1
    assert [r["result"] for r in store.receipt_records()] == [
        first_status, "duplicate", "conflict_quarantined"
    ]
    subsequent = store.ingest_many(
        [trial("later")], sender="peer-batch", transport="fleet"
    )
    assert subsequent["results"][0]["status"] == "sender_quarantined"
    assert [r["result"] for r in TrialStore(path).receipt_records()][-1] == (
        "sender_quarantined"
    )


@pytest.mark.parametrize("cap", [2, 5])
def test_long_duplicate_burst_after_pruning_preserves_one_trial_and_all_retained_receipts(
    tmp_path, monkeypatch, cap,
):
    path = tmp_path / "private" / "trials.sqlite3"
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", cap)
        store = TrialStore(path)
        original = trial("many-duplicates")
        assert store.ingest_many([original], sender="peer-original",
                                 transport="fleet")["accepted"] == 1
        for index in range(30):
            assert store.ingest_many(
                [original], sender=f"peer-duplicate-{index}",
                transport="fleet",
            )["duplicates"] == 1
        receipts = store.receipt_records()
        assert len(receipts) == cap
        assert all(r["result"] == "duplicate" for r in receipts)
        assert store.pool_snapshot()["runs"] == 1
        assert TrialStore(path).receipt_records() == receipts


def test_many_distinct_conflicting_senders_can_share_one_original_trial(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    root = trial("single-root")
    assert store.ingest_many([root], sender="peer-original",
                             transport="fleet")["accepted"] == 1
    conflict = trial("single-root", "fail")
    for index in range(16):
        assert store.ingest_many(
            [conflict], sender=f"peer-conflict-{index}", transport="fleet"
        )["quarantined"] == 1
    assert len(store.quarantine_records()) == 16
    receipts = store.receipt_records()
    assert len(receipts) == 17
    assert sum(r["result"] == "conflict_quarantined" for r in receipts) == 16
    assert store.pool_snapshot()["runs"] == 1
