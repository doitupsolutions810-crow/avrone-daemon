from concurrent.futures import ThreadPoolExecutor

import hashlib
import time
import pytest
import sqlite3

from avrone.trial_contract import canonical_trial_bytes, make_trial
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


def _create_legacy_trial_store(path, *, malformed_receipts=False):
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE trials (
            trial_id TEXT PRIMARY KEY, canonical_json BLOB NOT NULL, payload_sha256 TEXT NOT NULL,
            outcome TEXT NOT NULL, system TEXT NOT NULL, node TEXT NOT NULL, source TEXT NOT NULL,
            run_id TEXT NOT NULL, check_id TEXT NOT NULL, check_version TEXT NOT NULL,
            check_config_sha256 TEXT NOT NULL, attempt_id TEXT NOT NULL, first_sender TEXT NOT NULL,
            first_transport TEXT NOT NULL, bridge_run_id TEXT, received_at REAL NOT NULL
        )
    """)
    receipt_columns = (
        "receipt_id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, transport TEXT NOT NULL, "
        "result TEXT NOT NULL, payload_sha256 TEXT NOT NULL, received_at REAL NOT NULL"
        if malformed_receipts else
        "receipt_id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id TEXT NOT NULL, sender TEXT NOT NULL, "
        "transport TEXT NOT NULL, result TEXT NOT NULL, payload_sha256 TEXT NOT NULL, received_at REAL NOT NULL"
    )
    db.execute(f"CREATE TABLE trial_receipts ({receipt_columns})")
    db.execute("""
        CREATE TABLE trial_quarantine (
            quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id TEXT NOT NULL, sender TEXT NOT NULL,
            transport TEXT NOT NULL, reason TEXT NOT NULL, incoming_sha256 TEXT NOT NULL,
            incoming_json BLOB NOT NULL, received_at REAL NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE trial_quarantined_senders (
            sender TEXT PRIMARY KEY, first_trial_id TEXT NOT NULL, reason TEXT NOT NULL,
            quarantined_at REAL NOT NULL
        )
    """)
    db.commit()
    db.close()


def test_legacy_schema_migrates_and_preserves_bridge_run_per_delivery(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_trial_store(path)
    store = TrialStore(path)

    assert store.ingest_many([sample()], sender="peer-a", transport="fleet")["accepted"] == 1
    duplicate = store.ingest_many(
        [sample()], sender="peer-a", transport="bridge", bridge_run_id="bridge-run-1"
    )
    assert duplicate["results"][0]["status"] == "duplicate"
    conflict = store.ingest_many(
        [sample("fail")], sender="peer-b", transport="bridge", bridge_run_id="bridge-run-2"
    )
    assert conflict["results"][0]["status"] == "conflict_quarantined"

    db = store._connect()
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        receipts = db.execute(
            "SELECT transport,bridge_run_id,result FROM trial_receipts ORDER BY receipt_id"
        ).fetchall()
    finally:
        db.close()

    assert version == 2
    assert [(r["transport"], r["bridge_run_id"], r["result"]) for r in receipts] == [
        ("fleet", None, "accepted"),
        ("bridge", "bridge-run-1", "duplicate"),
        ("bridge", "bridge-run-2", "conflict_quarantined"),
    ]
    assert store.quarantine_records()[0]["bridge_run_id"] == "bridge-run-2"
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 1 and snapshot["failures"] == 0
    assert snapshot["independence"] == "not_assessed"


def test_future_database_schema_is_refused_without_downgrade(tmp_path):
    path = tmp_path / "future.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()

    with pytest.raises(TrialStoreError, match="newer"):
        TrialStore(path)

    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        db.close()


def test_failed_schema_migration_rolls_back_added_provenance_column(tmp_path):
    path = tmp_path / "broken-legacy.sqlite3"
    _create_legacy_trial_store(path, malformed_receipts=True)

    with pytest.raises(TrialStoreError, match="missing required columns"):
        TrialStore(path)

    db = sqlite3.connect(path)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(trial_receipts)")}
        assert "bridge_run_id" not in columns
        trial_columns = {row[1] for row in db.execute("PRAGMA table_info(trials)")}
        assert "source_identity_key" not in trial_columns
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        db.close()


def _insert_legacy_trial(path, envelope, sender="peer-a"):
    raw = canonical_trial_bytes(envelope)
    db = sqlite3.connect(path)
    try:
        db.execute(
            """
            INSERT INTO trials(
                trial_id,canonical_json,payload_sha256,outcome,system,node,source,run_id,
                check_id,check_version,check_config_sha256,attempt_id,first_sender,
                first_transport,bridge_run_id,received_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                envelope["trial_id"], raw, hashlib.sha256(raw).hexdigest(), envelope["outcome"],
                envelope["system"], envelope["node"], envelope["source"], envelope["run_id"],
                envelope["check_id"], envelope["check_version"], envelope["check_config_sha256"],
                envelope["attempt_id"], sender, "fleet", None, time.time(),
            ),
        )
        db.commit()
    finally:
        db.close()


def test_legacy_migration_backfills_source_identity_key(tmp_path):
    path = tmp_path / "legacy-with-row.sqlite3"
    _create_legacy_trial_store(path)
    _insert_legacy_trial(path, sample())

    store = TrialStore(path)
    db = store._connect()
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        row = db.execute(
            "SELECT source_identity_key FROM trials WHERE trial_id=?", (sample()["trial_id"],)
        ).fetchone()
    finally:
        db.close()

    assert version == 2
    assert row is not None and len(row["source_identity_key"]) == 64


def test_legacy_migration_refuses_preexisting_canonical_source_aliases(tmp_path):
    path = tmp_path / "legacy-source-aliases.sqlite3"
    _create_legacy_trial_store(path)
    values = dict(
        node="node-a", run_id="run-source-alias", check_id="verify",
        check_version="10.2", attempt_id="same-attempt", outcome="pass",
        check_config={"profile": "strict"},
    )
    _insert_legacy_trial(path, make_trial(system="grokbot", **values), sender="peer-a")
    _insert_legacy_trial(path, make_trial(system="GROKBOT", **values), sender="peer-b")

    with pytest.raises(TrialStoreError, match="canonical source/attempt aliases"):
        TrialStore(path)

    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        columns = {row[1] for row in db.execute("PRAGMA table_info(trials)")}
        assert "source_identity_key" not in columns
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 2
    finally:
        db.close()
