"""Round 201: legacy migration must stream canonical bytes under a strict cap.

Tests start from a valid current store, then downgrade only the schema layout
to a pre-v2 source-identity format. That retains the real SQLite and canonical
wire encodings while exercising migration rollback, corruption and contention.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from avrone.trial_contract import MAX_ENVELOPE_BYTES, canonical_trial_bytes, make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(i):
    return make_trial(
        system="grokbot", node="node-201", run_id="run-201",
        check_id="legacy-migration", check_version="v2",
        attempt_id=f"attempt-{i}", outcome="pass",
        check_config={"serial": i},
    )


def downgrade(path):
    """Replace a populated v2 source index with a real v0 column layout."""
    db = sqlite3.connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP TRIGGER IF EXISTS trial_source_identity_key_required_insert")
        db.execute("DROP TRIGGER IF EXISTS trial_source_identity_key_required_update")
        db.execute("DROP INDEX IF EXISTS trial_source_identity_key")
        db.execute("ALTER TABLE trials DROP COLUMN source_identity_key")
        db.execute("PRAGMA user_version=0")
        db.commit()
    finally:
        db.close()


def legacy_store(tmp_path, count=5):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    for i in range(count):
        result = store.ingest_many([trial(i)], sender="peer-a", transport="fleet")
        assert result["accepted"] == 1
    downgrade(path)
    return path


def state(path):
    db = sqlite3.connect(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in db.execute("PRAGMA table_info(trials)")}
        count = db.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
        return version, "source_identity_key" in columns, count
    finally:
        db.close()


def alter_blob(path, raw):
    db = sqlite3.connect(path)
    try:
        trial_id = db.execute(
            "SELECT trial_id FROM trials ORDER BY trial_id DESC LIMIT 1"
        ).fetchone()[0]
        old = db.execute(
            "SELECT canonical_json FROM trials WHERE trial_id=?", (trial_id,)
        ).fetchone()[0]
        db.execute(
            "UPDATE trials SET canonical_json=?,payload_sha256=? WHERE trial_id=?",
            (raw, hashlib.sha256(
                raw.encode("utf-8") if isinstance(raw, str) else raw
            ).hexdigest(), trial_id),
        )
        db.commit()
        return trial_id, old
    finally:
        db.close()


@pytest.mark.parametrize("batch", [1, 2, 3])
def test_legacy_keyset_migration_pages_validate_all_rows_and_keep_identity(
    tmp_path, monkeypatch, batch,
):
    path = legacy_store(tmp_path, 9)
    from avrone import trial_receiver

    traced = []
    original_connect = TrialStore._connect

    def trace_migration(self):
        db = original_connect(self)
        db.set_trace_callback(traced.append)
        return db

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MIGRATION_BATCH_SIZE", batch)
        ctx.setattr(TrialStore, "_connect", trace_migration)
        reopened = TrialStore(path)

    assert state(path) == (2, True, 9)
    assert reopened.pool_snapshot()["runs"] == 9
    db = reopened._connect()
    try:
        assert db.execute("SELECT COUNT(DISTINCT source_identity_key) FROM trials").fetchone()[0] == 9
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()

    # Keyset pagination, not unbounded fetchall of raw canonical BLOBs.
    pages = [sql for sql in traced if "canonical_json,source_identity_key" in sql
             and "ORDER BY trial_id LIMIT" in sql]
    assert len(pages) >= 2
    assert any("WHERE trial_id>" in sql for sql in pages)
    assert all(f"LIMIT {batch}" in sql for sql in pages)


@pytest.mark.parametrize("count,cap", [(4, 3), (1, 0), (7, 2)])
def test_overcapacity_legacy_store_refuses_migration_without_partial_schema(
    tmp_path, monkeypatch, count, cap,
):
    path = legacy_store(tmp_path, count)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", cap)
        with pytest.raises(TrialStoreError, match="migration exceeds receiver capacity"):
            TrialStore(path)
    assert state(path) == (0, False, count)
    assert TrialStore(path).pool_snapshot()["runs"] == count


def test_exact_legacy_migration_capacity_is_allowed(tmp_path, monkeypatch):
    path = legacy_store(tmp_path, 4)
    from avrone import trial_receiver
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MAX_TRIAL_RECORDS", 4)
        reopened = TrialStore(path)
        assert reopened.pool_snapshot()["runs"] == 4
    assert state(path) == (2, True, 4)


@pytest.mark.parametrize(
    "bad_raw",
    [
        pytest.param(b"Z" * (MAX_ENVELOPE_BYTES + 1), id="oversized-blob"),
        pytest.param("text-not-binary", id="text-typed-envelope"),
        pytest.param(b"{}", id="missing-envelope-fields"),
        pytest.param(b"\xff", id="invalid-utf8"),
    ],
)
def test_invalid_legacy_blob_rolls_back_all_schema_and_key_migration(
    tmp_path, bad_raw,
):
    path = legacy_store(tmp_path, 7)
    old_state = state(path)
    trial_id, old = alter_blob(path, bad_raw)
    with pytest.raises(TrialStoreError, match="cannot be validated"):
        TrialStore(path)
    assert state(path) == old_state
    # Repairing the original canonical payload permits a clean retry.
    db = sqlite3.connect(path)
    try:
        db.execute(
            "UPDATE trials SET canonical_json=?,payload_sha256=? WHERE trial_id=?",
            (old, hashlib.sha256(old).hexdigest(), trial_id),
        )
        db.commit()
    finally:
        db.close()
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 7
    assert state(path) == (2, True, 7)


def test_legacy_noncanonical_bytes_are_rejected_even_with_matching_digest(tmp_path):
    path = legacy_store(tmp_path, 2)
    db = sqlite3.connect(path)
    try:
        stored = db.execute(
            "SELECT canonical_json FROM trials ORDER BY trial_id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        db.close()
    envelope = json.loads(stored)
    raw = json.dumps(envelope, indent=2, sort_keys=True).encode()
    assert raw != canonical_trial_bytes(envelope)
    alter_blob(path, raw)
    with pytest.raises(TrialStoreError, match="cannot be validated"):
        TrialStore(path)
    assert state(path) == (0, False, 2)


def test_concurrent_legacy_keyset_migrators_preserve_atomic_schema(tmp_path, monkeypatch):
    path = legacy_store(tmp_path, 5)
    from avrone import trial_receiver
    barrier = Barrier(3)

    def migrate(_):
        barrier.wait(timeout=15)
        store = TrialStore(path)
        return store.pool_snapshot()["runs"]

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver, "MIGRATION_BATCH_SIZE", 2)
        with ThreadPoolExecutor(max_workers=3) as pool:
            assert list(pool.map(migrate, range(3))) == [5, 5, 5]
    assert state(path) == (2, True, 5)
    assert TrialStore(path).pool_snapshot()["runs"] == 5
