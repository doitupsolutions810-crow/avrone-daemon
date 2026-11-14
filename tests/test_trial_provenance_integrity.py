"""Round 193: persisted sender, bridge metadata, reason and timestamp must be sane.

Canonical trial bytes do not contain receiver-authenticated delivery provenance.
Readers must not silently display malformed SQLite metadata as legitimate.
These syntax/consistency tests do not replace external signed attestations.
"""
from __future__ import annotations

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def _trial(outcome="pass", attempt="193"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-193",
        check_id="provenance", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def _store(tmp_path, transport="fleet", outcome="pass"):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    args = {"sender": "peer-a", "transport": transport}
    if transport == "bridge":
        args["bridge_run_id"] = "bridge-193"
    result = store.ingest_many([_trial(outcome=outcome)], **args)
    assert result["results"][0]["status"] in {"accepted", "non_pass_stored"}
    return store, path


def _conflict(tmp_path, transport="fleet"):
    store, path = _store(tmp_path)
    args = {"sender": "peer-b", "transport": transport}
    if transport == "bridge":
        args["bridge_run_id"] = "bridge-193"
    assert store.ingest_many([_trial("fail")], **args)["quarantined"] == 1
    assert len(store.quarantine_records()) == 1
    return store, path


def _tamper(store, table, field, value):
    assert table in {"trials", "trial_quarantine"}
    assert field in {
        "first_sender", "first_transport", "sender", "transport",
        "bridge_run_id", "received_at", "reason",
    }
    db = store._connect()
    try:
        db.execute(f"UPDATE {table} SET {field}=?", (value,))
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("first_sender", "../peer", id="traversal-like-sender"),
        pytest.param("first_sender", "", id="empty-sender"),
        pytest.param("first_transport", "other", id="unsupported-transport"),
        pytest.param("bridge_run_id", "not-allowed-on-fleet", id="bridge-in-fleet"),
        pytest.param("received_at", -3.0, id="negative-timestamp"),
        pytest.param("received_at", "yesterday", id="text-timestamp"),
        pytest.param("received_at", float("inf"), id="nonfinite-timestamp"),
    ],
)
def test_trial_provenance_corruption_blocks_pool_and_replay(tmp_path, field, value):
    store, path = _store(tmp_path)
    _tamper(store, "trials", field, value)
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.ingest_many([_trial()], sender="peer-c", transport="fleet")
    reopened = TrialStore(path)
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        reopened.non_pass_records()


@pytest.mark.parametrize(
    "invalid_id",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param(" \t ", id="whitespace-control"),
        pytest.param("x" * 129, id="overlong"),
        pytest.param(37, id="not-text"),
    ],
)
def test_stored_bridge_id_must_stay_valid(tmp_path, invalid_id):
    store, _ = _store(tmp_path, transport="bridge")
    _tamper(store, "trials", "bridge_run_id", invalid_id)
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("reason", "made up reason", id="forged-reason"),
        pytest.param("bridge_run_id", "not-a-fleet-bridge", id="bridge-on-fleet"),
        pytest.param("received_at", -1, id="negative-conflict-time"),
        pytest.param("received_at", "bad", id="text-conflict-time"),
        pytest.param("received_at", float("inf"), id="nonfinite-conflict-time"),
    ],
)
def test_quarantine_provenance_corruption_blocks_audit_and_sender_ingest(
    tmp_path, field, value,
):
    store, _ = _conflict(tmp_path)
    _tamper(store, "trial_quarantine", field, value)
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.ingest_many([_trial(attempt="later")], sender="peer-b", transport="fleet")


@pytest.mark.parametrize(
    "invalid_bridge_id",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("x" * 129, id="too-long"),
        pytest.param("bad\nbridge", id="control-character"),
    ],
)
def test_quarantine_bridge_metadata_drift_is_rejected(tmp_path, invalid_bridge_id):
    store, _ = _conflict(tmp_path, transport="bridge")
    _tamper(store, "trial_quarantine", "bridge_run_id", invalid_bridge_id)
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()


def test_clean_fleet_and_bridge_provenance_survive_restart(tmp_path):
    store, path = _store(tmp_path, transport="bridge")
    first = _trial("pass")
    assert store.ingest_many([first], sender="peer-a", transport="bridge", bridge_run_id="bridge-193")["duplicates"] == 1
    failed = _trial(outcome="fail", attempt="later")
    assert store.ingest_many([failed], sender="peer-c", transport="fleet")["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 2
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 2


def test_clean_bridge_quarantine_provenance_survives_restart(tmp_path):
    store, path = _conflict(tmp_path, transport="bridge")
    before = store.quarantine_records()
    reopened = TrialStore(path)
    assert reopened.quarantine_records() == before
    blocked = reopened.ingest_many([_trial(attempt="later")], sender="peer-b", transport="fleet")
    assert blocked["results"][0]["status"] == "sender_quarantined"
