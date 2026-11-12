from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


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


def test_store_requires_private_non_symlinked_wal_storage(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(TrialStoreError, match="directory"):
        TrialStore(linked_dir / "trials.sqlite3")

    linked_file = tmp_path / "linked.sqlite3"
    target_file = tmp_path / "target.sqlite3"
    target_file.write_bytes(b"not a database")
    linked_file.symlink_to(target_file)
    with pytest.raises(TrialStoreError, match="database"):
        TrialStore(linked_file)

    store = TrialStore(tmp_path / "private" / "trials.sqlite3")
    db = store._connect()
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        db.close()


def test_receipt_and_quarantine_keep_per_delivery_provenance(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    passed = sample("pass")
    failed = sample("fail")
    store.ingest_many([passed], sender="peer-a", transport="bridge", bridge_run_id="bridge-a")
    store.ingest_many([passed], sender="peer-b", transport="bridge", bridge_run_id="bridge-b")
    conflict = store.ingest_many([failed], sender="peer-c", transport="bridge", bridge_run_id="bridge-c")
    assert conflict["results"][0]["status"] == "conflict_quarantined"

    receipts = store.receipt_records()
    assert [row["bridge_run_id"] for row in receipts] == ["bridge-a", "bridge-b", "bridge-c"]
    for row in receipts:
        assert row["source"] == "cyclekernel:node-a"
        assert row["system"] == "cyclekernel"
        assert row["node"] == "node-a"
        assert row["run_id"] == "run-8"
        assert row["check_id"] == "verify"
        assert row["check_version"] == "10.2"
        assert row["attempt_id"] == "attempt-1"
        assert row["check_config_sha256"] == passed["check_config_sha256"]

    quarantine = store.quarantine_records()[0]
    assert quarantine["sender"] == "peer-c"
    assert quarantine["source"] == "cyclekernel:node-a"
    assert quarantine["check_version"] == "10.2"
    assert quarantine["bridge_run_id"] == "bridge-c"


def _create_pre_provenance_database(path):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE trial_receipts("
        "receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,trial_id TEXT NOT NULL,sender TEXT NOT NULL,"
        "transport TEXT NOT NULL,result TEXT NOT NULL,payload_sha256 TEXT NOT NULL,received_at REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE trial_quarantine("
        "quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,trial_id TEXT NOT NULL,sender TEXT NOT NULL,"
        "transport TEXT NOT NULL,reason TEXT NOT NULL,incoming_sha256 TEXT NOT NULL,"
        "incoming_json BLOB NOT NULL,received_at REAL NOT NULL)"
    )
    db.execute(
        "INSERT INTO trial_receipts(trial_id,sender,transport,result,payload_sha256,received_at) "
        "VALUES('old-id','peer-old','fleet','accepted','old-hash',1.0)"
    )
    db.execute(
        "INSERT INTO trial_quarantine(trial_id,sender,transport,reason,incoming_sha256,incoming_json,received_at) "
        "VALUES('old-id','peer-bad','bridge','old conflict','old-hash',X'7B7D',2.0)"
    )
    db.commit()
    db.close()


def test_pre_provenance_database_migrates_and_preserves_rows(tmp_path):
    path = tmp_path / "trials.sqlite3"
    _create_pre_provenance_database(path)
    store = TrialStore(path)
    receipt = store.receipt_records()[0]
    quarantine = store.quarantine_records()[0]
    assert receipt["trial_id"] == "old-id"
    assert receipt["source"] == "" and receipt["check_version"] == ""
    assert receipt["bridge_run_id"] is None
    assert quarantine["trial_id"] == "old-id"
    assert quarantine["source"] == "" and quarantine["check_version"] == ""
    assert quarantine["bridge_run_id"] is None
    db = store._connect()
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        db.close()


def test_schema_migration_rolls_back_all_ddl_on_interruption(tmp_path, monkeypatch):
    path = tmp_path / "trials.sqlite3"
    _create_pre_provenance_database(path)
    original = TrialStore._ensure_column
    calls = 0

    def fail_during_migration(db, table, column, declaration):
        nonlocal calls
        original(db, table, column, declaration)
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected migration interruption")

    monkeypatch.setattr(TrialStore, "_ensure_column", staticmethod(fail_during_migration))
    store = TrialStore.__new__(TrialStore)
    store.path = path
    with pytest.raises(TrialStoreError, match="initialization failed"):
        store._initialize()

    db = sqlite3.connect(path)
    try:
        receipt_columns = {row[1] for row in db.execute("PRAGMA table_info(trial_receipts)")}
        quarantine_columns = {row[1] for row in db.execute("PRAGMA table_info(trial_quarantine)")}
        assert "source" not in receipt_columns
        assert "source" not in quarantine_columns
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trials'"
        ).fetchone()
    finally:
        db.close()


def test_future_database_schema_fails_closed_without_downgrade(tmp_path):
    path = tmp_path / "trials.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=99")
    db.commit()
    db.close()
    with pytest.raises(TrialStoreError, match="newer than supported"):
        TrialStore(path)
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        db.close()


def test_concurrent_store_initialization_is_serialized(tmp_path):
    path = tmp_path / "trials.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: TrialStore(path), range(16)))
    assert all(store.pool_snapshot()["runs"] == 0 for store in stores)
