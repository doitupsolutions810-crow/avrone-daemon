"""Round 194: sender-block state must remain atomic under faults, contention and restart."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="194", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-194", run_id="run-194",
        check_id="atomicity", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    assert store.ingest_many([trial()], sender="peer-a", transport="fleet")["accepted"] == 1
    return store, path


def counts(store):
    db = store._connect()
    try:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
            "trials", "trial_quarantine", "trial_quarantined_senders", "trial_receipts"
        ))
    finally:
        db.close()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("injected application failure"), id="runtime-failure"),
        pytest.param(sqlite3.IntegrityError("injected sqlite constraint"), id="sqlite-failure"),
    ],
)
def test_failure_after_conflict_marker_insertion_rolls_back_both_rows(tmp_path, monkeypatch, error):
    store, _ = seeded(tmp_path)
    before = counts(store)

    def fail_receipt(*args, **kwargs):
        raise error

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_record_receipt", fail_receipt)
        expected = TrialStoreError if isinstance(error, sqlite3.Error) else RuntimeError
        with pytest.raises(expected):
            store.ingest_many([trial(outcome="fail")], sender="peer-b", transport="fleet")

    assert counts(store) == before
    assert store.quarantine_records() == []
    assert store.pool_snapshot()["runs"] == 1
    accepted = store.ingest_many([trial(outcome="fail")], sender="peer-b", transport="fleet")
    assert accepted["quarantined"] == 1
    assert counts(store) == (1, 1, 1, before[3] + 1)


def test_distinct_concurrent_conflicting_senders_each_get_one_consistent_marker(tmp_path):
    store, path = seeded(tmp_path)
    senders = [f"peer-{i}" for i in range(1, 6)]
    def attempt(sender):
        return store.ingest_many([trial(outcome="fail")], sender=sender, transport="fleet")["results"][0]["status"]

    with ThreadPoolExecutor(max_workers=len(senders)) as pool:
        statuses = list(pool.map(attempt, senders))

    assert statuses == ["conflict_quarantined"] * len(senders)
    assert {row["sender"] for row in store.quarantine_records()} == set(senders)
    assert counts(store) == (1, len(senders), len(senders), 1 + len(senders))
    assert TrialStore(path).quarantine_records() == store.quarantine_records()


def test_single_concurrent_conflicting_sender_has_only_one_first_marker(tmp_path):
    store, _ = seeded(tmp_path)
    def attempt(_):
        return store.ingest_many(
            [trial(outcome="fail")], sender="peer-b", transport="fleet"
        )["results"][0]["status"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(12)))

    assert statuses.count("conflict_quarantined") == 1
    assert statuses.count("sender_quarantined") == 11
    assert counts(store) == (1, 1, 1, 13)
    assert len(store.quarantine_records()) == 1


def test_wal_checkpoint_restart_keeps_block_and_conflict_consistent(tmp_path):
    store, path = seeded(tmp_path)
    assert store.ingest_many(
        [trial(outcome="fail")], sender="peer-b", transport="fleet"
    )["quarantined"] == 1
    db = store._connect()
    try:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
    reopened = TrialStore(path)
    assert len(reopened.quarantine_records()) == 1
    blocked = reopened.ingest_many([trial("new")], sender="peer-b", transport="fleet")
    assert blocked["results"][0]["status"] == "sender_quarantined"
    assert reopened.pool_snapshot()["runs"] == 1


def test_receipt_pruning_cannot_prune_sender_block_evidence(tmp_path, monkeypatch):
    from avrone import trial_receiver

    store, path = seeded(tmp_path)
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_RECEIPTS", 2)
        assert store.ingest_many(
            [trial(outcome="fail")], sender="peer-b", transport="fleet"
        )["quarantined"] == 1
        for idx in range(6):
            result = store.ingest_many([trial(f"replay-{idx}")], sender="peer-b", transport="fleet")
            assert result["results"][0]["status"] == "sender_quarantined"

    assert counts(store) == (1, 1, 1, 2)
    assert len(TrialStore(path).quarantine_records()) == 1


def test_same_batch_first_conflict_blocks_sender_on_second_envelope(tmp_path):
    store, _ = seeded(tmp_path)
    first = trial(outcome="fail")
    second = trial("new-attempt", "pass")
    response = store.ingest_many([first, second], sender="peer-b", transport="fleet")

    assert [r["status"] for r in response["results"]] == [
        "conflict_quarantined", "sender_quarantined"
    ]
    assert counts(store) == (1, 1, 1, 3)
    assert store.pool_snapshot()["runs"] == 1


def test_second_batch_row_receipt_failure_does_not_leave_orphan_marker(tmp_path, monkeypatch):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    real_record = store._record_receipt
    writes = 0

    def fail_second(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("injected batch second-receipt fault")
        return real_record(*args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(store, "_record_receipt", fail_second)
        with pytest.raises(RuntimeError, match="second-receipt"):
            store.ingest_many(
                [trial(outcome="pass"), trial(outcome="fail")],
                sender="peer-a", transport="fleet"
            )

    # One transaction per envelope: the first row commits, the second rolls
    # back every side effect including its attempted conflict and sender block.
    assert counts(store) == (1, 0, 0, 1)
    assert store.quarantine_records() == []
    assert TrialStore(path).pool_snapshot()["runs"] == 1
