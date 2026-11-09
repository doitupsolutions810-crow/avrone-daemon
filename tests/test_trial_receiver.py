import threading
import time
from contextlib import closing

import pytest

from avrone.bridge import Bridge
from avrone.trial_contract import make_trial
from avrone.trial_receiver import (
    TrialStore,
    ingest_authenticated_bridge_posts,
    ingest_bridge_posts,
    post_trial_to_bridge,
    strict_json_loads,
)


def sample(**overrides):
    values = dict(
        system="grok-bot", node="node-a", run_id="run-31", check_id="reasoning-regression",
        check_version="2026.11", attempt_id="attempt-1", outcome="pass",
        check_config={"model": "test-model", "temperature": 0},
    )
    values.update(overrides)
    return make_trial(**values)


def test_store_is_idempotent_and_preserves_source_check_version_and_sender(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    record = sample()
    first = store.accept(record, "sender-a", "fleet", "nonce-1", received_at=100)
    again = TrialStore(store.path).accept(record, "sender-b", "fleet", "nonce-2", received_at=101)
    assert first["status"] == "accepted"
    assert again["status"] == "duplicate"
    assert first["receipt_id"] == again["receipt_id"]
    assert store.count() == 1
    saved = store.get_trial(record["trial_id"])
    assert saved["source"] == "grok-bot:node-a"
    assert saved["check_version"] == "2026.11"
    assert saved["envelope"]["attempt_id"] == "attempt-1"
    deliveries = store.deliveries_for(record["trial_id"])
    assert [d["authenticated_sender"] for d in deliveries] == ["sender-a", "sender-b"]


def test_conflicting_outcome_is_quarantined_without_overwriting_first_record(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    passed, failed = sample(outcome="pass"), sample(outcome="fail")
    assert passed["trial_id"] == failed["trial_id"]
    first = store.accept(passed, "sender-a", "fleet", "nonce-1")
    conflict = store.accept(failed, "sender-a", "fleet", "nonce-2")
    assert first["status"] == "accepted"
    assert conflict["status"] == "quarantined"
    assert store.count() == 1 and store.quarantine_count() == 1
    assert store.get_trial(passed["trial_id"])["envelope"]["outcome"] == "pass"


def test_repeated_conflict_reuses_one_quarantine_record(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    passed, failed = sample(outcome="pass"), sample(outcome="fail")
    store.accept(passed, "sender-a", "fleet", "nonce-1")
    first = store.accept(failed, "sender-a", "fleet", "nonce-2")
    second = store.accept(failed, "sender-a", "fleet", "nonce-3")
    assert first["status"] == second["status"] == "quarantined"
    assert first["quarantine_id"] == second["quarantine_id"]
    assert store.quarantine_count() == 1
    assert store.delivery_count() == 3
    assert store.quarantine_rows()[0]["reason"] == "same_trial_id_different_payload"


def test_non_pass_outcomes_are_stored_but_excluded_from_pool(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    result = store.accept(sample(outcome="timed_out"), "sender-a", "fleet", "nonce-timeout")
    assert result["status"] == "stored_non_pass"
    assert result["poolable"] is False
    report = store.pool_snapshot()
    assert report["runs"] == report["failures"] == 0
    assert report["independence"] == "not_assessed"


def test_delivery_order_does_not_change_pool_snapshot(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    records = [sample(run_id=f"run-{n}", attempt_id=f"attempt-{n}",
                      outcome="fail" if n % 2 else "pass") for n in range(12)]
    for record in records:
        store.accept(record, "sender-a", "fleet", f"nonce-a-{record['run_id']}")
    first = store.pool_snapshot()
    for record in reversed(records):
        store.accept(record, "sender-b", "bridge", f"post-{record['run_id']}")
    assert store.pool_snapshot() == first
    assert first["runs"] == 12
    assert first["independence"] == "not_assessed"


def test_restart_recovers_committed_rows_and_duplicate_receipt(tmp_path):
    path = tmp_path / "trial_evidence.sqlite3"
    record = sample()
    first = TrialStore(path).accept(record, "sender-a", "fleet", "nonce-1")
    reopened = TrialStore(path)
    again = reopened.accept(record, "sender-a", "fleet", "nonce-2")
    assert again["status"] == "duplicate"
    assert again["receipt_id"] == first["receipt_id"]
    assert reopened.count() == 1


def test_uncommitted_partial_transaction_is_rolled_back_on_close(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO deliveries(trial_id,authenticated_sender,transport,transport_id,"
            "claimed_origin,received_at,result) VALUES (?,?,?,?,?,?,?)",
            ("a" * 64, "sender-a", "fleet", "partial", None, int(time.time()), "accepted"),
        )
    reopened = TrialStore(store.path)
    assert reopened.count() == 0
    assert reopened.delivery_count() == 0


def test_concurrent_same_trial_commits_once_and_records_each_delivery(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    record = sample()
    barrier = threading.Barrier(12)
    results, errors = [], []

    def worker(index):
        try:
            barrier.wait()
            results.append(store.accept(record, "sender-a", "fleet", f"nonce-{index}"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert sum(result["status"] == "accepted" for result in results) == 1
    assert sum(result["status"] == "duplicate" for result in results) == 11
    assert len({result["receipt_id"] for result in results}) == 1
    assert store.count() == 1
    assert store.delivery_count() == 12


def test_bridge_adapter_keeps_authenticated_sender_separate_from_claimed_origin(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    bridge = Bridge("run-bridge", data_dir=None, node_id="local-node")
    record = sample()
    post = post_trial_to_bridge(bridge, record)
    post["origin_node"] = "forged-origin"
    summary = ingest_authenticated_bridge_posts(store, [post], "relay-node", {"relay-node"})
    assert summary["accepted"] == 1
    delivery = store.deliveries_for(record["trial_id"])[0]
    assert delivery["authenticated_sender"] == "relay-node"
    assert delivery["claimed_origin"] == "forged-origin"


def test_bridge_adapter_rejects_missing_or_unallowlisted_authenticated_sender(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    bridge = Bridge("run-bridge", data_dir=None)
    post = post_trial_to_bridge(bridge, sample())
    with pytest.raises(PermissionError, match="allowlisted"):
        ingest_authenticated_bridge_posts(store, [post], "sender-a", set())
    with pytest.raises(PermissionError, match="allowlisted"):
        ingest_authenticated_bridge_posts(store, [post], "sender-a", {"sender-b"})


def test_bridge_adapter_rejects_duplicate_key_and_malformed_posts(tmp_path):
    store = TrialStore(tmp_path / "trial_evidence.sqlite3")
    summary = ingest_bridge_posts(store, [
        {"kind": "evidence", "id": "p1", "text": '{"a":1,"a":2}'},
        {"kind": "evidence", "id": "p2", "text": '{"schema":NaN}'},
        {"kind": "evidence", "id": "p3", "text": "[" * 2000 + "0" + "]" * 2000},
        {"kind": "finding", "id": "p4", "text": "not evidence"},
    ], "sender-a")
    assert summary["rejected"] == 3
    assert store.count() == 0


def test_strict_json_rejects_non_utf8_duplicate_keys_nonfinite_and_oversize():
    for raw in (b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}', b" " * (64 * 1024 + 1)):
        with pytest.raises(ValueError):
            strict_json_loads(raw)
