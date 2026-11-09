"""Durable idempotent receiver for versioned AVRONE verification trials.

SQLite transactions provide cross-thread/process serialization. Conflicting
payloads for an existing trial ID are retained in quarantine; the first receipt
is never overwritten and the sending peer is blocked from subsequent ingestion.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .evidence_pool import MAX_POOL_TRIALS, pool_trials
from .trial_contract import canonical_trial_json, validate_trial_envelope

MAX_TRIAL_BATCH = 100
MAX_TRIAL_BODY_BYTES = 256 * 1024
MAX_STORED_TRIALS = MAX_POOL_TRIALS
MAX_REQUEST_ID_CHARS = 128
_PEER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_ALLOWED_TRANSPORTS = frozenset({"fleet_hmac_v1", "local_event_v1"})


class TrialStoreError(RuntimeError):
    """Persistence is unavailable or failed; the caller must not report acceptance."""


class TrialStore:
    def __init__(self, data_dir: Path | str):
        self.root = Path(data_dir) / "evidence"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink():
                raise TrialStoreError("trial evidence directory must not be a symlink")
            self.path = self.root / "trials.sqlite3"
            if self.path.is_symlink():
                raise TrialStoreError("trial database must not be a symlink")
            if self.path.exists() and not self.path.is_file():
                raise TrialStoreError("trial database path is not a regular file")
            self._initialize()
        except OSError as exc:
            raise TrialStoreError("could not create trial evidence storage") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            if self.path.is_symlink():
                raise TrialStoreError("trial database must not be a symlink")
            conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            return conn
        except sqlite3.Error as exc:
            raise TrialStoreError("trial database is unavailable") from exc

    def _initialize(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise TrialStoreError("trial database could not enable WAL mode")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trials (
                    trial_id TEXT PRIMARY KEY,
                    envelope_json TEXT NOT NULL,
                    envelope_sha256 TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    source TEXT NOT NULL,
                    system TEXT NOT NULL,
                    node TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    check_id TEXT NOT NULL,
                    check_version TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    pooled INTEGER NOT NULL CHECK (pooled IN (0, 1)),
                    first_peer TEXT NOT NULL,
                    first_transport TEXT NOT NULL,
                    received_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
                    peer TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    first_received_at REAL NOT NULL,
                    last_received_at REAL NOT NULL,
                    delivery_count INTEGER NOT NULL CHECK (delivery_count >= 1),
                    PRIMARY KEY (trial_id, peer)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    existing_sha256 TEXT NOT NULL,
                    incoming_sha256 TEXT NOT NULL,
                    incoming_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    received_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantined_peers (
                    peer TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    conflict_count INTEGER NOT NULL CHECK (conflict_count >= 1)
                );
                PRAGMA user_version=1;
            """)
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                raise TrialStoreError("trial database integrity check failed; evidence was not reset")
        except sqlite3.Error as exc:
            raise TrialStoreError("trial database initialization failed; existing evidence was not reset") from exc
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _request_id(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str) or len(value) > MAX_REQUEST_ID_CHARS:
            raise ValueError("request_id must be a string of at most 128 characters")
        return value

    @staticmethod
    def _item(index: int, status: str, record: Any, reason: str | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {"index": index, "status": status}
        if isinstance(record, dict):
            for key in ("trial_id", "source", "check_version", "outcome"):
                value = record.get(key)
                if isinstance(value, str):
                    item[key] = value
        if reason:
            item["reason"] = reason
        return item

    def receive_batch(self, records: Any, *, peer: str, transport: str,
                      request_id: Any = None) -> dict[str, Any]:
        if not isinstance(peer, str) or not _PEER_RE.fullmatch(peer):
            raise ValueError("authenticated peer id is invalid")
        if not isinstance(transport, str) or transport not in _ALLOWED_TRANSPORTS:
            raise ValueError("unsupported trial transport")
        if not isinstance(records, list):
            raise ValueError("trials must be a list")
        if len(records) > MAX_TRIAL_BATCH:
            raise ValueError(f"too many trials ({len(records)} > {MAX_TRIAL_BATCH})")
        request_id = self._request_id(request_id)
        now = time.time()
        items: list[dict[str, Any]] = []
        accepted = duplicates = rejected = conflicts = pooled = 0
        quarantined = False
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            blocked = conn.execute(
                "SELECT reason FROM quarantined_peers WHERE peer=?", (peer,)
            ).fetchone()
            if blocked is not None:
                conn.commit()
                return {
                    "schema": "avrone.trial-receipt/v1",
                    "status": "quarantined",
                    "peer": peer,
                    "accepted": 0,
                    "duplicates": 0,
                    "rejected": 0,
                    "conflicts": 0,
                    "pooled": 0,
                    "operational": 0,
                    "quarantined": True,
                    "independence": "not_assessed",
                    "items": [self._item(i, "quarantined", r, str(blocked["reason"]))
                              for i, r in enumerate(records)],
                }

            stored_count = conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
            for index, record in enumerate(records):
                if not validate_trial_envelope(record):
                    rejected += 1
                    items.append(self._item(index, "rejected", record, "invalid_envelope"))
                    continue
                encoded = canonical_trial_json(record)
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                existing = conn.execute(
                    "SELECT envelope_sha256 FROM trials WHERE trial_id=?", (record["trial_id"],)
                ).fetchone()
                if existing is None:
                    if stored_count >= MAX_STORED_TRIALS:
                        rejected += 1
                        items.append(self._item(index, "rejected", record, "trial_store_full"))
                        continue
                    is_pooled = record["outcome"] in ("passed", "failed")
                    conn.execute("""
                        INSERT INTO trials (
                            trial_id, envelope_json, envelope_sha256, schema_version, source,
                            system, node, run_id, check_id, check_version, attempt_id, outcome,
                            pooled, first_peer, first_transport, received_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        record["trial_id"], encoded, digest, record["schema"], record["source"],
                        record["system"], record["node"], record["run_id"], record["check_id"],
                        record["check_version"], record["attempt_id"], record["outcome"],
                        int(is_pooled), peer, transport, now,
                    ))
                    conn.execute("""
                        INSERT INTO receipts (
                            trial_id, peer, transport, request_id, first_received_at,
                            last_received_at, delivery_count
                        ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    """, (record["trial_id"], peer, transport, request_id, now, now))
                    accepted += 1
                    stored_count += 1
                    pooled += int(is_pooled)
                    items.append(self._item(index, "accepted", record))
                    continue

                if hmac.compare_digest(str(existing["envelope_sha256"]), digest):
                    conn.execute("""
                        INSERT INTO receipts (
                            trial_id, peer, transport, request_id, first_received_at,
                            last_received_at, delivery_count
                        ) VALUES (?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(trial_id, peer) DO UPDATE SET
                            transport=excluded.transport,
                            request_id=excluded.request_id,
                            last_received_at=excluded.last_received_at,
                            delivery_count=MIN(receipts.delivery_count + 1, 2147483647)
                    """, (record["trial_id"], peer, transport, request_id, now, now))
                    duplicates += 1
                    items.append(self._item(index, "duplicate", record))
                    continue

                conn.execute("""
                    INSERT INTO conflicts (
                        trial_id, peer, existing_sha256, incoming_sha256,
                        incoming_json, reason, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (record["trial_id"], peer, existing["envelope_sha256"], digest,
                      encoded, "same_trial_id_different_payload", now))
                conn.execute("""
                    INSERT INTO quarantined_peers (peer, reason, first_seen, last_seen, conflict_count)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(peer) DO UPDATE SET
                        reason=excluded.reason,
                        last_seen=excluded.last_seen,
                        conflict_count=quarantined_peers.conflict_count + 1
                """, (peer, "duplicate_trial_conflict", now, now))
                conflicts += 1
                quarantined = True
                items.append(self._item(index, "conflict", record, "duplicate_trial_conflict"))
                for later_index in range(index + 1, len(records)):
                    items.append(self._item(later_index, "quarantined", records[later_index],
                                             "peer_quarantined_after_conflict"))
                break
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise TrialStoreError("trial transaction failed; no acceptance is reported") from exc
        finally:
            conn.close()

        if conflicts:
            status = "conflict_quarantined"
        elif rejected and (accepted or duplicates):
            status = "partial_rejected"
        elif rejected:
            status = "rejected"
        elif accepted:
            status = "accepted"
        elif duplicates:
            status = "duplicate"
        else:
            status = "empty"
        return {
            "schema": "avrone.trial-receipt/v1",
            "status": status,
            "peer": peer,
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "conflicts": conflicts,
            "pooled": pooled,
            "operational": accepted - pooled,
            "quarantined": quarantined,
            "independence": "not_assessed",
            "items": items,
        }

    def get_trial(self, trial_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM trials WHERE trial_id=?", (trial_id,)).fetchone()
            if row is None:
                return None
            return {
                "envelope": json.loads(row["envelope_json"]),
                "source": row["source"],
                "check_version": row["check_version"],
                "first_peer": row["first_peer"],
                "first_transport": row["first_transport"],
                "received_at": row["received_at"],
                "pooled": bool(row["pooled"]),
            }
        except (sqlite3.Error, ValueError, RecursionError) as exc:
            raise TrialStoreError("could not read trial provenance") from exc
        finally:
            conn.close()

    def peer_quarantined(self, peer: str) -> bool:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM quarantined_peers WHERE peer=?", (peer,)
            ).fetchone() is not None
        except sqlite3.Error as exc:
            raise TrialStoreError("could not read peer quarantine state") from exc
        finally:
            conn.close()

    def snapshot(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM trials WHERE pooled=1").fetchone()[0]
            if count > MAX_POOL_TRIALS:
                raise TrialStoreError(f"pooled trial cap exceeded ({count} > {MAX_POOL_TRIALS})")
            rows = conn.execute("""
                SELECT source, trial_id, outcome FROM trials
                WHERE pooled=1 ORDER BY received_at, trial_id
            """).fetchall()
            records = [
                {"source": row["source"], "trial_id": row["trial_id"],
                 "failed": row["outcome"] == "failed"}
                for row in rows
            ]
        except sqlite3.Error as exc:
            raise TrialStoreError("could not build Great Scale evidence snapshot") from exc
        finally:
            conn.close()
        try:
            result = pool_trials(records)
        except ValueError as exc:
            raise TrialStoreError("stored trial pool failed validation") from exc
        result["independence"] = "not_assessed"
        return result

    def integrity_check(self) -> str:
        conn = self._connect()
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return str(row[0]) if row else "unknown"
        except sqlite3.Error as exc:
            raise TrialStoreError("trial database integrity check failed") from exc
        finally:
            conn.close()
