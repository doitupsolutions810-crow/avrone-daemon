"""Round 196: multi-query quarantine audits must use one WAL read snapshot.

An audit reads quarantines, original trials, and sender markers separately.
Without BEGIN, another receiver can insert a legitimate conflict between
queries, causing a false mismatch or partial/inaccurate audit result.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="first", outcome="pass"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-196",
        check_id="snapshot", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def setup(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    reader = TrialStore(path)
    assert reader.ingest_many([trial()], sender="peer-a", transport="fleet")["accepted"] == 1
    assert reader.ingest_many([trial(outcome="fail")], sender="peer-b", transport="fleet")["quarantined"] == 1
    writer = TrialStore(path)
    return reader, writer


def add_another_conflict(store, attempt="second", sender="peer-c"):
    assert store.ingest_many([trial(attempt)], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([trial(attempt, "fail")], sender=sender, transport="fleet")["quarantined"] == 1


def test_writer_inserts_new_conflict_during_audit_without_phantom_marker(tmp_path, monkeypatch):
    reader, writer = setup(tmp_path)
    original = reader._assert_quarantine_conflict_semantics
    called = 0

    def interleave(db, row):
        nonlocal called
        called += 1
        if called == 1:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(add_another_conflict, writer).result(timeout=10)
        return original(db, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(reader, "_assert_quarantine_conflict_semantics", interleave)
        old_audit = reader.quarantine_records()

    assert called == 1
    assert [row["sender"] for row in old_audit] == ["peer-b"]
    assert {row["sender"] for row in reader.quarantine_records()} == {"peer-b", "peer-c"}


def test_writer_inserts_during_marker_validation_without_partial_audit(tmp_path, monkeypatch):
    reader, writer = setup(tmp_path)
    original = reader._verify_sender_quarantine_pair
    calls = 0

    def interleave(marker, row):
        nonlocal calls
        calls += 1
        if calls == 1:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(add_another_conflict, writer).result(timeout=10)
        return original(marker, row)

    with monkeypatch.context() as ctx:
        ctx.setattr(reader, "_verify_sender_quarantine_pair", interleave)
        current = reader.quarantine_records()

    assert calls == 1
    assert [row["sender"] for row in current] == ["peer-b"]
    assert {row["sender"] for row in reader.quarantine_records()} == {"peer-b", "peer-c"}


def test_audit_error_releases_reader_snapshot_for_subsequent_writer(tmp_path, monkeypatch):
    reader, writer = setup(tmp_path)
    def fail(db, row):
        raise TrialStoreError("injected audit validation failure")

    with monkeypatch.context() as ctx:
        ctx.setattr(reader, "_assert_quarantine_conflict_semantics", fail)
        with pytest.raises(TrialStoreError, match="injected audit"):
            reader.quarantine_records()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(add_another_conflict, writer).result(timeout=10)
    assert len(reader.quarantine_records()) == 2


def test_concurrent_clean_conflict_audits_and_writers_do_not_claim_corruption(tmp_path):
    reader, writer = setup(tmp_path)

    def write_many():
        for i in range(3):
            add_another_conflict(writer, attempt=f"wave-{i}", sender=f"peer-wave-{i}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = pool.submit(write_many)
        reads = pool.submit(lambda: [reader.quarantine_records() for _ in range(12)])
        snapshots = reads.result(timeout=30)
        writes.result(timeout=30)

    assert all(1 <= len(rows) <= 4 for rows in snapshots)
    assert all(
        len({row["sender"] for row in rows}) == len(rows)
        for rows in snapshots
    )
    assert {row["sender"] for row in reader.quarantine_records()} == {
        "peer-b", "peer-wave-0", "peer-wave-1", "peer-wave-2"
    }


def test_clean_audit_restart_preserves_exact_provenance(tmp_path):
    reader, _ = setup(tmp_path)
    expected = reader.quarantine_records()
    reopened = TrialStore(reader.path)
    assert reopened.quarantine_records() == expected
    assert reopened.pool_snapshot()["runs"] == 1
