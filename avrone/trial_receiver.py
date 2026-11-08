"""Durable idempotent receiver for avrone.trial/v1 and operational outcomes."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .evidence_pool import pool_trials
from .trial_contract import (
    MAX_FIELD_CHARS,
    validate_trial_record,
    trial_record_outcome,
    trial_record_poolable,
)

MAX_BODY_BYTES = 256 * 1024
MAX_BATCH_EVENTS = 32
MAX_EVENT_BYTES = 16 * 1024
MAX_BRIDGE_POST_IDS = 20
AUTH_METHOD = "fleet_hmac_shared_token_allowlist"
AUTH_ASSURANCE = "shared_secret_group_member"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_NODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_POST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class TrialStoreConfigError(RuntimeError):
    """The durable receiver is not safely configured."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_bridge(bridge: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    if bridge is None:
        return None
    required = {"schema", "run_id", "node_id", "post_ids"}
    if not isinstance(bridge, dict) or set(bridge) != required:
        raise ValueError("bridge provenance fields do not match avrone.bridge-trial/v1")
    if bridge.get("schema") != "avrone.bridge-trial/v1":
        raise ValueError("unsupported bridge provenance schema")
    run_id, node_id, post_ids = bridge.get("run_id"), bridge.get("node_id"), bridge.get("post_ids")
    if (not isinstance(run_id, str) or not run_id or len(run_id) > MAX_FIELD_CHARS
            or unicodedata.normalize("NFC", run_id) != run_id
            or any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in run_id)):
        raise ValueError("bridge run_id is invalid")
    if not isinstance(node_id, str) or not _NODE_RE.fullmatch(node_id):
        raise ValueError("bridge node_id is invalid")
    if node_id != record["node"]:
        raise ValueError("bridge node_id does not match trial node")
    from .bridge import bridge_run_id
    if bridge_run_id(run_id) != record["run_id"]:
        raise ValueError("bridge run_id does not match trial run_id")
    if not isinstance(post_ids, list) or len(post_ids) > MAX_BRIDGE_POST_IDS:
        raise ValueError("bridge post_ids must be a bounded list")
    cleaned: list[str] = []
    for post_id in post_ids:
        if not isinstance(post_id, str) or not _POST_RE.fullmatch(post_id):
            raise ValueError("bridge post id is invalid")
        if post_id in cleaned:
            raise ValueError("bridge post_ids must be unique")
        cleaned.append(post_id)
    return {
        "schema": "avrone.bridge-trial/v1",
        "run_id": run_id,
        "node_id": node_id,
        "post_ids": cleaned,
    }


def _validate_provenance(provenance: Any, record: dict[str, Any]) -> dict[str, Any]:
    required = {
        "authenticated_sender", "sender_claim", "auth_method", "auth_assurance",
        "request_nonce", "request_timestamp", "request_body_sha256", "bridge",
    }
    if not isinstance(provenance, dict) or set(provenance) != required:
        raise ValueError("transport provenance fields do not match the receiver contract")
    sender = provenance.get("authenticated_sender")
    claim = provenance.get("sender_claim")
    if not isinstance(sender, str) or not _NODE_RE.fullmatch(sender):
        raise ValueError("authenticated sender is invalid")
    if (not isinstance(claim, str) or not claim or len(claim) > 128 or not claim.isascii()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in claim)
            or claim.split("/", 1)[0] != sender):
        raise ValueError("signed sender claim does not match authenticated sender")
    if provenance.get("auth_method") != AUTH_METHOD or provenance.get("auth_assurance") != AUTH_ASSURANCE:
        raise ValueError("unsupported transport authentication provenance")
    nonce = provenance.get("request_nonce")
    timestamp = provenance.get("request_timestamp")
    body_sha = provenance.get("request_body_sha256")
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise ValueError("request nonce is invalid")
    if (not isinstance(timestamp, str) or not timestamp.isascii()
            or not timestamp.isdecimal() or len(timestamp) > 12):
        raise ValueError("request timestamp is invalid")
    if not isinstance(body_sha, str) or not _HEX64_RE.fullmatch(body_sha):
        raise ValueError("request body digest is invalid")
    bridge = _validate_bridge(provenance.get("bridge"), record)
    return {
        "authenticated_sender": sender,
        "sender_claim": claim,
        "auth_method": AUTH_METHOD,
        "auth_assurance": AUTH_ASSURANCE,
        "request_nonce": nonce,
        "request_timestamp": timestamp,
        "request_body_sha256": body_sha,
        "bridge": bridge,
    }


