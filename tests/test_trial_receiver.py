from concurrent.futures import ThreadPoolExecutor

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore


def sample(outcome="pass", attempt_id="attempt-1"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-8", check_id="verify",
        check_version="10.2", attempt_id=attempt_id, outcome=outcome,
        check_config={"profile": "airgap"},
    )


def test_identical_delivery_is_idempotent_and_pool_snapshot_is_stable(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    envelope = sample()
    first = store.ingest_many([envelope], sender="peer-a", transport="fleet")
    second = store.ingest_many([envelope], sender="peer-a", transport="fleet")
    assert first["results"][0]["status"] == "accepted"
    assert second["results"][0]["status"] == "duplicate"
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 1 and snapshot["failures"] == 0
    assert snapshot["independence"] == "not_assessed"


def test_non_pass_outcomes_are_stored_but_never_enter_pool(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    envelopes = [
        sample("skipped", "a"), sample("cancelled", "b"),
        sample("timed_out", "c"), sample("inconclusive", "d"),
    ]
    result = store.ingest_many(envelopes, sender="peer-a", transport="fleet")
    assert result["non_pass"] == 4
    assert {row["outcome"] for row in store.non_pass_records()} == {
        "skipped", "cancelled", "timed_out", "inconclusive",
    }
    assert store.pool_snapshot()["runs"] == 0
    assert store.pool_snapshot()["evidence"] is None


def test_conflicting_outcome_quarantines_sender_and_preserves_first_record(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    passed = sample("pass")
    failed = sample("fail")
    assert passed["trial_id"] == failed["trial_id"]
    assert store.ingest_many([passed], sender="peer-a", transport="fleet")["accepted"] == 1
    conflict = store.ingest_many([failed], sender="peer-b", transport="bridge", bridge_run_id="bridge-1")
    assert conflict["results"][0]["status"] == "conflict_quarantined"
    assert store.quarantine_records()[0]["sender"] == "peer-b"
    again = store.ingest_many([failed], sender="peer-b", transport="fleet")
    assert again["results"][0]["status"] == "sender_quarantined"
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 1 and snapshot["failures"] == 0


def test_reordered_deliveries_do_not_change_the_pool(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    records = [sample("pass", "a"), sample("fail", "b"), sample("skipped", "c")]
    store.ingest_many(records, sender="peer-a", transport="fleet")
    before = store.pool_snapshot()
    store.ingest_many(list(reversed(records)), sender="peer-a", transport="fleet")
    after = store.pool_snapshot()
    assert before == after
    assert after["runs"] == 2 and after["failures"] == 1


def test_store_survives_close_and_reopen(tmp_path):
    path = tmp_path / "trials.sqlite3"
    TrialStore(path).ingest_many([sample()], sender="peer-a", transport="fleet")
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 1


def test_partial_receipt_failure_rolls_back_trial_insert(tmp_path, monkeypatch):
    path = tmp_path / "trials.sqlite3"
    store = TrialStore(path)

    def fail_receipt(*args, **kwargs):
        raise RuntimeError("injected receipt write failure")

    monkeypatch.setattr(store, "_record_receipt", fail_receipt)
    with pytest.raises(RuntimeError, match="injected"):
        store.ingest_many([sample()], sender="peer-a", transport="fleet")
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 0


def test_concurrent_duplicate_writes_commit_one_trial(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    envelope = sample()

    def ingest(_):
        return store.ingest_many([envelope], sender="peer-a", transport="fleet")["results"][0]["status"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(ingest, range(36)))
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 35
    assert store.pool_snapshot()["runs"] == 1


def test_malformed_envelopes_and_transport_metadata_are_rejected(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    with pytest.raises(ValueError):
        store.ingest_many([{"trial_id": "x"}], sender="peer-a", transport="fleet")
    with pytest.raises(ValueError):
        store.ingest_many([sample()], sender="../peer", transport="fleet")
    with pytest.raises(ValueError):
        store.ingest_many([sample()], sender="peer-a", transport="unknown")


def test_source_and_check_version_provenance_are_persisted(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    envelope = sample()
    store.ingest_many([envelope], sender="peer-a", transport="fleet")
    db = store._connect()
    try:
        row = db.execute(
            "SELECT system,node,source,run_id,check_id,check_version,"
            "check_config_sha256,attempt_id,first_sender,first_transport FROM trials"
        ).fetchone()
    finally:
        db.close()
    assert row["system"] == "cyclekernel"
    assert row["node"] == "node-a"
    assert row["source"] == "cyclekernel:node-a"
    assert row["check_version"] == "10.2"
    assert row["check_config_sha256"] == envelope["check_config_sha256"]
    assert row["attempt_id"] == "attempt-1"
    assert row["first_sender"] == "peer-a"
    assert row["first_transport"] == "fleet"
