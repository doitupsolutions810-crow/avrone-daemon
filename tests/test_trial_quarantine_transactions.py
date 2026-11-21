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


# Round 223: real process death at SQLite transaction boundaries. These tests
# deliberately use spawn (no inherited SQLite handles) and SIGKILL (no Python
# finally/rollback handlers), then reopen the database in a fresh receiver.

import multiprocessing
import os
import signal


def _hold_child_at_crash_point(reached, release):
    reached.set()
    # The parent kills this process after observing the exact transaction
    # boundary. This timeout is only a dead-parent safety valve, not a test
    # synchronization primitive.
    release.wait(60)
    os._exit(86)


def _crash_before_commit_worker(
    path, payloads, sender, point, target_call, reached, release,
):
    store = TrialStore(path)
    original_record = store._record_receipt
    calls = 0

    def intercepted_record(db, **kwargs):
        nonlocal calls
        calls += 1
        if calls == target_call and point == "before_receipt":
            _hold_child_at_crash_point(reached, release)
        original_record(db, **kwargs)
        if calls == target_call and point == "after_receipt":
            _hold_child_at_crash_point(reached, release)

    store._record_receipt = intercepted_record
    store.ingest_many(payloads, sender=sender, transport="fleet")


def _crash_after_commit_worker(path, payload, sender, reached, release):
    original_connect = sqlite3.connect
    armed = False

    class CommitPauseConnection(sqlite3.Connection):
        def commit(self):
            super().commit()
            if armed and not reached.is_set():
                # The durable commit has returned; the caller has not yet
                # received a response. Killing here tests retry-after-unknown-
                # response semantics rather than transaction rollback.
                _hold_child_at_crash_point(reached, release)

    def connect(*args, **kwargs):
        return original_connect(*args, factory=CommitPauseConnection, **kwargs)

    sqlite3.connect = connect
    store = TrialStore(path)
    armed = True
    store.ingest_many([payload], sender=sender, transport="fleet")


def _crash_during_migration_worker(path, reached, release):
    original_connect = sqlite3.connect

    class MigrationPauseConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            cursor = super().execute(sql, *args)
            if (
                sql.lstrip().upper().startswith(
                    "UPDATE TRIALS SET SOURCE_IDENTITY_KEY"
                )
                and not reached.is_set()
            ):
                # ALTER TABLE and one backfill update have executed but remain
                # inside _initialize's single uncommitted migration transaction.
                _hold_child_at_crash_point(reached, release)
            return cursor

    def connect(*args, **kwargs):
        return original_connect(*args, factory=MigrationPauseConnection, **kwargs)

    sqlite3.connect = connect
    TrialStore(path)


def _kill_at_event(target, args):
    context = multiprocessing.get_context("spawn")
    reached = context.Event()
    release = context.Event()
    process = context.Process(target=target, args=(*args, reached, release))
    process.start()
    if not reached.wait(20):
        exitcode = process.exitcode
        if process.is_alive():
            process.terminate()
        process.join(10)
        pytest.fail(f"child did not reach requested crash point (exitcode={exitcode})")
    process.kill()
    process.join(10)
    assert not process.is_alive()
    assert process.exitcode == -signal.SIGKILL


def _raw_counts_and_integrity(path):
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "trials", "trial_quarantine",
                "trial_quarantined_senders", "trial_receipts",
            )
        )
    finally:
        db.close()


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash-recovery test requires POSIX")
@pytest.mark.parametrize("point", ["before_receipt", "after_receipt"])
def test_sigkill_rolls_back_first_seen_trial_and_receipt_as_one_unit(
    tmp_path, point,
):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    payload = trial("crash-first-seen")
    _kill_at_event(
        _crash_before_commit_worker,
        (str(path), [payload], "peer-crash", point, 1),
    )

    reopened = TrialStore(path)
    assert _raw_counts_and_integrity(path) == (0, 0, 0, 0)
    assert reopened.receipt_records() == []
    assert reopened.pool_snapshot()["runs"] == 0
    assert reopened.non_pass_records() == []

    first = reopened.ingest_many([payload], sender="peer-crash", transport="fleet")
    assert first["accepted"] == 1
    replay = reopened.ingest_many([payload], sender="peer-crash", transport="fleet")
    assert replay["duplicates"] == 1
    assert _raw_counts_and_integrity(path) == (1, 0, 0, 2)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash-recovery test requires POSIX")