def verify_trial_receipt(secret: str, receipt: Any) -> bool:
    required = {
        "receipt_id", "trial_id", "record_digest", "outcome", "accepted_at", "signature",
    }
    if (not isinstance(secret, str) or len(secret) < 32
            or not isinstance(receipt, dict) or set(receipt) != required):
        return False
    try:
        if str(uuid.UUID(receipt["receipt_id"])) != receipt["receipt_id"]:
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    if (not isinstance(receipt.get("trial_id"), str) or not _HEX64_RE.fullmatch(receipt["trial_id"])
            or not isinstance(receipt.get("record_digest"), str)
            or not _HEX64_RE.fullmatch(receipt["record_digest"])
            or not isinstance(receipt.get("outcome"), str)
            or receipt.get("outcome") not in {"passed", "failed", "skipped", "cancelled", "timed_out", "inconclusive", "blocked"}
            or isinstance(receipt.get("accepted_at"), bool)
            or not isinstance(receipt.get("accepted_at"), int)
            or receipt["accepted_at"] < 0
            or not isinstance(receipt.get("signature"), str)
            or not _HEX64_RE.fullmatch(receipt["signature"])):
        return False
    unsigned = {key: value for key, value in receipt.items() if key != "signature"}
    try:
        expected = hmac.new(secret.encode("utf-8"), canonical_json(unsigned), hashlib.sha256).hexdigest()
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    return hmac.compare_digest(receipt["signature"], expected)


