"""Round 207: invalid wall clock readings must not enter persisted evidence.

SQLite converts float NaN to SQL NULL. The receiver's receipt time is included
in trial provenance and sender quarantine markers; a malformed clock must not
produce accepted, duplicate, rejected or quarantined delivery receipts.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from avrone import trial_receiver
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def envelope(attempt="clock-207", *, system="cyclekernel", outcome="pass"):
    return make_trial(
        system=system, node="node-clock", run_id="run-clock",
        check_id="clock", check_version="v2", attempt_id=attempt,
        outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    original = envelope()
    assert store.ingest_many([original], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([envelope(outcome="fail")], sender="peer-b",
                             transport="fleet")["quarantined"] == 1
    return store, path, original


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


INVALID_READINGS = [
    pytest.param(float("nan"), id="nan-sqlite-null"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(-0.001, id="negative-fraction"),
    pytest.param(-1, id="negative-integer"),
    pytest.param(None, id="none"),
    pytest.param("1700000000", id="numeric-string"),
    pytest.param(Decimal("1700000000"), id="decimal-object"),
    pytest.param(True, id="boolean-true"),
    pytest.param(False, id="boolean-false"),
]


@pytest.mark.parametrize("reading", INVALID_READINGS)
@pytest.mark.parametrize(
    "scenario",
    ["new", "duplicate", "same-id-conflict", "source-alias", "sender-blocked"],
)
def test_bad_clock_refuses_every_receipt_status_without_any_partial_write(
    tmp_path, monkeypatch, reading, scenario,
):
    store, path, original = seeded(tmp_path)
    before = counts(store)
    snapshot = store.pool_snapshot()
    quarantine = store.quarantine_records()
    receipts = store.receipt_records()
    sender = "peer-b" if scenario == "sender-blocked" else "peer-c"
    payload = {
        "new": envelope("second"),
        "duplicate": original,
        "same-id-conflict": envelope(outcome="fail"),
        "source-alias": envelope(system="CYCLEKERNEL"),
        "sender-blocked": envelope("blocked"),
    }[scenario]

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: reading)
        with pytest.raises(TrialStoreError, match="invalid receipt timestamp"):
            store.ingest_many([payload], sender=sender, transport="fleet")

    assert counts(store) == before
    assert store.pool_snapshot() == snapshot
    assert store.quarantine_records() == quarantine
    assert store.receipt_records() == receipts
    assert TrialStore(path).pool_snapshot() == snapshot


@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(OSError("clock unavailable"), id="oserror"),
        pytest.param(OverflowError("clock overflow"), id="overflow"),
        pytest.param(ValueError("invalid clock"), id="value-error"),
    ],
)
@pytest.mark.parametrize("sender", ["peer-b", "peer-c"])
def test_clock_source_errors_preserve_previous_evidence(
    tmp_path, monkeypatch, exception, sender,
):
    store, path, original = seeded(tmp_path)
    before = counts(store)

    def raise_clock_error():
        raise exception

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", raise_clock_error)
        with pytest.raises(TrialStoreError, match="receiver clock could not provide"):
            store.ingest_many([envelope("clock-failed")], sender=sender, transport="fleet")

    assert counts(store) == before
    assert len(TrialStore(path).quarantine_records()) == 1


@pytest.mark.parametrize("timestamp", [0, 1, 1700000000, 1700000000.125])
def test_finite_nonnegative_clock_is_representable_across_all_provenance(
    tmp_path, monkeypatch, timestamp,
):
    store, path, _ = seeded(tmp_path)
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.time, "time", lambda: timestamp)
        result = store.ingest_many(
            [envelope("healthy", outcome="skipped")],
            sender="peer-c", transport="bridge", bridge_run_id="clock-207",
        )
    assert result["non_pass"] == 1
    records = store.non_pass_records()
    assert len(records) == 1
    assert records[0]["received_at"] == timestamp
    assert records[0]["bridge_run_id"] == "clock-207"
    assert store.receipt_records()[-1]["received_at"] == timestamp
    assert TrialStore(path).pool_snapshot()["runs"] == 1
