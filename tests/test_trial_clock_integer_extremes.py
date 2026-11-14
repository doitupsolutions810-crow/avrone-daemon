"""Round 209: enormous time.time() integers and coercive subclasses fail closed.

Python's math.isfinite(10**1000) raises OverflowError, while SQLite rejects
integers outside the signed 64-bit range on insertion. A receiver clock must
neither leak those unnormalized exceptions nor corrupt a persisted receipt.
"""
from __future__ import annotations

import pytest

from avrone import trial_receiver
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


class TrapInt(int):
    def __lt__(self, other):
        raise AssertionError("untrusted int subclass comparison executed")


class TrapFloat(float):
    def __lt__(self, other):
        raise AssertionError("untrusted float subclass comparison executed")


def trial(attempt="209", outcome="pass", system="cyclekernel"):
    return make_trial(
        system=system, node="node-209", run_id="run-209",
        check_id="clock-extremes", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([trial(outcome="fail")],
                             sender="peer-b", transport="fleet")["quarantined"] == 1
    return store, path, first


def totals(store):
    db = store._connect()
    try:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in (
                         "trials", "trial_receipts", "trial_quarantine",
                         "trial_quarantined_senders",
                     ))
    finally:
        db.close()


INVALID_EXTREMES = [
    pytest.param(1 << 63, id="just-above-sqlite-int64"),
    pytest.param(1 << 64, id="unsigned-64bit"),
    pytest.param(10 ** 100, id="huge-positive-int"),
    pytest.param(10 ** 1000, id="float-conversion-overflow"),
    pytest.param(-(10 ** 1000), id="huge-negative-int"),
    pytest.param(TrapInt(1700000000), id="coercive-int-subclass"),
    pytest.param(TrapFloat(1700000000.0), id="coercive-float-subclass"),
]


@pytest.mark.parametrize("clock_reading", INVALID_EXTREMES)
@pytest.mark.parametrize(
    "scenario", ["first-seen", "duplicate", "conflict", "alias", "blocked"],
)
def test_extreme_clock_rejected_for_all_ingest_results_without_mutation(
    tmp_path, monkeypatch, clock_reading, scenario,
):
    store, path, first = seeded(tmp_path)
    before = totals(store)
    snapshot = store.pool_snapshot()
    quarantine = store.quarantine_records()
    receipts = store.receipt_records()
    payload = {
        "first-seen": trial("new"),
        "duplicate": first,
        "conflict": trial(outcome="fail"),
        "alias": trial(system="CYCLEKERNEL"),
        "blocked": trial("blocked"),
    }[scenario]
    sender = "peer-b" if scenario == "blocked" else "peer-c"
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: clock_reading)
        with pytest.raises(TrialStoreError, match="invalid receipt timestamp"):
            store.ingest_many([payload], sender=sender, transport="fleet")
    assert totals(store) == before
    assert store.pool_snapshot() == snapshot
    assert store.quarantine_records() == quarantine
    assert store.receipt_records() == receipts
    assert TrialStore(path).pool_snapshot() == snapshot


def test_second_envelope_overflow_does_not_create_partial_second_receipt(
    tmp_path, monkeypatch,
):
    store, path, _ = seeded(tmp_path)
    before = totals(store)
    readings = iter([1700000000.0, 10 ** 1000])

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: next(readings))
        with pytest.raises(TrialStoreError, match="invalid receipt timestamp"):
            store.ingest_many(
                [trial("first-in-batch"), trial("second-in-batch")],
                sender="peer-c", transport="fleet",
            )

    after = totals(store)
    # Receiver guarantees one transaction per envelope. First commits, second
    # fails before opening SQLite; no extra receipt or sender block can appear.
    assert after == (before[0] + 1, before[1] + 1, before[2], before[3])
    assert store.pool_snapshot()["runs"] == 2
    assert TrialStore(path).pool_snapshot()["runs"] == 2


def test_positive_builtin_clock_int_remains_idempotent(tmp_path, monkeypatch):
    store, path, _ = seeded(tmp_path)
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: 1700000000)
        assert store.ingest_many(
            [trial("good")], sender="peer-c", transport="fleet"
        )["accepted"] == 1

    assert store.receipt_records()[-1]["received_at"] == 1700000000
    assert store.ingest_many(
        [trial("good")], sender="peer-c", transport="fleet"
    )["duplicates"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 2