@pytest.mark.parametrize("point", ["before_receipt", "after_receipt"])
def test_sigkill_rolls_back_conflict_quarantine_and_sender_block_together(
    tmp_path, point,
):
    store, path = seeded(tmp_path)
    conflicting = trial(outcome="fail")
    _kill_at_event(
        _crash_before_commit_worker,
        (str(path), [conflicting], "peer-crash", point, 1),
    )

    reopened = TrialStore(path)
    assert _raw_counts_and_integrity(path) == (1, 0, 0, 1)
    assert reopened.quarantine_records() == []
    assert [row["result"] for row in reopened.receipt_records()] == ["accepted"]

    conflict = reopened.ingest_many(
        [conflicting], sender="peer-crash", transport="fleet"
    )
    assert conflict["quarantined"] == 1
    blocked = reopened.ingest_many(
        [trial("after-block")], sender="peer-crash", transport="fleet"
    )
    assert blocked["results"][0]["status"] == "sender_quarantined"
    assert _raw_counts_and_integrity(path) == (1, 1, 1, 3)
    assert len(reopened.quarantine_records()) == 1


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash-recovery test requires POSIX")
def test_sigkill_second_batch_envelope_preserves_only_prior_committed_envelope(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    TrialStore(path)
    first = trial("batch-first-committed")
    second = trial("batch-second-interrupted")
    _kill_at_event(
        _crash_before_commit_worker,
        (str(path), [first, second], "peer-batch", "after_receipt", 2),
    )

    reopened = TrialStore(path)
    assert _raw_counts_and_integrity(path) == (1, 0, 0, 1)
    assert [row["trial_id"] for row in reopened.receipt_records()] == [
        first["trial_id"]
    ]
    assert reopened.pool_snapshot()["runs"] == 1

    replay = reopened.ingest_many(
        [first, second], sender="peer-batch", transport="fleet"
    )
    assert [row["status"] for row in replay["results"]] == [
        "duplicate", "accepted"
    ]
    assert _raw_counts_and_integrity(path) == (2, 0, 0, 3)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash-recovery test requires POSIX")
def test_sigkill_after_commit_before_response_replays_as_duplicate(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    TrialStore(path)
    payload = trial("commit-before-response", outcome="skipped")
    _kill_at_event(
        _crash_after_commit_worker,
        (str(path), payload, "peer-commit"),
    )

    reopened = TrialStore(path)
    assert _raw_counts_and_integrity(path) == (1, 0, 0, 1)
    assert reopened.non_pass_records()[0]["trial_id"] == payload["trial_id"]
    assert reopened.pool_snapshot()["runs"] == 0
    replay = reopened.ingest_many(
        [payload], sender="peer-commit", transport="fleet"
    )
    assert replay["duplicates"] == 1
    assert _raw_counts_and_integrity(path) == (1, 0, 0, 2)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash-recovery test requires POSIX")
def test_sigkill_during_legacy_backfill_rolls_back_schema_and_retries_migration(tmp_path):
    store, path = seeded(tmp_path)
    db = store._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP TRIGGER IF EXISTS trial_source_identity_key_required_insert")
        db.execute("DROP TRIGGER IF EXISTS trial_source_identity_key_required_update")
        db.execute("DROP INDEX trial_source_identity_key")
        db.execute("ALTER TABLE trials DROP COLUMN source_identity_key")
        db.execute("PRAGMA user_version=1")
        db.commit()
    finally:
        db.close()

    _kill_at_event(_crash_during_migration_worker, (str(path),))
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row[1] for row in db.execute("PRAGMA table_info(trials)")}
        assert "source_identity_key" not in columns
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
    finally:
        db.close()

    migrated = TrialStore(path)
    db = migrated._connect()
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row["name"] for row in db.execute("PRAGMA table_info(trials)")}
        assert "source_identity_key" in columns
        assert db.execute(
            "SELECT COUNT(DISTINCT source_identity_key) FROM trials"
        ).fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
    assert migrated.pool_snapshot()["runs"] == 1


# Round 224: independent OS processes, not only threads, race after recovery.
# Every worker opens its own TrialStore before a shared barrier releases writes.

def _multiprocess_ingest_worker(path, payload, sender, barrier, result_queue):
    try:
        store = TrialStore(path)
        barrier.wait(timeout=30)
        response = store.ingest_many(
            [payload], sender=sender, transport="fleet"
        )
        result_queue.put(("ok", response["results"][0]["status"]))
    except BaseException as exc:
        result_queue.put(("error", repr(exc)))
        raise


def _run_multiprocess_ingests(path, payload, sender, workers=6):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(workers + 1)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_ingest_worker,
            args=(str(path), payload, sender, barrier, result_queue),
        )
        for _ in range(workers)
    ]
    try:
        for process in processes:
            process.start()
        # All processes have completed TrialStore startup and own independent
        # connections before the parent releases the write race.
        barrier.wait(timeout=30)
        results = [result_queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=15)
        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0] * workers
        assert all(state == "ok" for state, _ in results), results
        return [status for _, status in results]
    finally:
        for process in processes:
            # A process.start() failure can leave later Process objects
            # unstarted; join() is invalid for those objects.
            if process.pid is None:
                continue
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()


@pytest.mark.skipif(os.name != "posix", reason="multiprocess SQLite race test requires POSIX")
def test_independent_process_duplicate_replays_preserve_one_first_seen_trial(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    payload = trial("process-duplicate-race")
    assert store.ingest_many(
        [payload], sender="peer-seed", transport="fleet"
    )["accepted"] == 1

    statuses = _run_multiprocess_ingests(
        path, payload, "peer-process-replay", workers=6
    )
    assert statuses == ["duplicate"] * 6
    assert _raw_counts_and_integrity(path) == (1, 0, 0, 7)
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 1
    receipts = reopened.receipt_records()
    assert sum(row["result"] == "accepted" for row in receipts) == 1
    assert sum(row["result"] == "duplicate" for row in receipts) == 6


@pytest.mark.skipif(os.name != "posix", reason="multiprocess SQLite race test requires POSIX")
def test_independent_process_conflicts_create_exactly_one_sender_quarantine(tmp_path):
    store, path = seeded(tmp_path)
    conflicting = trial(outcome="fail")
    statuses = _run_multiprocess_ingests(
        path, conflicting, "peer-process-conflict", workers=6
    )
    assert statuses.count("conflict_quarantined") == 1
    assert statuses.count("sender_quarantined") == 5
    assert _raw_counts_and_integrity(path) == (1, 1, 1, 7)

    reopened = TrialStore(path)
    assert len(reopened.quarantine_records()) == 1
    receipts = reopened.receipt_records()
    assert sum(row["result"] == "conflict_quarantined" for row in receipts) == 1
    assert sum(row["result"] == "sender_quarantined" for row in receipts) == 5
    assert reopened.pool_snapshot()["runs"] == 1
