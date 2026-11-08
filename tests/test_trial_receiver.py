from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing
from multiprocessing import get_context

import pytest

from avrone.trial_contract import make_trial, make_trial_outcome
from avrone.trial_receiver import TrialStore, verify_trial_receipt

RECEIPT_SECRET = "trial-receipt-test-secret-" + "r" * 40


def provenance(nonce="nonce-000000000001", sender="node-a"):
    return {
        "authenticated_sender": sender,
        "sender_claim": sender,
        "auth_method": "fleet_hmac_shared_token_allowlist",
        "auth_assurance": "shared_secret_group_member",
        "request_nonce": nonce,
        "request_timestamp": "1794000000",
        "request_body_sha256": "a" * 64,
        "bridge": None,
    }


def event(**overrides):
    values = dict(
        system="avrone", node="node-a", run_id="run-1", check_id="suite",
        check_version="7", attempt_id="attempt-1", failed=False,
        check_config={"profile": "strict"},
    )
    values.update(overrides)
    return make_trial(**values)


def delivery(record, bridge=None):
    return {"record": record, "bridge": bridge}


def _process_accept(path, record, index):
    store = TrialStore(path, RECEIPT_SECRET)
    return store.accept(record, provenance(f"process_nonce_{index:04d}"))["status"]


def test_persist_restart_and_stable_idempotent_receipt(tmp_path):
    store = TrialStore(tmp_path / "nested" / "trials.sqlite3", RECEIPT_SECRET)
    record = event()
    first = store.accept(record, provenance())
    reopened = TrialStore(store.path, RECEIPT_SECRET)
    again = reopened.accept(record, provenance("nonce-000000000002"))
    assert first["status"] == "accepted"
    assert again["status"] == "duplicate"
    assert first["receipt"]["receipt_id"] == again["receipt"]["receipt_id"]
    assert verify_trial_receipt(RECEIPT_SECRET, again["receipt"])
    assert reopened.counts() == {"records": 1, "deliveries": 2, "quarantine": 0, "poolable": 1}


