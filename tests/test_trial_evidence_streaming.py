"""Round 197: bounded streaming verification must avoid materializing JSON blobs."""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="first", outcome="pass", *, config=None):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-197",
        check_id="stream", check_version="v2", attempt_id=attempt,
        outcome=outcome, check_config=config,
    )


def seeded(tmp_path):
    store = TrialStore(tmp_path / "private" / "trials.sqlite3")
    inputs = [trial("pass"), trial("fail", "fail"), trial("skip", "skipped")]
    result = store.ingest_many(inputs, sender="peer-a", transport="fleet")
    assert result["accepted"] == 2 and result["non_pass"] == 1
    return store, inputs


class NoFetchall:
    """Cursor proxy forbids eager materialization of stored canonical payloads."""

    def __init__(self, cursor):
        self._cursor = cursor

    def __iter__(self):
        return iter(self._cursor)

    def fetchall(self):
        raise AssertionError("fetchall() would retain all canonical JSON blobs")


class StreamingConnection:
    def __init__(self, db):
        self.db = db
        self.called = 0

    def execute(self, sql, *args):
        assert sql == "SELECT * FROM trials ORDER BY trial_id"
        self.called += 1
        return NoFetchall(self.db.execute(sql, *args))


def test_verified_reader_does_not_call_fetchall_or_keep_payload_blob(tmp_path):
    store, _ = seeded(tmp_path)
    db = store._connect()
    try:
        proxy = StreamingConnection(db)
        records = list(store._verified_trial_rows(proxy))
        assert proxy.called == 1
        assert len(records) == 3
        assert all("canonical_json" not in row for row in records)
        assert all("payload_sha256" not in row for row in records)
        assert {row["outcome"] for row in records} == {"pass", "fail", "skipped"}
    finally:
        db.close()


def test_streaming_reader_has_lazy_iteration_and_one_row_boundary(tmp_path):
    store, _ = seeded(tmp_path)
    db = store._connect()
    try:
        proxy = StreamingConnection(db)
        iterator = store._verified_trial_rows(proxy)
        assert proxy.called == 0, "cursor should not execute before first yield"
        first = next(iterator)
        assert "trial_id" in first
        assert proxy.called == 1
        rest = list(iterator)
        assert len(rest) == 2
    finally:
        db.close()


def test_streaming_reader_fails_closed_on_overcapacity_db(tmp_path, monkeypatch):
    store, _ = seeded(tmp_path)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", 2)
        with pytest.raises(TrialStoreError, match="capacity"):
            store.pool_snapshot()
        with pytest.raises(TrialStoreError, match="capacity"):
            store.non_pass_records()


def test_streaming_integrity_rejects_corrupt_second_row_before_pool_report(tmp_path):
    store, inputs = seeded(tmp_path)
    db = store._connect()
    try:
        db.execute("UPDATE trials SET source='forged:node' WHERE trial_id=?", (inputs[1]["trial_id"],))
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="evidence integrity"):
        store.non_pass_records()


def test_clean_mixed_outcomes_keep_pool_and_nonpass_sort_order(tmp_path):
    store, inputs = seeded(tmp_path)
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 2
    assert snapshot["failures"] == 1
    assert [r["trial_id"] for r in store.non_pass_records()] == [inputs[2]["trial_id"]]


def test_large_bounded_envelope_remains_verified_without_retained_blob(tmp_path):
    store = TrialStore(tmp_path / "private" / "trials.sqlite3")
    big = trial(config={"padding": "x" * 12000})
    assert store.ingest_many([big], sender="peer-a", transport="fleet")["accepted"] == 1
    db = store._connect()
    try:
        rows = list(store._verified_trial_rows(db))
        assert len(rows) == 1
        assert "canonical_json" not in rows[0]
        assert rows[0]["trial_id"] == big["trial_id"]
        assert len(db.execute("SELECT canonical_json FROM trials").fetchone()[0]) > 12000
    finally:
        db.close()


def test_streaming_reader_reopens_cleanly_and_preserves_idempotency(tmp_path):
    store, inputs = seeded(tmp_path)
    reopened = TrialStore(store.path)
    assert reopened.pool_snapshot() == store.pool_snapshot()
    replay = reopened.ingest_many(inputs, sender="peer-a", transport="fleet")
    assert replay["duplicates"] == 3
    assert reopened.non_pass_records() == store.non_pass_records()
