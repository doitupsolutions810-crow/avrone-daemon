"""Round 191: stored quarantine conflicts must validate before audit display."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from avrone.trial_contract import MAX_ENVELOPE_BYTES, canonical_trial_bytes, make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def _trial(*, outcome="pass", attempt_id="191"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-191",
        check_id="integrity", check_version="v2",
        attempt_id=attempt_id, outcome=outcome, check_config={"guard": True},
    )


def _conflicted(tmp_path, *, attempt_id="191", bridge=False):
    path = tmp_path / "private" / "trial.sqlite3"
    store = TrialStore(path)
    first = _trial(outcome="pass", attempt_id=attempt_id)
    incoming = _trial(outcome="fail", attempt_id=attempt_id)
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    args = {"sender": "peer-b", "transport": "fleet"}
    if bridge:
        args = {"sender": "peer-b", "transport": "bridge", "bridge_run_id": "bridge-191"}
    result = store.ingest_many([incoming], **args)
    assert result["quarantined"] == 1
    assert store.quarantine_records()[0]["trial_id"] == first["trial_id"]
    return store, path, first, incoming


def _patch(store, assignments, params=()):
    db = store._connect()
    try:
        db.execute("UPDATE trial_quarantine SET " + assignments, params)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "field,bad",
    [
        pytest.param("incoming_sha256", "f" * 64, id="wrong-digest"),
        pytest.param("trial_id", "f" * 64, id="wrong-trial-id"),
        pytest.param("sender", "../other-node", id="invalid-sender"),
        pytest.param("sender", "", id="empty-sender"),
        pytest.param("transport", "unknown", id="invalid-transport"),
    ],
)
def test_quarantine_metadata_tampering_is_never_reported(tmp_path, field, bad):
    store, path, first, _ = _conflicted(tmp_path)
    _patch(store, f"{field}=?", (bad,))

    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()
    reopened = TrialStore(path)
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        reopened.quarantine_records()
    assert store.pool_snapshot()["runs"] == 1
    assert store.pool_snapshot()["failures"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty-json"),
        pytest.param(b"not-json", id="malformed-json"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b'{"trial_id": NaN}', id="nonfinite-json"),
        pytest.param(b"Z" * (MAX_ENVELOPE_BYTES + 1), id="overlimit"),
        pytest.param(b"{}", id="missing-fields"),
    ],
)
def test_quarantine_payload_tampering_is_rejected_even_if_digest_updated(
    tmp_path, raw,
):
    store, _, _, _ = _conflicted(tmp_path)
    _patch(
        store,
        "incoming_json=?,incoming_sha256=?",
        (sqlite3.Binary(raw), hashlib.sha256(raw).hexdigest()),
    )
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()


def test_quarantine_noncanonical_json_does_not_pass_even_with_matching_digest(tmp_path):
    store, _, _, incoming = _conflicted(tmp_path)
    raw = json.dumps(incoming, sort_keys=True, indent=2).encode("utf-8")
    assert raw != canonical_trial_bytes(incoming)
    _patch(
        store,
        "incoming_json=?,incoming_sha256=?",
        (sqlite3.Binary(raw), hashlib.sha256(raw).hexdigest()),
    )
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()


def test_quarantine_reader_does_not_expose_raw_incoming_body(tmp_path):
    store, path, first, incoming = _conflicted(tmp_path, bridge=True)
    rows = store.quarantine_records()
    assert len(rows) == 1
    assert set(rows[0]) == {
        "trial_id", "sender", "transport", "bridge_run_id",
        "reason", "incoming_sha256", "received_at",
    }
    assert rows[0]["trial_id"] == incoming["trial_id"]
    assert rows[0]["transport"] == "bridge"
    assert rows[0]["bridge_run_id"] == "bridge-191"
    assert rows[0]["incoming_sha256"] == hashlib.sha256(
        canonical_trial_bytes(incoming)
    ).hexdigest()
    reopened = TrialStore(path)
    assert reopened.quarantine_records() == rows


def test_corrupt_later_quarantine_prevents_partial_audit_display(tmp_path):
    store, _, _, _ = _conflicted(tmp_path)
    second = _trial(attempt_id="second")
    second_conflict = _trial(outcome="fail", attempt_id="second")
    assert store.ingest_many([second], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([second_conflict], sender="peer-c", transport="fleet")["quarantined"] == 1
    assert len(store.quarantine_records()) == 2

    db = store._connect()
    try:
        db.execute(
            "UPDATE trial_quarantine SET incoming_sha256=? WHERE sender='peer-c'",
            ("0" * 64,),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="quarantine integrity"):
        store.quarantine_records()
    assert store.pool_snapshot()["runs"] == 2


def test_clean_conflict_stays_quarantined_across_restart_and_retry(tmp_path):
    store, path, first, incoming = _conflicted(tmp_path, bridge=True)
    original = store.quarantine_records()
    reopened = TrialStore(path)
    replay = reopened.ingest_many([incoming], sender="peer-b", transport="fleet")
    assert replay["results"][0]["status"] == "sender_quarantined"
    assert reopened.quarantine_records() == original
    assert reopened.pool_snapshot()["runs"] == 1
    assert reopened.pool_snapshot()["failures"] == 0