def test_authenticated_sender_must_match_trial_source_node(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    record = event(node="node-a")
    with pytest.raises(ValueError, match="does not match trial node"):
        store.accept(record, provenance(sender="node-b"))
    assert store.counts() == {
        "records": 0, "deliveries": 0, "quarantine": 0, "poolable": 0,
    }


def test_sender_claim_agent_suffix_cannot_change_trial_source_node(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    record = event(node="node-a")
    proof = provenance(sender="node-a")
    proof["sender_claim"] = "node-a/worker-7"
    result = store.accept(record, proof)
    assert result["status"] == "accepted"
    assert store.get_record(record["trial_id"])["record"]["node"] == "node-a"


def test_conflicting_outcome_is_quarantined_without_overwriting_first_record(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    passed = event(failed=False)
    failed = event(failed=True)
    assert passed["trial_id"] == failed["trial_id"]
    first = store.accept(passed, provenance())
    conflict = store.accept(failed, provenance("nonce-000000000002"))
    saved = store.get_record(passed["trial_id"])
    assert first["status"] == "accepted"
    assert conflict["status"] == "conflict_quarantined"
    assert conflict["accepted"] is False
    assert conflict["quarantine_id"] is not None
    assert saved["record"]["failed"] is False
    assert saved["receipt"]["receipt_id"] == first["receipt"]["receipt_id"]
    assert store.counts()["quarantine"] == 1
    assert store.pool_snapshot()["runs"] == 1


def test_nonpass_outcomes_are_stored_but_never_projected_into_pool(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    outcomes = ["skipped", "cancelled", "timed_out", "inconclusive", "blocked"]
    events = []
    for index, outcome in enumerate(outcomes):
        record = make_trial_outcome(
            system="avrone", node="node-a", run_id="run-1", check_id="suite",
            check_version="7", attempt_id=f"nonpass-{index}", outcome=outcome,
        )
        events.append(delivery(record))
    transport = {k: v for k, v in provenance().items() if k != "bridge"}
    results = store.ingest_batch(events, transport)
    assert all(r["status"] == "operational_outcome_stored" for r in results)
    assert store.counts()["records"] == 5
    assert store.counts()["poolable"] == 0
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 0 and snapshot["evidence"] is None
    assert snapshot["independence"] == "not_assessed"


def test_batch_reordering_and_duplicate_delivery_are_idempotent(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    a = event(attempt_id="attempt-a")
    b = event(attempt_id="attempt-b", failed=True)
    transport = {k: v for k, v in provenance().items() if k != "bridge"}
    first = store.ingest_batch([delivery(a), delivery(b), delivery(a)], transport)
    second = store.ingest_batch([delivery(b), delivery(a)], transport)
    assert [r["status"] for r in first] == ["accepted", "accepted", "duplicate"]
    assert [r["status"] for r in second] == ["duplicate", "duplicate"]
    assert store.counts()["records"] == 2
    assert store.counts()["deliveries"] == 5


def test_concurrent_threads_and_independent_store_instances_write_once(tmp_path):
    path = tmp_path / "trials.sqlite3"
    record = event()

    def write(index):
        return TrialStore(path, RECEIPT_SECRET).accept(
            record, provenance(f"thread_nonce_{index:04d}")
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(write, range(24)))
    assert sum(r["status"] == "accepted" for r in results) == 1
    assert sum(r["status"] == "duplicate" for r in results) == 23
    assert len({r["receipt"]["receipt_id"] for r in results}) == 1
    assert TrialStore(path, RECEIPT_SECRET).counts()["records"] == 1


def test_independent_processes_share_one_idempotent_acceptance(tmp_path):
    path = str(tmp_path / "multiprocess.sqlite3")
    record = event()
    context = get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        statuses = list(pool.map(_process_accept, [path] * 8, [record] * 8, range(8)))
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 7
    assert TrialStore(path, RECEIPT_SECRET).counts()["records"] == 1


def test_bridge_and_transport_provenance_are_retained(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    record = event()
    transport = provenance()
    transport["bridge"] = {
        "schema": "avrone.bridge-trial/v1",
        "run_id": "run-1",
        "node_id": "node-a",
        "post_ids": ["post-1"],
    }
    result = store.accept(record, transport)
    assert result["status"] == "accepted"
    with closing(store._connect()) as db:
        row = db.execute("SELECT provenance_json FROM trial_deliveries").fetchone()
    import json
    saved = json.loads(bytes(row["provenance_json"]).decode())
    assert saved["authenticated_sender"] == "node-a"
    assert saved["auth_assurance"] == "shared_secret_group_member"
    assert saved["bridge"]["post_ids"] == ["post-1"]
    assert store.get_record(record["trial_id"])["record"]["check_version"] == "7"


def test_invalid_batch_is_rejected_before_any_write(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    good = event()
    bad = dict(good)
    bad["check_version"] = "tampered"
    transport = {k: v for k, v in provenance().items() if k != "bridge"}
    with pytest.raises(ValueError, match="contract"):
        store.ingest_batch([delivery(good), delivery(bad)], transport)
    assert store.counts()["records"] == 0


def test_interrupted_transaction_rolls_back_record_and_delivery_together(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    with closing(store._connect()) as db:
        db.execute("""
            CREATE TRIGGER abort_delivery BEFORE INSERT ON trial_deliveries
            BEGIN SELECT RAISE(ABORT, 'injected torn-write boundary'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.accept(event(), provenance())
    assert store.counts() == {"records": 0, "deliveries": 0, "quarantine": 0, "poolable": 0}


def test_corrupt_database_fails_closed(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"partial sqlite header")
    with pytest.raises(sqlite3.DatabaseError):
        TrialStore(path, RECEIPT_SECRET)


def test_receipt_tampering_is_detected(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    receipt = store.accept(event(), provenance())["receipt"]
    receipt["outcome"] = "failed"
    assert not verify_trial_receipt(RECEIPT_SECRET, receipt)


def test_batch_cap_is_enforced(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3", RECEIPT_SECRET)
    transport = {k: v for k, v in provenance().items() if k != "bridge"}
    with pytest.raises(ValueError, match="1..32"):
        store.ingest_batch([delivery(event(attempt_id=f"a-{i}")) for i in range(33)], transport)
