"""Regression cases for mutation between trial preflight and durable ingestion.

These tests model a caller retaining the same mutable envelope after handing it
to the receiver. Evidence must be committed from validated canonical bytes,
not from a later version of that caller-owned dictionary.
"""
from __future__ import annotations

import json

import pytest

from avrone.trial_contract import canonical_trial_bytes, make_trial
from avrone.trial_receiver import TrialStore


def _trial(attempt_id: str = "attempt-original", *, outcome: str = "pass"):
    return make_trial(
        system="cyclekernel",
        node="node-a",
        run_id="run-preflight",
        check_id="verify",
        check_version="v2",
        attempt_id=attempt_id,
        outcome=outcome,
        check_config={"mode": "strict", "seed": 186},
    )


def _mutate(envelope: dict, mutation: str):
    if mutation == "contradict_outcome":
        envelope["outcome"] = "fail"
    elif mutation == "change_source":
        envelope["source"] = "forged:node"
    elif mutation == "remove_required":
        envelope.pop("run_id")
    elif mutation == "replace_identity":
        envelope.clear()
        envelope.update(_trial("different-attempt", outcome="fail"))
    elif mutation == "insert_unexpected":
        envelope["verified"] = True
    else:
        raise AssertionError(f"unrecognized test mutation: {mutation}")


@pytest.mark.parametrize(
    "mutation",
    [
        "contradict_outcome",
        "change_source",
        "remove_required",
        "replace_identity",
        "insert_unexpected",
    ],
)
def test_mutation_after_preflight_never_changes_persisted_evidence(
    tmp_path, monkeypatch, mutation,
):
    store = TrialStore(tmp_path / "mutating-input.sqlite3")
    original = _trial()
    frozen = dict(original)
    original_bytes = canonical_trial_bytes(original)
    real_ingest_one = store._ingest_one

    def mutate_before_transaction(selected, raw, **kwargs):
        # Mutation occurs after ingest_many's validation/serialization pass.
        _mutate(original, mutation)
        return real_ingest_one(selected, raw, **kwargs)

    monkeypatch.setattr(store, "_ingest_one", mutate_before_transaction)
    result = store.ingest_many([original], sender="peer-a", transport="fleet")

    assert result["status"] == "accepted"
    assert result["accepted"] == 1
    assert result["results"] == [
        {"trial_id": frozen["trial_id"], "outcome": "pass", "status": "accepted"}
    ]

    db = store._connect()
    try:
        rows = db.execute(
            "SELECT trial_id,source,outcome,canonical_json FROM trials"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["trial_id"] == frozen["trial_id"]
        assert row["source"] == frozen["source"]
        assert row["outcome"] == "pass"
        assert bytes(row["canonical_json"]) == original_bytes
        assert json.loads(bytes(row["canonical_json"])) == frozen
        receipt = db.execute(
            "SELECT trial_id,result FROM trial_receipts"
        ).fetchone()
        assert (receipt["trial_id"], receipt["result"]) == (
            frozen["trial_id"], "accepted"
        )
    finally:
        db.close()

    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 1
    assert snapshot["failures"] == 0


def test_mutation_of_later_batch_member_during_earlier_commit_is_isolated(
    tmp_path, monkeypatch,
):
    store = TrialStore(tmp_path / "batch-mutation.sqlite3")
    first = _trial("first")
    second = _trial("second")
    expected_second = dict(second)
    second_bytes = canonical_trial_bytes(second)
    real_ingest_one = store._ingest_one

    def mutate_other_member(selected, raw, **kwargs):
        if selected["trial_id"] == first["trial_id"]:
            _mutate(second, "replace_identity")
        return real_ingest_one(selected, raw, **kwargs)

    monkeypatch.setattr(store, "_ingest_one", mutate_other_member)
    result = store.ingest_many([first, second], sender="peer-a", transport="fleet")

    assert result["status"] == "accepted"
    assert result["accepted"] == 2
    assert result["quarantined"] == 0
    assert result["results"][1] == {
        "trial_id": expected_second["trial_id"],
        "outcome": "pass",
        "status": "accepted",
    }
    db = store._connect()
    try:
        rows = db.execute(
            "SELECT trial_id,canonical_json,outcome FROM trials ORDER BY trial_id"
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 2
    preserved = next(row for row in rows if row["trial_id"] == expected_second["trial_id"])
    assert bytes(preserved["canonical_json"]) == second_bytes
    assert preserved["outcome"] == "pass"
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 2
    assert snapshot["failures"] == 0


def test_mutation_then_replay_preserves_original_identity_and_receipt(
    tmp_path, monkeypatch,
):
    path = tmp_path / "replay-after-mutation.sqlite3"
    store = TrialStore(path)
    original = _trial()
    expected = dict(original)
    real_ingest_one = store._ingest_one

    def replace_after_preflight(selected, raw, **kwargs):
        _mutate(original, "replace_identity")
        return real_ingest_one(selected, raw, **kwargs)

    monkeypatch.setattr(store, "_ingest_one", replace_after_preflight)
    first = store.ingest_many([original], sender="peer-a", transport="fleet")
    assert first["results"][0]["trial_id"] == expected["trial_id"]
    reopened = TrialStore(path)
    replay = reopened.ingest_many([expected], sender="peer-b", transport="fleet")
    assert replay["duplicates"] == 1
    assert replay["accepted"] == 0
    assert reopened.quarantine_records() == []
    assert reopened.pool_snapshot()["runs"] == 1
