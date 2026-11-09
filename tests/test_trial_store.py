import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_store import MAX_TRIAL_BATCH, TrialStore, TrialStoreError


def trial(outcome="passed", attempt="attempt-1", check="bridge-contract"):
    return make_trial(
        system="cyclekernel",
        node="node-a",
        run_id="run-1",
        check_id=check,
        check_version="v7",
        attempt_id=attempt,
        outcome=outcome,
        check_config={"mode": "offline", "limit": 4},
    )


def receive(store, rows, peer="peer-a", request_id="request-1"):
    return store.receive_batch(rows, peer=peer, transport="fleet_hmac_v1", request_id=request_id)


def test_exact_retry_is_idempotent_and_preserves_receipt_provenance(tmp_path):
    store = TrialStore(tmp_path)
    row = trial()
    first = receive(store, [row])
    retry = receive(store, [row], request_id="request-2")
    assert first["status"] == "accepted" and first["accepted"] == 1
    assert retry["status"] == "duplicate" and retry["duplicates"] == 1
    assert store.snapshot()["runs"] == 1
    saved = store.get_trial(row["trial_id"])
    assert saved["source"] == "cyclekernel:node-a"
    assert saved["check_version"] == "v7"
    assert saved["first_peer"] == "peer-a"
    assert saved["first_transport"] == "fleet_hmac_v1"


def test_same_trial_id_with_conflicting_outcome_is_quarantined(tmp_path):
    store = TrialStore(tmp_path)
    passed, failed = trial("passed"), trial("failed")
    assert passed["trial_id"] == failed["trial_id"]
    assert receive(store, [passed])["accepted"] == 1
    conflict = receive(store, [failed])
    assert conflict["status"] == "conflict_quarantined"
    assert conflict["conflicts"] == 1 and conflict["quarantined"] is True
    assert store.get_trial(passed["trial_id"])["envelope"]["outcome"] == "passed"
    assert store.peer_quarantined("peer-a")
    blocked = receive(store, [passed], request_id="after-quarantine")
    assert blocked["status"] == "quarantined" and blocked["accepted"] == 0


def test_quarantine_survives_restart_and_does_not_block_another_peer(tmp_path):
    first = TrialStore(tmp_path)
    passed, failed = trial("passed"), trial("failed")
    receive(first, [passed])
    receive(first, [failed])
    restarted = TrialStore(tmp_path)
    assert restarted.peer_quarantined("peer-a")
    assert receive(restarted, [passed], peer="peer-b")["status"] == "duplicate"


def test_reordered_batches_and_repeated_batches_do_not_change_counts(tmp_path):
    store = TrialStore(tmp_path)
    rows = [trial(attempt="a"), trial(attempt="b"), trial(attempt="c")]
    first = receive(store, list(reversed(rows)))
    second = receive(store, rows, request_id="reordered")
    assert first["accepted"] == 3
    assert second["duplicates"] == 3
    assert store.snapshot()["runs"] == 3


def test_non_pass_outcomes_are_stored_but_excluded_from_pool(tmp_path):
    store = TrialStore(tmp_path)
    rows = [trial(outcome=x, attempt=x) for x in ("skipped", "cancelled", "timed_out", "inconclusive")]
    result = receive(store, rows)
    assert result["accepted"] == 4 and result["operational"] == 4 and result["pooled"] == 0
    snapshot = store.snapshot()
    assert snapshot["runs"] == 0 and snapshot["evidence"] is None
    assert snapshot["independence"] == "not_assessed"


def test_malformed_records_are_explicitly_rejected(tmp_path):
    store = TrialStore(tmp_path)
    result = receive(store, [None, {"schema": "avrone.trial/v1"}])
    assert result["status"] == "rejected"
    assert result["rejected"] == 2 and result["accepted"] == 0


def test_source_and_check_version_survive_reordered_relay(tmp_path):
    store = TrialStore(tmp_path)
    row = trial()
    receive(store, [row], peer="peer-b")
    saved = store.get_trial(row["trial_id"])
    assert saved["source"] == row["source"]
    assert saved["check_version"] == row["check_version"]
    assert saved["envelope"]["run_id"] == row["run_id"]


def test_concurrent_same_trial_deliveries_commit_once(tmp_path):
    store = TrialStore(tmp_path)
    row = trial()

    def submit(index):
        return receive(store, [row], peer=f"peer-{index}", request_id=f"r-{index}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(submit, range(12)))
    assert sum(result["accepted"] for result in results) == 1
    assert sum(result["duplicates"] for result in results) == 11
    assert store.snapshot()["runs"] == 1


def test_transaction_failure_rolls_back_the_whole_batch(tmp_path):
    store = TrialStore(tmp_path)
    first, second = trial(attempt="first"), trial(attempt="second", check="abort-check")
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("""
            CREATE TRIGGER fail_second BEFORE INSERT ON trials
            WHEN NEW.check_id='abort-check'
            BEGIN SELECT RAISE(ABORT, 'simulated torn transaction'); END
        """)
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(TrialStoreError, match="no acceptance"):
        receive(store, [first, second])
    assert store.get_trial(first["trial_id"]) is None
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("DROP TRIGGER fail_second")
        conn.commit()
    finally:
        conn.close()
    assert receive(store, [first, second])["accepted"] == 2


def test_corrupt_or_torn_database_fails_closed_without_reset(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    path = root / "trials.sqlite3"
    damaged = b"partial-sqlite-header"
    path.write_bytes(damaged)
    with pytest.raises(TrialStoreError, match="not reset"):
        TrialStore(tmp_path)
    assert path.read_bytes() == damaged


def test_batch_limit_and_request_id_limit_are_enforced(tmp_path):
    store = TrialStore(tmp_path)
    rows = [trial(attempt=str(i)) for i in range(MAX_TRIAL_BATCH + 1)]
    with pytest.raises(ValueError, match="too many trials"):
        receive(store, rows)
    with pytest.raises(ValueError, match="request_id"):
        receive(store, [trial()], request_id="x" * 129)


def test_empty_batch_is_explicit_and_does_not_create_pool_evidence(tmp_path):
    store = TrialStore(tmp_path)
    result = receive(store, [])
    assert result["status"] == "empty"
    assert result["accepted"] == result["duplicates"] == result["rejected"] == 0
    assert store.snapshot()["evidence"] is None


def test_independence_is_never_inferred_from_distinct_ids(tmp_path):
    store = TrialStore(tmp_path)
    receive(store, [trial(attempt="a"), trial(attempt="b")])
    assert store.snapshot()["independence"] == "not_assessed"
    assert store.integrity_check() == "ok"
