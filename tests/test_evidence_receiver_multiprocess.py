"""Cross-process SQLite uniqueness tests for Great Scale evidence acceptance."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from avrone.evidence_receiver import EvidenceStore
from avrone.integration_contract import build_evidence_event

RECEIPT_SECRET = "process-receipt-secret-" + "r" * 40


def _accept_in_process(args):
    db_path, payload, nonce, now = args
    store = EvidenceStore(db_path)
    try:
        receipt, duplicate = store.accept(payload, nonce, now, RECEIPT_SECRET)
        return ("accepted", receipt["receipt_id"], duplicate)
    except KeyError:
        return ("identity-conflict", None, None)


def _event(failures=1):
    return build_evidence_event(
        source="multiprocess-contention",
        run_id="same-run",
        check_id="same-check",
        failures=failures,
        runs=10,
        refs=["test:multiprocess"],
    )


def test_cross_process_same_event_has_one_receipt(tmp_path):
    db_path = str(tmp_path / "shared-evidence.sqlite3")
    store = EvidenceStore(db_path)
    payload = _event()
    now = 1_800_000_000
    tasks = [
        (db_path, payload, f"processnonce{i:024d}", now)
        for i in range(8)
    ]
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_accept_in_process, tasks))

    assert all(result[0] == "accepted" for result in results), results
    receipt_ids = {result[1] for result in results}
    assert len(receipt_ids) == 1, results
    assert sum(not result[2] for result in results) == 1, results
    assert sum(result[2] for result in results) == 7, results
    assert store.count() == 1


def test_cross_process_identity_conflict_cannot_create_second_event(tmp_path):
    db_path = str(tmp_path / "shared-conflict.sqlite3")
    store = EvidenceStore(db_path)
    now = 1_800_000_000
    tasks = [
        (db_path, _event(1), "conflictnonce000000000000001", now),
        (db_path, _event(2), "conflictnonce000000000000002", now),
    ]
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_accept_in_process, tasks))

    assert sorted(result[0] for result in results) == [
        "accepted", "identity-conflict"
    ], results
    assert store.count() == 1
