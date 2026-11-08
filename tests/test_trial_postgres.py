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


def _worker_commit_then_lose_reply(args: tuple[str, dict, str]) -> None:
    dsn, record, nonce = args
    result = PostgresTrialStore(dsn, SECRET).accept(record, _provenance(nonce))
    if result["status"] not in {"accepted", "duplicate"}:
        os._exit(75)
    os._exit(74)  # transaction committed; simulate loss before the HTTP response is sent


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


def test_postgres_commit_then_lost_response_retries_as_same_duplicate():
    record = _record()
    process = Process(
        target=_worker_commit_then_lose_reply,
        args=((DSN, record, "commit_lost_reply_nonce"),),
        daemon=True,
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("commit-loss simulation worker did not exit within 20 seconds")
    assert process.exitcode == 74

    # The process committed but never sent its receipt. A sender retry must be
    # able to recover the durable winner rather than creating a second accept.
    retry = _store().accept(record, _provenance("commit_retry_nonce_0001"))
    assert retry["status"] == "duplicate"
    assert retry["accepted"] is True
    assert retry["duplicate"] is True
    assert verify_trial_receipt(SECRET, retry["receipt"])
    saved = _store().get_record(record["trial_id"])
    assert saved is not None
    assert saved["receipt"]["receipt_id"] == retry["receipt"]["receipt_id"]


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


def test_postgres_repeated_conflict_is_quarantined_once_but_delivery_is_audited_each_time():
    accepted = _record(failed=False)
    conflict = dict(accepted)
    conflict["failed"] = True
    store = _store()
    first = store.accept(accepted, _provenance("repeat_conflict_accept_01"))
    assert first["status"] == "accepted"

    one = store.accept(conflict, _provenance("repeat_conflict_delivery_01"))
    two = store.accept(conflict, _provenance("repeat_conflict_delivery_02"))
    assert one["status"] == two["status"] == "conflict_quarantined"
    assert one["quarantine_id"] == two["quarantine_id"]
    assert store.get_record(accepted["trial_id"])["record"] == accepted
    counts = store.counts()
    assert counts["records"] >= 1
    assert counts["quarantine"] >= 1


def test_postgres_batch_prevalidates_every_event_before_first_write():
    store = _store()
    valid = _record()
    invalid = _record()
    invalid["check_version"] = ""
    transport = {
        key: value for key, value in _provenance("batch_prevalidation_nonce").items()
        if key != "bridge"
    }
    events = [
        {"record": valid, "bridge": None},
        {"record": invalid, "bridge": None},
    ]
    with pytest.raises(ValueError):
        store.ingest_batch(events, transport)
    assert store.get_record(valid["trial_id"]) is None
    assert store.get_record(invalid["trial_id"]) is None


def test_postgres_batch_size_boundary_accepts_max_and_rejects_over_max():
    from avrone.trial_receiver import MAX_BATCH_EVENTS

    store = _store()
    records = [_record() for _ in range(MAX_BATCH_EVENTS)]
    transport = {
        key: value for key, value in _provenance("batch_size_boundary_nonce").items()
        if key != "bridge"
    }
    events = [{"record": record, "bridge": None} for record in records]
    accepted = store.ingest_batch(events, transport)
    assert len(accepted) == MAX_BATCH_EVENTS
    assert all(item["status"] == "accepted" for item in accepted)

    too_many = events + [{"record": _record(), "bridge": None}]
    with pytest.raises(ValueError):
        store.ingest_batch(too_many, transport)


def test_postgres_invalid_input_never_creates_a_delivery_or_record():
    store = _store()
    before = store.counts()
    record = _record()
    invalid = _provenance("invalid_sender_nonce_01")
    invalid["authenticated_sender"] = "not valid!"
    with pytest.raises(ValueError):
        store.accept(record, invalid)
    assert store.get_record(record["trial_id"]) is None
    after = store.counts()
    assert after == before


def test_postgres_delivery_write_failure_rolls_back_accepted_record():
    import psycopg
    from avrone.trial_receiver import TrialStoreUnavailable

    record = _record()
    token = uuid.uuid4().hex
    function_name = f"avrone_test_fail_delivery_{token}"
    trigger_name = f"avrone_test_fail_delivery_{token}"
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""CREATE FUNCTION {function_name}() RETURNS trigger
                    LANGUAGE plpgsql AS $
                    BEGIN
                      IF NEW.trial_id = TG_ARGV[0] THEN
                        RAISE EXCEPTION 'injected delivery write failure';
                      END IF;
                      RETURN NEW;
                    END
                    $"""
            )
            cur.execute(
                f"""CREATE TRIGGER {trigger_name}
                    BEFORE INSERT ON trial_deliveries
                    FOR EACH ROW EXECUTE FUNCTION {function_name}('{record["trial_id"]}')"""
            )

    try:
        with pytest.raises(TrialStoreUnavailable):
            _store().accept(record, _provenance("atomic_delivery_fail_nonce"))
        assert _store().get_record(record["trial_id"]) is None
    finally:
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON trial_deliveries")
                cur.execute(f"DROP FUNCTION IF EXISTS {function_name}()")


def test_postgres_unreachable_backend_fails_closed_without_sqlite_fallback():
    from avrone.trial_receiver import TrialStoreUnavailable
    with pytest.raises(TrialStoreUnavailable):
        PostgresTrialStore("postgresql://postgres:postgres@127.0.0.1:1/unavailable", SECRET)
