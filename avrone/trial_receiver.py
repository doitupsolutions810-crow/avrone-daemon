"""Durable, idempotent storage and authenticated-transport adapters for trial v1.

This module does not authenticate a network request itself. HTTP callers must
pass a sender returned by FleetManager.dispatch_gateway and enforce an explicit
fleet allowlist. Bridge callers must be invoked only from the signed fleet
netsquad receive path. Claimed bridge origin is stored separately from that
authenticated transport sender.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .evidence_pool import MAX_POOL_TRIALS, pool_trials
from .trial_contract import TrialContractError, canonical_trial, validate_trial_envelope

MAX_DELIVERY_BYTES = 64 * 1024
MAX_BRIDGE_TEXT_BYTES = 3_500
MAX_STORED_TRIALS = MAX_POOL_TRIALS
MAX_DELIVERIES = 500_000
MAX_QUARANTINE_ROWS = 100_000
_SENDER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_STATUSES = {"accepted", "duplicate", "stored_non_pass", "quarantined"}


class TrialStoreError(RuntimeError):
    """Trial storage could not complete durably."""


class TrialStoreFull(TrialStoreError):
    """A hard append-only storage quota has been reached."""


def strict_json_loads(raw: bytes | str) -> Any:
    """Reject duplicate keys, non-finite numbers, invalid UTF-8, and deeply nested JSON."""
    if isinstance(raw, str):
        try:
            raw = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid JSON text encoding") from exc
    if not isinstance(raw, bytes) or len(raw) > MAX_DELIVERY_BYTES:
        raise ValueError("JSON payload exceeds the delivery byte limit")

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON object key")
            out[key] = value
        return out

    def bad_constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=bad_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid or excessively nested JSON") from exc


def _canonical_receipt(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _sender(value: Any) -> str:
    if not isinstance(value, str) or not _SENDER_RE.fullmatch(value):
        raise ValueError("authenticated sender is missing or malformed")
    return value


def _optional_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value.strip())
    if not value or len(value) > limit or any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in value):
        return None
    return value


class TrialStore:
    """SQLite WAL/FULL store; first write wins and conflicting replays are append-quarantined."""

    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("trial evidence requires persistent storage")
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("trial evidence database path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trials (
                    trial_id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, envelope_json BLOB NOT NULL,
                    receipt_id TEXT NOT NULL, system TEXT NOT NULL, node TEXT NOT NULL, source TEXT NOT NULL,
                    run_id TEXT NOT NULL, check_id TEXT NOT NULL, check_version TEXT NOT NULL,
                    check_config_sha256 TEXT NOT NULL, attempt_id TEXT NOT NULL, outcome TEXT NOT NULL,
                    failed INTEGER, authenticated_sender TEXT NOT NULL, first_transport TEXT NOT NULL,
                    stored_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id TEXT NOT NULL,
                    authenticated_sender TEXT NOT NULL, transport TEXT NOT NULL, transport_id TEXT,
                    claimed_origin TEXT, received_at INTEGER NOT NULL, result TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trial_deliveries_trial ON deliveries(trial_id, received_at);
                CREATE TABLE IF NOT EXISTS quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id TEXT NOT NULL,
                    existing_digest TEXT NOT NULL, incoming_digest TEXT NOT NULL, envelope_json BLOB NOT NULL,
                    reason TEXT NOT NULL, authenticated_sender TEXT NOT NULL, transport TEXT NOT NULL,
                    transport_id TEXT, claimed_origin TEXT, received_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trial_quarantine_trial ON quarantine(trial_id, received_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_quarantine_digest ON quarantine(trial_id, incoming_digest);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _record_delivery(db, trial_id, sender, transport, transport_id, claimed_origin, received_at, result):
        count = int(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])
        if count >= MAX_DELIVERIES:
            raise TrialStoreFull(f"trial delivery journal reached {MAX_DELIVERIES} rows")
        db.execute(
            "INSERT INTO deliveries(trial_id,authenticated_sender,transport,transport_id,"
            "claimed_origin,received_at,result) VALUES (?,?,?,?,?,?,?)",
            (trial_id, sender, transport, transport_id, claimed_origin, received_at, result),
        )

    def accept(
        self,
        envelope: dict[str, Any],
        authenticated_sender: str,
        transport: str = "fleet",
        transport_id: str | None = None,
        claimed_origin: str | None = None,
        received_at: int | None = None,
    ) -> dict[str, Any]:
        if not validate_trial_envelope(envelope):
            raise TrialContractError("trial envelope rejected")
        if transport not in {"fleet", "bridge"}:
            raise ValueError("transport must be fleet or bridge")
        sender = _sender(authenticated_sender)
        transport_id = _optional_text(transport_id, 128)
        claimed_origin = _optional_text(claimed_origin, 80)
        now = int(time.time()) if received_at is None else received_at
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("received_at must be a non-negative integer")

        raw = canonical_trial(envelope)
        digest = hashlib.sha256(raw).hexdigest()
        poolable = envelope["outcome"] in {"pass", "fail"}
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT payload_digest,envelope_json,receipt_id,stored_at FROM trials WHERE trial_id=?",
                    (envelope["trial_id"],),
                ).fetchone()
                if existing:
                    if existing["payload_digest"] == digest and bytes(existing["envelope_json"]) == raw:
                        self._record_delivery(db, envelope["trial_id"], sender, transport, transport_id,
                                              claimed_origin, now, "duplicate")
                        db.commit()
                        return {
                            "status": "duplicate", "duplicate": True, "poolable": poolable,
                            "trial_id": envelope["trial_id"], "payload_digest": digest,
                            "receipt_id": existing["receipt_id"], "stored_at": int(existing["stored_at"]),
                            "received_at": now,
                        }

                    qrow = db.execute(
                        "SELECT id FROM quarantine WHERE trial_id=? AND incoming_digest=?",
                        (envelope["trial_id"], digest),
                    ).fetchone()
                    if qrow:
                        quarantine_id = int(qrow["id"])
                    else:
                        qcount = int(db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0])
                        if qcount >= MAX_QUARANTINE_ROWS:
                            raise TrialStoreFull(f"trial conflict quarantine reached {MAX_QUARANTINE_ROWS} rows")
                        cur = db.execute(
                            "INSERT INTO quarantine(trial_id,existing_digest,incoming_digest,envelope_json,reason,"
                            "authenticated_sender,transport,transport_id,claimed_origin,received_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (envelope["trial_id"], existing["payload_digest"], digest, raw,
                             "same_trial_id_different_payload", sender, transport, transport_id,
                             claimed_origin, now),
                        )
                        quarantine_id = int(cur.lastrowid)
                    self._record_delivery(db, envelope["trial_id"], sender, transport, transport_id,
                                          claimed_origin, now, "quarantined")
                    db.commit()
                    return {
                        "status": "quarantined", "duplicate": False, "poolable": False,
                        "trial_id": envelope["trial_id"], "payload_digest": digest,
                        "receipt_id": str(uuid.uuid4()), "quarantine_id": quarantine_id,
                        "stored_at": now, "received_at": now,
                    }

                total = int(db.execute("SELECT COUNT(*) FROM trials").fetchone()[0])
                if total >= MAX_STORED_TRIALS:
                    raise TrialStoreFull(f"trial store reached {MAX_STORED_TRIALS} records")
                receipt_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO trials(trial_id,payload_digest,envelope_json,receipt_id,system,node,source,"
                    "run_id,check_id,check_version,check_config_sha256,attempt_id,outcome,failed,"
                    "authenticated_sender,first_transport,stored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        envelope["trial_id"], digest, raw, receipt_id, envelope["system"], envelope["node"],
                        envelope["source"], envelope["run_id"], envelope["check_id"], envelope["check_version"],
                        envelope["check_config_sha256"], envelope["attempt_id"], envelope["outcome"],
                        None if envelope["failed"] is None else int(envelope["failed"]),
                        sender, transport, now,
                    ),
                )
                status = "accepted" if poolable else "stored_non_pass"
                self._record_delivery(db, envelope["trial_id"], sender, transport, transport_id,
                                      claimed_origin, now, status)
                db.commit()
                return {
                    "status": status, "duplicate": False, "poolable": poolable,
                    "trial_id": envelope["trial_id"], "payload_digest": digest,
                    "receipt_id": receipt_id, "stored_at": now, "received_at": now,
                }
            except BaseException:
                db.rollback()
                raise

    def count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM trials").fetchone()[0])

    def delivery_count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])

    def quarantine_count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0])

    def quarantine_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("quarantine limit must be an integer")
        limit = max(1, min(limit, 1_000))
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM quarantine ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["envelope"] = json.loads(bytes(item.pop("envelope_json")).decode("utf-8"))
            out.append(item)
        return out

    def get_trial(self, trial_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM trials WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["envelope"] = json.loads(bytes(result.pop("envelope_json")).decode("utf-8"))
        return result

    def deliveries_for(self, trial_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT authenticated_sender,transport,transport_id,claimed_origin,received_at,result "
                "FROM deliveries WHERE trial_id=? ORDER BY id",
                (trial_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def pool_snapshot(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT source,trial_id,failed FROM trials WHERE outcome IN ('pass','fail') ORDER BY trial_id"
            ).fetchall()
        return pool_trials([{"source": r["source"], "trial_id": r["trial_id"],
                             "failed": bool(r["failed"])} for r in rows])


def signed_receipt(secret: str, result: dict[str, Any], authenticated_sender: str,
                   transport: str, receiver_node: str) -> dict[str, Any]:
    if not isinstance(secret, str) or not secret:
        raise ValueError("receipt signing secret is unavailable")
    unsigned = {
        "receipt_id": result["receipt_id"], "trial_id": result["trial_id"],
        "payload_digest": result["payload_digest"], "status": result["status"],
        "received_at": int(result["received_at"]), "authenticated_sender": _sender(authenticated_sender),
        "receiver_node": _sender(receiver_node), "transport": transport,
    }
    signature = hmac.new(secret.encode("utf-8"), _canonical_receipt(unsigned), hashlib.sha256).hexdigest()
    return {**unsigned, "signature": signature}


def verify_receipt(secret: str, receipt: Any) -> bool:
    required = {
        "receipt_id", "trial_id", "payload_digest", "status", "received_at",
        "authenticated_sender", "receiver_node", "transport", "signature",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        return False
    try:
        if str(uuid.UUID(receipt["receipt_id"])) != receipt["receipt_id"]:
            return False
        if not isinstance(receipt["trial_id"], str) or not _HEX64.fullmatch(receipt["trial_id"]):
            return False
        if not isinstance(receipt["payload_digest"], str) or not _HEX64.fullmatch(receipt["payload_digest"]):
            return False
        if receipt["status"] not in _RECEIPT_STATUSES:
            return False
        if isinstance(receipt["received_at"], bool) or not isinstance(receipt["received_at"], int):
            return False
        _sender(receipt["authenticated_sender"])
        _sender(receipt["receiver_node"])
        if receipt["transport"] not in {"fleet", "bridge"}:
            return False
        signature = receipt["signature"]
        if not isinstance(signature, str) or not _HEX64.fullmatch(signature):
            return False
        unsigned = {key: receipt[key] for key in required if key != "signature"}
        expected = hmac.new(secret.encode("utf-8"), _canonical_receipt(unsigned), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def post_trial_to_bridge(bridge: Any, envelope: dict[str, Any], agent: str = "trial-adapter") -> dict[str, Any]:
    """Publish a validated envelope as a typed Bridge post; fleet HMAC authenticates relay delivery."""
    raw = canonical_trial(envelope)
    if len(raw) > MAX_BRIDGE_TEXT_BYTES:
        raise TrialContractError("trial envelope does not fit a bounded bridge post")
    return bridge.post(agent, "evidence", raw.decode("utf-8"))


def ingest_bridge_posts(
    store: TrialStore,
    posts: Any,
    authenticated_sender: str,
    received_at: int | None = None,
) -> dict[str, int]:
    """Ingest evidence-kind posts after the caller has authenticated and allowlisted the fleet sender."""
    sender = _sender(authenticated_sender)
    counts = {"accepted": 0, "duplicates": 0, "non_pass": 0, "quarantined": 0, "rejected": 0}
    if not isinstance(posts, (list, tuple)):
        return counts
    for post in posts:
        if not isinstance(post, dict) or post.get("kind") != "evidence":
            continue
        text = post.get("text")
        if not isinstance(text, str):
            counts["rejected"] += 1
            continue
        try:
            raw = text.encode("utf-8")
            if len(raw) > MAX_BRIDGE_TEXT_BYTES:
                raise ValueError("bridge evidence post exceeds its byte limit")
            envelope = strict_json_loads(raw)
            if not validate_trial_envelope(envelope):
                raise ValueError("bridge evidence envelope rejected")
        except (ValueError, UnicodeEncodeError, RecursionError):
            counts["rejected"] += 1
            continue
        result = store.accept(
            envelope, sender, "bridge", _optional_text(post.get("id"), 80),
            _optional_text(post.get("origin_node"), 80), received_at,
        )
        if result["status"] == "accepted":
            counts["accepted"] += 1
        elif result["status"] == "duplicate":
            counts["duplicates"] += 1
        elif result["status"] == "stored_non_pass":
            counts["non_pass"] += 1
        elif result["status"] == "quarantined":
            counts["quarantined"] += 1
    return counts


def ingest_authenticated_bridge_posts(
    store: TrialStore,
    posts: Any,
    authenticated_sender: str,
    allowed_senders: set[str] | frozenset[str],
    received_at: int | None = None,
) -> dict[str, int]:
    """Fail closed unless the signed sender is present in a non-empty explicit allowlist."""
    sender = _sender(authenticated_sender)
    if not allowed_senders or sender not in allowed_senders:
        raise PermissionError("bridge evidence sender is not explicitly allowlisted")
    return ingest_bridge_posts(store, posts, sender, received_at)
