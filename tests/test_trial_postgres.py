from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Process, get_context

import pytest

pytest.importorskip("psycopg")
from avrone.trial_contract import make_trial, make_trial_outcome, trial_record_outcome, trial_record_poolable
from avrone.trial_postgres import PostgresTrialStore
from avrone.trial_receiver import canonical_json, verify_trial_receipt

DSN = os.environ.get("AVRONE_TEST_POSTGRES_DSN", "").strip()
SECRET = "postgres-trial-test-secret-" + "s" * 40
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL test DSN not configured")


def _record(failed: bool = False) -> dict:
    return make_trial(
        system="avrone", node="pg-test-node", run_id="pg-run-" + uuid.uuid4().hex,
        check_id="shared-store-concurrency", check_version="1", attempt_id="attempt-1",
        failed=failed, check_config={"backend": "postgres", "profile": "strict"},
    )


def _provenance(nonce: str) -> dict:
    return {
        "authenticated_sender": "pg-test-node", "sender_claim": "pg-test-node",
        "auth_method": "fleet_hmac_shared_token_allowlist",
        "auth_assurance": "shared_secret_group_member", "request_nonce": nonce,
        "request_timestamp": "1794000000", "request_body_sha256": "a" * 64, "bridge": None,
    }


def _worker_accept(args: tuple[str, dict, str]) -> dict:
    dsn, record, nonce = args
    store = PostgresTrialStore(dsn, SECRET)
    result = store.accept(record, _provenance(nonce))
    return {
        "status": result["status"], "receipt_id": result["receipt"]["receipt_id"],
        "quarantine_id": result["quarantine_id"],
    }


def _worker_crash_uncommitted(args: tuple[str, dict]) -> None:
    dsn, record = args
    store = PostgresTrialStore(dsn, SECRET)
    provenance = _provenance("crash_worker_nonce")
    from avrone.trial_receiver import _validate_provenance
    provenance = _validate_provenance(provenance, record)
    blob = canonical_json(record)
    digest = hashlib.sha256(blob).hexdigest()
    now = 1794000000
    receipt = store._receipt(record["trial_id"], digest, trial_record_outcome(record), now)
    with store._session() as (_, cur):
        cur.execute(
            """INSERT INTO trial_records(
                trial_id, record_digest, record_json, outcome, poolable, source, system, node,
                run_id, check_id, check_version, check_config_sha256, attempt_id, receipt_json,
                accepted_at, first_sender, first_auth_method
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                record["trial_id"], digest, blob, trial_record_outcome(record),
                int(trial_record_poolable(record)), record["source"], record["system"], record["node"],
                record["run_id"], record["check_id"], record["check_version"],
                record["check_config_sha256"], record["attempt_id"], canonical_json(receipt), now,
                provenance["authenticated_sender"], provenance["auth_method"],
            ),
        )
        os._exit(73)  # simulate process loss before transaction commit


def _store() -> PostgresTrialStore:
    return PostgresTrialStore(DSN, SECRET)


def test_postgres_persist_restart_and_stable_signed_receipt():
    record = _record()
    first = _store().accept(record, _provenance("restart_nonce_0001"))
    again = _store().accept(record, _provenance("restart_nonce_0002"))
    assert first["status"] == "accepted"
    assert again["status"] == "duplicate"
    assert first["receipt"]["receipt_id"] == again["receipt"]["receipt_id"]
    assert verify_trial_receipt(SECRET, again["receipt"])
    assert _store().get_record(record["trial_id"])["record"] == record


def test_postgres_independent_processes_accept_one_identity_once():
    record = _record()
    args = [(DSN, record, f"proc_{index:04d}_nonce") for index in range(16)]
    with ProcessPoolExecutor(max_workers=8, mp_context=get_context("spawn")) as pool:
        results = list(pool.map(_worker_accept, args))
    statuses = [item["status"] for item in results]
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 15
    assert len({item["receipt_id"] for item in results}) == 1
    assert _store().get_record(record["trial_id"])["record"] == record


def test_postgres_racing_conflicting_payloads_quarantines_one_digest():
    passed = _record(failed=False)
    failed = dict(passed)
    failed["failed"] = True
    assert passed["trial_id"] == failed["trial_id"]
    args = [
        (DSN, passed if index % 2 == 0 else failed, f"conflict_{index:04d}_nonce")
        for index in range(16)
    ]
    with ProcessPoolExecutor(max_workers=8, mp_context=get_context("spawn")) as pool:
        results = list(pool.map(_worker_accept, args))
    statuses = [item["status"] for item in results]
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 7
    assert statuses.count("conflict_quarantined") == 8
    assert len({item["quarantine_id"] for item in results if item["quarantine_id"] is not None}) == 1
    saved = _store().get_record(passed["trial_id"])
    assert saved is not None
    assert verify_trial_receipt(SECRET, saved["receipt"])


def test_postgres_crash_before_commit_leaves_no_partial_identity():
    record = _record()
    process = Process(
        target=_worker_crash_uncommitted, args=((DSN, record),), daemon=True,
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("crash simulation worker did not exit within 20 seconds")
    assert process.exitcode == 73
    assert _store().get_record(record["trial_id"]) is None


def test_postgres_batch_retry_and_nonpass_exclusion():
    store = _store()
    passed, second = _record(), _record()
    transport = {key: value for key, value in _provenance("batch_nonce_0001").items() if key != "bridge"}
    events = [{"record": passed, "bridge": None}, {"record": second, "bridge": None}]
    first = store.ingest_batch(events, transport)
    retry = store.ingest_batch(events, {**transport, "request_nonce": "batch_nonce_0002"})
    assert [item["status"] for item in first] == ["accepted", "accepted"]
    assert [item["status"] for item in retry] == ["duplicate", "duplicate"]
    before_runs = store.pool_snapshot()["runs"]
    nonpass = make_trial_outcome(
        system="avrone", node="pg-test-node", run_id="pg-nonpass-" + uuid.uuid4().hex,
        check_id="shared-store-concurrency", check_version="1", attempt_id="attempt-1",
        outcome="timed_out", check_config={"backend": "postgres"},
    )
    stored = store.accept(nonpass, _provenance("nonpass_nonce_0001"))
    assert stored["status"] == "operational_outcome_stored"
    assert stored["poolable"] is False
    assert store.pool_snapshot()["runs"] == before_runs


def test_postgres_unreachable_backend_fails_closed_without_sqlite_fallback():
    from avrone.trial_receiver import TrialStoreUnavailable
    with pytest.raises(TrialStoreUnavailable):
        PostgresTrialStore("postgresql://postgres:postgres@127.0.0.1:1/unavailable", SECRET)
