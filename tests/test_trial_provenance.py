"""Adversarial tests for schema-v3 receiver provenance digests and migration."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def sample(outcome: str = "pass", attempt_id: str = "attempt-1"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run-v3", check_id="verify",
        check_version="10.2", attempt_id=attempt_id, outcome=outcome,
        check_config={"profile": "airgap", "unicode": "café-🤖"},
    )


def seed_store(path):
    store = TrialStore(path)
    store.ingest_many([sample("pass")], sender="peer-a", transport="fleet")
    store.ingest_many(
        [sample("fail")], sender="peer-b", transport="bridge", bridge_run_id="bridge-1"
    )
    store.ingest_many(
        [sample("skipped", "attempt-2")], sender="peer-c", transport="fleet"
    )
    return store


def downgrade_to_v2(path):
    db = sqlite3.connect(path)
    try:
        for table in ("trials", "trial_receipts", "trial_quarantine", "trial_quarantined_senders"):
            for action in ("insert", "update"):
                db.execute(
                    f"DROP TRIGGER IF EXISTS {table}_provenance_sha256_required_{action}"
                )
            db.execute(f"ALTER TABLE {table} DROP COLUMN provenance_sha256")
        db.execute("PRAGMA user_version=2")
        db.commit()
    finally:
        db.close()


def test_v3_persists_and_verifies_first_receipt_quarantine_and_delivery_provenance(tmp_path):
    store = seed_store(tmp_path / "trials.sqlite3")
    db = store._connect()
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = ("trials", "trial_receipts", "trial_quarantine", "trial_quarantined_senders")
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE length(provenance_sha256)=64").fetchone()[0]
            for table in tables
        }
    finally:
        db.close()
    assert version == 3
    assert counts == {"trials": 2, "trial_receipts": 3, "trial_quarantine": 1, "trial_quarantined_senders": 1}
    assert len(store.receipt_records()) == 3
    assert len(store.quarantine_records()) == 1
    assert len(store.non_pass_records()) == 1
    assert store.pool_snapshot()["independence"] == "not_assessed"


@pytest.mark.parametrize(
    "column,value,transport",
    [
        ("first_sender", "peer-z", "fleet"),
        ("received_at", 123.5, "fleet"),
        ("bridge_run_id", "bridge-altered", "bridge"),
    ],
)
def test_trial_first_receipt_digest_detects_valid_shape_metadata_rewrites(
    tmp_path, column, value, transport,
):
    path = tmp_path / "trial-provenance.sqlite3"
    store = TrialStore(path)
    envelope = sample()
    kwargs = {"sender": "peer-a", "transport": transport}
    if transport == "bridge":
        kwargs["bridge_run_id"] = "bridge-original"
    store.ingest_many([envelope], **kwargs)
    db = store._connect()
    try:
        db.execute(
            f"UPDATE trials SET {column}=? WHERE trial_id=?",
            (value, envelope["trial_id"]),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="provenance integrity"):
        store.pool_snapshot()


def test_quarantine_digest_detects_alternate_valid_conflict_reason(tmp_path):
    store = TrialStore(tmp_path / "quarantine-provenance.sqlite3")
    store.ingest_many([sample("pass")], sender="peer-a", transport="fleet")
    store.ingest_many(
        [sample("fail")], sender="peer-b", transport="bridge", bridge_run_id="bridge-1"
    )
    db = store._connect()
    try:
        db.execute(
            "UPDATE trial_quarantine SET reason=? WHERE sender='peer-b'",
            ("canonical source/check/attempt identity arrived with a different trial_id",),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="provenance integrity"):
        store.quarantine_records()


def test_sender_quarantine_digest_detects_valid_trial_id_rewrite(tmp_path):
    store = TrialStore(tmp_path / "sender-provenance.sqlite3")
    store.ingest_many([sample("pass")], sender="peer-a", transport="fleet")
    store.ingest_many([sample("fail")], sender="peer-b", transport="fleet")
    db = store._connect()
    try:
        db.execute(
            "UPDATE trial_quarantined_senders SET first_trial_id=? WHERE sender='peer-b'",
            ("f" * 64,),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="provenance integrity|state integrity"):
        store.quarantine_records()


def test_receipt_digest_detects_valid_result_rewrite(tmp_path):
    store = TrialStore(tmp_path / "receipt-provenance.sqlite3")
    store.ingest_many([sample("pass")], sender="peer-a", transport="fleet")
    db = store._connect()
    try:
        db.execute("UPDATE trial_receipts SET result='duplicate'")
        db.commit()
    finally:
        db.close()
    with pytest.raises(TrialStoreError, match="receipt provenance integrity"):
        store.receipt_records()


def test_provenance_digest_triggers_reject_malformed_digest_values(tmp_path):
    store = seed_store(tmp_path / "trigger-provenance.sqlite3")
    db = store._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="invalid provenance_sha256"):
            db.execute("UPDATE trials SET provenance_sha256=?", ("G" * 64,))
        db.rollback()
    finally:
        db.close()
    assert store.pool_snapshot()["runs"] == 1


def test_v2_migration_backfills_all_existing_provenance_without_changing_evidence(tmp_path):
    path = tmp_path / "migration-v2.sqlite3"
    original = seed_store(path)
    before_pool = original.pool_snapshot()
    before_non_pass = original.non_pass_records()
    before_receipts = original.receipt_records()
    before_quarantine = original.quarantine_records()
    downgrade_to_v2(path)

    migrated = TrialStore(path)
    db = migrated._connect()
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        for table in ("trials", "trial_receipts", "trial_quarantine", "trial_quarantined_senders"):
            assert db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE provenance_sha256 IS NULL OR length(provenance_sha256)!=64"
            ).fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
    assert migrated.pool_snapshot() == before_pool
    assert migrated.non_pass_records() == before_non_pass
    assert len(migrated.receipt_records()) == len(before_receipts)
    assert len(migrated.quarantine_records()) == len(before_quarantine)


def test_v2_migration_rolls_back_columns_and_version_on_invalid_legacy_provenance(tmp_path):
    path = tmp_path / "migration-rollback.sqlite3"
    seed_store(path)
    downgrade_to_v2(path)
    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE trials SET first_transport='unknown'")
        db.commit()
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="provenance cannot be validated"):
        TrialStore(path)

    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in db.execute("PRAGMA table_info(trials)")}
        assert "provenance_sha256" not in columns
    finally:
        db.close()


def test_concurrent_v2_initializers_commit_one_complete_provenance_migration(tmp_path):
    path = tmp_path / "migration-concurrent.sqlite3"
    seed_store(path)
    downgrade_to_v2(path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        stores = list(executor.map(lambda _: TrialStore(path), range(4)))

    assert all(store.pool_snapshot()["runs"] == 1 for store in stores)
    assert all(len(store.receipt_records()) == 3 for store in stores)
    db = stores[0]._connect()
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
