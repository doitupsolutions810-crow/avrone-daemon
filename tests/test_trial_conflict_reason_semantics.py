"""Round 195: quarantined conflict explanation must be proven by the original.

An enum check for reason is not enough. The two legitimate classifications
have different proof obligations: exact trial_id with different canonical
payload, or canonical source/check/attempt alias with different trial_id.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from avrone.trial_contract import canonical_trial_bytes, make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


SAME = "same trial_id arrived with a different canonical envelope"
ALIAS = "canonical source/check/attempt identity arrived with a different trial_id"


def trial(*, system="grokbot", attempt="195", outcome="pass"):
    return make_trial(
        system=system, node="node-a", run_id="run-195",
        check_id="semantic-proof", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def conflicted(tmp_path, *, alias=False):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    incoming = trial(system="GROKBOT") if alias else trial(outcome="fail")
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([incoming], sender="peer-b", transport="fleet")["quarantined"] == 1
    assert store.quarantine_records()[0]["reason"] == (ALIAS if alias else SAME)
    return store, path, first, incoming


def patch(store, commands):
    db = store._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        for sql, args in commands:
            db.execute(sql, args)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("alias", [False, True], ids=["same-trial-id", "canonical-source-alias"])
def test_correct_conflict_reason_survives_audit_and_reopen(tmp_path, alias):
    store, path, first, incoming = conflicted(tmp_path, alias=alias)
    assert (incoming["trial_id"] != first["trial_id"]) is alias
    rows = store.quarantine_records()
    reopened = TrialStore(path)
    assert reopened.quarantine_records() == rows
    denied = reopened.ingest_many([trial(attempt="new")], sender="peer-b", transport="fleet")
    assert denied["results"][0]["status"] == "sender_quarantined"


@pytest.mark.parametrize("alias", [False, True], ids=["same-id-label-swapped", "alias-label-swapped"])
def test_forged_but_allowed_reason_is_refused_even_when_marker_matches(tmp_path, alias):
    store, path, _, _ = conflicted(tmp_path, alias=alias)
    forged = SAME if alias else ALIAS
    patch(store, [
        ("UPDATE trial_quarantine SET reason=?", (forged,)),
        ("UPDATE trial_quarantined_senders SET reason=?", (forged,)),
    ])
    with pytest.raises(TrialStoreError, match="conflict reason is inconsistent"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="conflict reason is inconsistent"):
        store.ingest_many([trial(attempt="fresh")], sender="peer-b", transport="fleet")
    with pytest.raises(TrialStoreError, match="conflict reason is inconsistent"):
        TrialStore(path).quarantine_records()


@pytest.mark.parametrize("alias", [False, True], ids=["original-same", "original-alias"])
def test_missing_original_cannot_prove_quarantine_reason(tmp_path, alias):
    store, path, _, _ = conflicted(tmp_path, alias=alias)
    patch(store, [("DELETE FROM trials", ())])
    with pytest.raises(TrialStoreError, match="lacks original evidence"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="lacks original evidence"):
        store.ingest_many([trial(attempt="fresh")], sender="peer-b", transport="fleet")


def test_original_trial_projection_corruption_invalidates_quarantine_proof(tmp_path):
    store, _, _, _ = conflicted(tmp_path)
    patch(store, [("UPDATE trials SET outcome='fail'", ())])
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.quarantine_records()


def test_quarantine_retargeted_to_unrelated_valid_envelope_fails_proof(tmp_path):
    store, _, _, _ = conflicted(tmp_path)
    unrelated = trial(system="unrelated", attempt="different")
    raw = canonical_trial_bytes(unrelated)
    patch(store, [
        ("UPDATE trial_quarantine SET trial_id=?,incoming_json=?,incoming_sha256=?",
         (unrelated["trial_id"], sqlite3.Binary(raw), hashlib.sha256(raw).hexdigest())),
        ("UPDATE trial_quarantined_senders SET first_trial_id=?", (unrelated["trial_id"],)),
    ])
    with pytest.raises(TrialStoreError, match="lacks original evidence"):
        store.quarantine_records()


@pytest.mark.parametrize("alias_system", ["GROKBOT", "Ｇｒｏｋｂｏｔ", "grokbot" + chr(0x200B)])
def test_equivalent_source_alias_conflicts_prove_original_identity(tmp_path, alias_system):
    store = TrialStore(tmp_path / "private" / "trials.sqlite3")
    first = trial()
    incoming = trial(system=alias_system)
    assert first["trial_id"] != incoming["trial_id"]
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([incoming], sender="peer-b", transport="fleet")["quarantined"] == 1
    assert store.quarantine_records()[0]["reason"] == ALIAS


def test_clean_pool_stays_unchanged_by_validated_conflict_and_sender_replay(tmp_path):
    store, path, first, _ = conflicted(tmp_path)
    before = store.pool_snapshot()
    replay = store.ingest_many([trial(attempt="other")], sender="peer-b", transport="fleet")
    assert replay["results"][0]["status"] == "sender_quarantined"
    assert store.pool_snapshot() == before
    reopened = TrialStore(path)
    assert reopened.pool_snapshot() == before
    assert len(reopened.quarantine_records()) == 1