class TrialStore:
    """Single-host SQLite store; every trial identity has one immutable accepted record."""

    def __init__(self, path: str | Path, receipt_secret: str):
        p = Path(path)
        if not p.is_absolute() or str(p) == ":memory:":
            raise TrialStoreConfigError("AVRONE_TRIAL_DB must be an absolute persistent path")
        if len(receipt_secret) < 32:
            raise TrialStoreConfigError("AVRONE_TRIAL_RECEIPT_SECRET must be at least 32 characters")
        if p.is_symlink():
            raise TrialStoreConfigError("trial database must not be a symlink")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.receipt_secret = receipt_secret
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trial_records (
                    trial_id TEXT PRIMARY KEY,
                    record_digest TEXT NOT NULL,
                    record_json BLOB NOT NULL,
                    outcome TEXT NOT NULL,
                    poolable INTEGER NOT NULL CHECK (poolable IN (0, 1)),
                    source TEXT NOT NULL,
                    system TEXT NOT NULL,
                    node TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    check_id TEXT NOT NULL,
                    check_version TEXT NOT NULL,
                    check_config_sha256 TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    receipt_json BLOB NOT NULL,
                    accepted_at INTEGER NOT NULL,
                    first_sender TEXT NOT NULL,
                    first_auth_method TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trial_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    provenance_json BLOB NOT NULL,
                    status TEXT NOT NULL,
                    received_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trial_deliveries_trial
                    ON trial_deliveries(trial_id, received_at);
                CREATE TABLE IF NOT EXISTS trial_quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    accepted_digest TEXT NOT NULL,
                    conflicting_digest TEXT NOT NULL,
                    conflicting_record_json BLOB NOT NULL,
                    provenance_json BLOB NOT NULL,
                    reason TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    UNIQUE(trial_id, conflicting_digest)
                );
            """)

    @classmethod
    def from_env(cls, fleet_token: str = "") -> "TrialStore":
        path = os.environ.get("AVRONE_TRIAL_DB", "").strip()
        secret = os.environ.get("AVRONE_TRIAL_RECEIPT_SECRET", "")
        if not path or not Path(path).is_absolute() or path == ":memory:":
            raise TrialStoreConfigError("AVRONE_TRIAL_DB must be an absolute persistent path")
        if len(secret) < 32:
            raise TrialStoreConfigError("AVRONE_TRIAL_RECEIPT_SECRET must be at least 32 characters")
        if fleet_token and hmac.compare_digest(secret, fleet_token):
            raise TrialStoreConfigError("receipt secret must be separate from the fleet token")
        return cls(path, secret)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        db.row_factory = sqlite3.Row
        return db

    def _receipt(self, trial_id: str, digest: str, outcome: str, accepted_at: int) -> dict[str, Any]:
        unsigned = {
            "receipt_id": str(uuid.uuid4()),
            "trial_id": trial_id,
            "record_digest": digest,
            "outcome": outcome,
            "accepted_at": accepted_at,
        }
        signature = hmac.new(
            self.receipt_secret.encode("utf-8"), canonical_json(unsigned), hashlib.sha256,
        ).hexdigest()
        return {**unsigned, "signature": signature}

    def accept(self, record: dict[str, Any], provenance: dict[str, Any],
               now: int | None = None) -> dict[str, Any]:
        if not validate_trial_record(record):
            raise ValueError("trial record contract rejected")
        provenance = _validate_provenance(provenance, record)
        record_blob = canonical_json(record)
        if len(record_blob) > MAX_EVENT_BYTES:
            raise ValueError(f"trial record exceeds {MAX_EVENT_BYTES} bytes")
        provenance_blob = canonical_json(provenance)
        digest = hashlib.sha256(record_blob).hexdigest()
        outcome = trial_record_outcome(record)
        poolable = trial_record_poolable(record)
        received_at = int(time.time()) if now is None else int(now)
        trial_id = record["trial_id"]
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT record_digest, receipt_json FROM trial_records WHERE trial_id=?",
                (trial_id,),
            ).fetchone()
            quarantine_id = None
            if row is None:
                receipt = self._receipt(trial_id, digest, outcome, received_at)
                db.execute(
                    """INSERT INTO trial_records(
                        trial_id, record_digest, record_json, outcome, poolable, source,
                        system, node, run_id, check_id, check_version, check_config_sha256,
                        attempt_id, receipt_json, accepted_at, first_sender, first_auth_method
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        trial_id, digest, record_blob, outcome, int(poolable), record["source"],
                        record["system"], record["node"], record["run_id"], record["check_id"],
                        record["check_version"], record["check_config_sha256"], record["attempt_id"],
                        canonical_json(receipt), received_at, provenance["authenticated_sender"],
                        provenance["auth_method"],
                    ),
                )
                status = "accepted" if poolable else "operational_outcome_stored"
                duplicate = False
                accepted = True
            elif hmac.compare_digest(row["record_digest"], digest):
                receipt = json.loads(bytes(row["receipt_json"]).decode("utf-8"))
                status = "duplicate" if poolable else "operational_outcome_duplicate"
                duplicate = True
                accepted = True
            else:
                db.execute(
                    """INSERT OR IGNORE INTO trial_quarantine(
                        trial_id, accepted_digest, conflicting_digest, conflicting_record_json,
                        provenance_json, reason, received_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        trial_id, row["record_digest"], digest, record_blob, provenance_blob,
                        "same trial_id received with different canonical payload or outcome",
                        received_at,
                    ),
                )
                qrow = db.execute(
                    "SELECT id FROM trial_quarantine WHERE trial_id=? AND conflicting_digest=?",
                    (trial_id, digest),
                ).fetchone()
                quarantine_id = int(qrow["id"]) if qrow else None
                receipt = json.loads(bytes(row["receipt_json"]).decode("utf-8"))
                status = "conflict_quarantined"
                duplicate = False
                accepted = False
            db.execute(
                """INSERT INTO trial_deliveries(
                    trial_id, record_digest, provenance_json, status, received_at
                ) VALUES (?,?,?,?,?)""",
                (trial_id, digest, provenance_blob, status, received_at),
            )
            db.commit()
        return {
            "trial_id": trial_id,
            "status": status,
            "accepted": accepted,
            "duplicate": duplicate,
            "poolable": poolable if accepted else False,
            "receipt": receipt,
            "quarantine_id": quarantine_id,
        }

    def ingest_batch(self, events: Any, transport: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH_EVENTS:
            raise ValueError(f"events must be a list containing 1..{MAX_BATCH_EVENTS} items")
        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in events:
            if not isinstance(item, dict) or set(item) != {"record", "bridge"}:
                raise ValueError("each event must contain exactly record and bridge")
            record = item["record"]
            if not validate_trial_record(record):
                raise ValueError("trial record contract rejected")
            if len(canonical_json(record)) > MAX_EVENT_BYTES:
                raise ValueError(f"trial record exceeds {MAX_EVENT_BYTES} bytes")
            provenance = {
                **transport,
                "bridge": _validate_bridge(item["bridge"], record),
            }
            provenance = _validate_provenance(provenance, record)
            prepared.append((record, provenance))
        # Each event is its own atomic transaction. If a later write is interrupted,
        # retrying the batch with a fresh fleet nonce returns stable duplicates for the committed prefix.
        return [self.accept(record, provenance) for record, provenance in prepared]

    def pool_snapshot(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT source, trial_id, outcome, record_json FROM trial_records "
                "WHERE poolable=1 ORDER BY trial_id"
            ).fetchall()
        trials = []
        for row in rows:
            record = json.loads(bytes(row["record_json"]).decode("utf-8"))
            trials.append({
                "source": row["source"],
                "trial_id": row["trial_id"],
                "failed": record["failed"],
            })
        return pool_trials(trials)

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            return {
                "records": int(db.execute("SELECT COUNT(*) FROM trial_records").fetchone()[0]),
                "deliveries": int(db.execute("SELECT COUNT(*) FROM trial_deliveries").fetchone()[0]),
                "quarantine": int(db.execute("SELECT COUNT(*) FROM trial_quarantine").fetchone()[0]),
                "poolable": int(db.execute("SELECT COUNT(*) FROM trial_records WHERE poolable=1").fetchone()[0]),
            }

    def get_record(self, trial_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT record_json, receipt_json FROM trial_records WHERE trial_id=?",
                (trial_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "record": json.loads(bytes(row["record_json"]).decode("utf-8")),
            "receipt": json.loads(bytes(row["receipt_json"]).decode("utf-8")),
        }
