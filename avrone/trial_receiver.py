"""Durable, idempotent receiver for authenticated AVRONE trial envelopes."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any

from .evidence_pool import canonical_source_key, pool_trials
from .trial_contract import MAX_ENVELOPE_BYTES, canonical_trial_bytes, validate_trial_envelope

SENDER_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,62}\Z")
MAX_BATCH = 10
MAX_TRIAL_RECORDS = 100_000
MAX_RECEIPTS = 100_000
MAX_QUARANTINE_RECORDS = 1_000
MAX_QUARANTINED_SENDERS = 1_000
DB_SCHEMA_VERSION = 3  # v3 binds persisted first-receipt, delivery, and quarantine provenance
WAL_MODE_RETRY_SECONDS = 10.0
WAL_MODE_RETRY_INITIAL_SECONDS = 0.01
WAL_MODE_RETRY_MAX_SECONDS = 0.25


_PROVENANCE_RESULTS = frozenset({
    "accepted", "duplicate", "non_pass_stored", "conflict_quarantined", "sender_quarantined",
})
_PROVENANCE_REASONS = frozenset({
    "canonical source/check/attempt identity arrived with a different trial_id",
    "same trial_id arrived with a different canonical envelope",
})


def _provenance_time(value: Any) -> str:
    """Encode a finite SQLite timestamp as its exact binary64 value."""
    if type(value) not in (int, float):
        raise ValueError("provenance timestamp must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("provenance timestamp must be finite and non-negative")
    return number.hex()


def _provenance_sha256(kind: str, fields: dict[str, Any]) -> str:
    """Domain-separated digest for mutable receiver metadata (not a keyed signature)."""
    normalized = dict(fields)
    for key in ("received_at", "quarantined_at"):
        if key in normalized:
            normalized[key] = _provenance_time(normalized[key])
    raw = json.dumps(
        {"domain": "avrone.trial-provenance/v1", "kind": kind, "fields": normalized},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class TrialStoreError(RuntimeError):
    """The receiver cannot safely persist or continue accepting trial evidence."""


def _source_identity_key(envelope: dict[str, Any]) -> str:
    """Hash source/attempt identity using the evidence pool's source equivalence rules.

    The wire trial_id remains v2-compatible. This storage key prevents NFC-distinct
    but evidence-pool-equivalent source spellings from counting one attempt twice.
    """
    identity = {
        "schema": envelope["schema"],
        "source_key": canonical_source_key(envelope["source"]),
        "run_id": envelope["run_id"],
        "check_id": envelope["check_id"],
        "check_version": envelope["check_version"],
        "check_config_sha256": envelope["check_config_sha256"],
        "attempt_id": envelope["attempt_id"],
    }
    raw = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class TrialStore:
    """SQLite WAL store. One transaction per envelope; conflicts never replace first-seen data."""

    def __init__(self, path: Path | str):
        self.path = Path(os.path.abspath(os.fspath(path)))
        # Check existing ancestors before mkdir: checking only the final parent misses
        # symlinks such as data-link/trials/, which can escape the configured data root.
        self._assert_no_symlink_components(self.path.parent, "trial data directory")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_no_symlink_components(self.path.parent, "trial data directory")
        os.chmod(self.path.parent, 0o700)
        self._assert_database_file()
        self._initialize()

    @staticmethod
    def _assert_no_symlink_components(path: Path, label: str) -> None:
        """Reject symlinks in every lexical component, not just the leaf directory."""
        absolute = Path(os.path.abspath(os.fspath(path)))
        for component in (absolute, *absolute.parents):
            try:
                linked = component.is_symlink()
            except OSError as exc:
                raise TrialStoreError(f"{label} path cannot be inspected") from exc
            if linked:
                raise TrialStoreError(f"{label} must not contain symlinked path components")

    def _assert_database_file(self) -> None:
        """Reject non-regular and multiply-linked database files before SQLite opens them."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TrialStoreError("trial database path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise TrialStoreError("trial database must not be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise TrialStoreError("trial database must be a regular file")
        if info.st_nlink != 1:
            raise TrialStoreError("trial database must not be a hard link")

    def _connect(self) -> sqlite3.Connection:
        self._assert_no_symlink_components(self.path.parent, "trial data directory")
        self._assert_database_file()
        try:
            db = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.Error as exc:
            raise TrialStoreError("trial database is unavailable") from exc

    @staticmethod
    def _enable_wal(db: sqlite3.Connection) -> None:
        """Enable WAL while tolerating bounded contention between first-time openers.

        SQLite may return SQLITE_BUSY/SQLITE_LOCKED from journal_mode=WAL before
        BEGIN IMMEDIATE is reached. The connection busy_timeout does not reliably
        serialize this journal-mode transition across simultaneous fresh openers,
        so retry only those transient lock errors with a bounded monotonic deadline.
        """
        deadline = time.monotonic() + WAL_MODE_RETRY_SECONDS
        delay = WAL_MODE_RETRY_INITIAL_SECONDS
        while True:
            try:
                mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                break
            except sqlite3.OperationalError as exc:
                error_code = getattr(exc, "sqlite_errorcode", None)
                base_code = error_code & 0xFF if isinstance(error_code, int) else None
                message = str(exc).lower()
                transient_lock = (
                    base_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or "database is locked" in message
                    or "database is busy" in message
                )
                remaining = deadline - time.monotonic()
                if not transient_lock or remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, WAL_MODE_RETRY_MAX_SECONDS)

        if str(mode).lower() != "wal":
            raise TrialStoreError("SQLite WAL mode is unavailable")


    @classmethod
    def _backfill_provenance(cls, db: sqlite3.Connection) -> None:
        """Backfill v3 metadata digests in bounded batches inside the schema transaction."""
        cursor = db.execute(
            "SELECT trial_id,source_identity_key,payload_sha256,first_sender,first_transport,"
            "bridge_run_id,received_at FROM trials ORDER BY trial_id"
        )
        seen = 0
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            seen += len(batch)
            if seen > MAX_TRIAL_RECORDS:
                raise TrialStoreError("existing trial provenance exceeds the record bound")
            for row in batch:
                if (not isinstance(row["first_sender"], str)
                        or not SENDER_RE.fullmatch(row["first_sender"])
                        or row["first_transport"] not in {"fleet", "bridge"}):
                    raise TrialStoreError("existing trial provenance cannot be validated for migration")
                if row["first_transport"] == "bridge":
                    if not cls._valid_bridge_run_id(row["bridge_run_id"]):
                        raise TrialStoreError("existing trial bridge provenance cannot be validated for migration")
                elif row["bridge_run_id"] is not None:
                    raise TrialStoreError("existing trial fleet provenance cannot be validated for migration")
                cls._verify_received_at(row["received_at"], "trial migration")
                digest = _provenance_sha256("trial-first-receipt", {
                    "trial_id": row["trial_id"], "source_identity_key": row["source_identity_key"],
                    "payload_sha256": row["payload_sha256"], "first_sender": row["first_sender"],
                    "first_transport": row["first_transport"], "bridge_run_id": row["bridge_run_id"],
                    "received_at": row["received_at"],
                })
                db.execute("UPDATE trials SET provenance_sha256=? WHERE trial_id=?", (digest, row["trial_id"]))

        cursor = db.execute(
            "SELECT receipt_id,trial_id,sender,transport,bridge_run_id,result,payload_sha256,received_at "
            "FROM trial_receipts ORDER BY receipt_id"
        )
        seen = 0
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            seen += len(batch)
            if seen > MAX_RECEIPTS:
                raise TrialStoreError("existing receipt provenance exceeds the record bound")
            for row in batch:
                if (not isinstance(row["trial_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["trial_id"])
                        or not isinstance(row["sender"], str) or not SENDER_RE.fullmatch(row["sender"])
                        or row["transport"] not in {"fleet", "bridge"}
                        or row["result"] not in _PROVENANCE_RESULTS):
                    raise TrialStoreError("existing receipt provenance cannot be validated for migration")
                if row["transport"] == "bridge":
                    if not cls._valid_bridge_run_id(row["bridge_run_id"]):
                        raise TrialStoreError("existing receipt bridge provenance cannot be validated for migration")
                elif row["bridge_run_id"] is not None:
                    raise TrialStoreError("existing receipt fleet provenance cannot be validated for migration")
                cls._verify_received_at(row["received_at"], "receipt migration")
                digest = _provenance_sha256("delivery-receipt", {
                    "trial_id": row["trial_id"], "sender": row["sender"],
                    "transport": row["transport"], "bridge_run_id": row["bridge_run_id"],
                    "result": row["result"], "payload_sha256": row["payload_sha256"],
                    "received_at": row["received_at"],
                })
                db.execute("UPDATE trial_receipts SET provenance_sha256=? WHERE receipt_id=?", (digest, row["receipt_id"]))

        cursor = db.execute(
            "SELECT quarantine_id,trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,received_at "
            "FROM trial_quarantine ORDER BY quarantine_id"
        )
        seen = 0
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            seen += len(batch)
            if seen > MAX_QUARANTINE_RECORDS:
                raise TrialStoreError("existing quarantine provenance exceeds the record bound")
            for row in batch:
                if (not isinstance(row["trial_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["trial_id"])
                        or not isinstance(row["sender"], str) or not SENDER_RE.fullmatch(row["sender"])
                        or row["transport"] not in {"fleet", "bridge"}
                        or row["reason"] not in _PROVENANCE_REASONS):
                    raise TrialStoreError("existing quarantine provenance cannot be validated for migration")
                if row["transport"] == "bridge":
                    if not cls._valid_bridge_run_id(row["bridge_run_id"]):
                        raise TrialStoreError("existing quarantine bridge provenance cannot be validated for migration")
                elif row["bridge_run_id"] is not None:
                    raise TrialStoreError("existing quarantine fleet provenance cannot be validated for migration")
                cls._verify_received_at(row["received_at"], "quarantine migration")
                digest = _provenance_sha256("conflict-quarantine", {
                    "trial_id": row["trial_id"], "sender": row["sender"],
                    "transport": row["transport"], "bridge_run_id": row["bridge_run_id"],
                    "reason": row["reason"], "incoming_sha256": row["incoming_sha256"],
                    "received_at": row["received_at"],
                })
                db.execute("UPDATE trial_quarantine SET provenance_sha256=? WHERE quarantine_id=?", (digest, row["quarantine_id"]))

        cursor = db.execute(
            "SELECT sender,first_trial_id,reason,quarantined_at FROM trial_quarantined_senders ORDER BY sender"
        )
        seen = 0
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            seen += len(batch)
            if seen > MAX_QUARANTINED_SENDERS:
                raise TrialStoreError("existing sender quarantine provenance exceeds the record bound")
            for row in batch:
                if (not isinstance(row["sender"], str) or not SENDER_RE.fullmatch(row["sender"])
                        or not isinstance(row["first_trial_id"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", row["first_trial_id"])
                        or row["reason"] not in _PROVENANCE_REASONS):
                    raise TrialStoreError("existing sender quarantine provenance cannot be validated for migration")
                cls._verify_received_at(row["quarantined_at"], "sender quarantine migration")
                digest = _provenance_sha256("sender-quarantine-anchor", {
                    "sender": row["sender"], "first_trial_id": row["first_trial_id"],
                    "reason": row["reason"], "quarantined_at": row["quarantined_at"],
                })
                db.execute("UPDATE trial_quarantined_senders SET provenance_sha256=? WHERE sender=?", (digest, row["sender"]))

    def _initialize(self) -> None:
        db = self._connect()
        try:
            # Refuse a future schema before changing journal mode or database metadata.
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > DB_SCHEMA_VERSION:
                raise TrialStoreError("trial database schema is newer than this receiver")
            self._enable_wal(db)
            db.execute("BEGIN IMMEDIATE")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > DB_SCHEMA_VERSION:
                raise TrialStoreError("trial database schema is newer than this receiver")

            # Keep DDL inside the transaction. sqlite3.executescript() commits implicitly and
            # would make a partially applied migration harder to roll back.
            statements = (
                """
                CREATE TABLE IF NOT EXISTS trials (
                    trial_id TEXT PRIMARY KEY,
                    source_identity_key TEXT NOT NULL,
                    canonical_json BLOB NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    system TEXT NOT NULL,
                    node TEXT NOT NULL,
                    source TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    check_id TEXT NOT NULL,
                    check_version TEXT NOT NULL,
                    check_config_sha256 TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    first_sender TEXT NOT NULL,
                    first_transport TEXT NOT NULL,
                    bridge_run_id TEXT,
                    received_at REAL NOT NULL,
                    provenance_sha256 TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS trial_receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    bridge_run_id TEXT,
                    result TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    provenance_sha256 TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS trial_quarantine (
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    bridge_run_id TEXT,
                    reason TEXT NOT NULL,
                    incoming_sha256 TEXT NOT NULL,
                    incoming_json BLOB NOT NULL,
                    received_at REAL NOT NULL,
                    provenance_sha256 TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS trial_quarantined_senders (
                    sender TEXT PRIMARY KEY,
                    first_trial_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at REAL NOT NULL,
                    provenance_sha256 TEXT NOT NULL
                )
                """,
            )
            for statement in statements:
                db.execute(statement)

            required_columns = {
                "trials": {
                    "trial_id", "source_identity_key", "canonical_json", "payload_sha256", "outcome", "system", "node",
                    "source", "run_id", "check_id", "check_version", "check_config_sha256",
                    "attempt_id", "first_sender", "first_transport", "bridge_run_id", "received_at",
                    "provenance_sha256",
                },
                "trial_receipts": {
                    "receipt_id", "trial_id", "sender", "transport", "bridge_run_id", "result",
                    "payload_sha256", "received_at", "provenance_sha256",
                },
                "trial_quarantine": {
                    "quarantine_id", "trial_id", "sender", "transport", "bridge_run_id", "reason",
                    "incoming_sha256", "incoming_json", "received_at", "provenance_sha256",
                },
                "trial_quarantined_senders": {
                    "sender", "first_trial_id", "reason", "quarantined_at", "provenance_sha256",
                },
            }
            for table, required in required_columns.items():
                present = {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not present:
                    raise TrialStoreError(f"trial database table {table} is unavailable")
                if table in {"trial_receipts", "trial_quarantine"} and "bridge_run_id" not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN bridge_run_id TEXT")
                    present.add("bridge_run_id")
                if table == "trials" and "source_identity_key" not in present:
                    db.execute("ALTER TABLE trials ADD COLUMN source_identity_key TEXT")
                    present.add("source_identity_key")
                if "provenance_sha256" not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN provenance_sha256 TEXT")
                    present.add("provenance_sha256")
                missing = required - present
                if missing:
                    names = ", ".join(sorted(missing))
                    raise TrialStoreError(f"trial database schema is missing required columns in {table}: {names}")

            if version < 2:
                rows = db.execute(
                    "SELECT trial_id,canonical_json,source_identity_key FROM trials ORDER BY trial_id"
                ).fetchall()
                for row in rows:
                    try:
                        envelope = json.loads(bytes(row["canonical_json"]).decode("utf-8"))
                        if not validate_trial_envelope(envelope) or envelope["trial_id"] != row["trial_id"]:
                            raise ValueError("stored envelope does not match its trial_id")
                        identity_key = _source_identity_key(envelope)
                    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
                        raise TrialStoreError(
                            "existing trial record cannot be validated for source identity migration"
                        ) from None
                    if row["source_identity_key"] not in (None, identity_key):
                        raise TrialStoreError("existing trial source identity key does not match its envelope")
                    db.execute(
                        "UPDATE trials SET source_identity_key=? WHERE trial_id=?",
                        (identity_key, row["trial_id"]),
                    )
                duplicate = db.execute(
                    "SELECT source_identity_key FROM trials "
                    "GROUP BY source_identity_key HAVING COUNT(*) > 1 LIMIT 1"
                ).fetchone()
                if duplicate:
                    raise TrialStoreError(
                        "existing trial records contain canonical source/attempt aliases; manual review required"
                    )

            if version < 3:
                self._backfill_provenance(db)

            missing_identity_key = db.execute(
                "SELECT 1 FROM trials WHERE source_identity_key IS NULL LIMIT 1"
            ).fetchone()
            if missing_identity_key:
                raise TrialStoreError("trial database contains a missing source identity key")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS trial_source_identity_key "
                "ON trials(source_identity_key)"
            )
            db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trial_source_identity_key_required_insert
                BEFORE INSERT ON trials
                WHEN NEW.source_identity_key IS NULL
                    OR typeof(NEW.source_identity_key) != 'text'
                    OR length(NEW.source_identity_key) != 64
                    OR NEW.source_identity_key GLOB '*[^0-9a-f]*'
                BEGIN
                    SELECT RAISE(ABORT, 'invalid source_identity_key');
                END
                """
            )
            db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trial_source_identity_key_required_update
                BEFORE UPDATE OF source_identity_key ON trials
                WHEN NEW.source_identity_key IS NULL
                    OR typeof(NEW.source_identity_key) != 'text'
                    OR length(NEW.source_identity_key) != 64
                    OR NEW.source_identity_key GLOB '*[^0-9a-f]*'
                BEGIN
                    SELECT RAISE(ABORT, 'invalid source_identity_key');
                END
                """
            )
            for table in ("trials", "trial_receipts", "trial_quarantine", "trial_quarantined_senders"):
                for action in ("INSERT", "UPDATE"):
                    suffix = action.lower()
                    trigger_event = "INSERT" if action == "INSERT" else "UPDATE OF provenance_sha256"
                    db.execute(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS {table}_provenance_sha256_required_{suffix}
                        BEFORE {trigger_event} ON {table}
                        WHEN NEW.provenance_sha256 IS NULL
                            OR typeof(NEW.provenance_sha256) != 'text'
                            OR length(NEW.provenance_sha256) != 64
                            OR NEW.provenance_sha256 GLOB '*[^0-9a-f]*'
                        BEGIN
                            SELECT RAISE(ABORT, 'invalid provenance_sha256');
                        END
                        """
                    )
            db.execute(
                "CREATE INDEX IF NOT EXISTS trial_receipts_trial_id ON trial_receipts(trial_id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS trial_quarantine_trial_id ON trial_quarantine(trial_id)"
            )
            db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            os.chmod(self.path, 0o600)
            db.commit()
        except TrialStoreError:
            db.rollback()
            raise
        except (OSError, sqlite3.Error) as exc:
            db.rollback()
            raise TrialStoreError("trial database initialization failed") from exc
        finally:
            db.close()

    @staticmethod
    def _record_receipt(
        db: sqlite3.Connection, *, trial_id: str, sender: str, transport: str,
        result: str, payload_sha256: str, received_at: float,
        bridge_run_id: str | None = None,
    ) -> None:
        provenance = _provenance_sha256("delivery-receipt", {
            "trial_id": trial_id, "sender": sender, "transport": transport,
            "bridge_run_id": bridge_run_id, "result": result,
            "payload_sha256": payload_sha256, "received_at": received_at,
        })
        db.execute(
            "INSERT INTO trial_receipts(trial_id,sender,transport,bridge_run_id,result,payload_sha256,received_at,provenance_sha256) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (trial_id, sender, transport, bridge_run_id, result, payload_sha256, received_at, provenance),
        )
        db.execute(
            "DELETE FROM trial_receipts WHERE receipt_id IN "
            "(SELECT receipt_id FROM trial_receipts ORDER BY receipt_id DESC LIMIT -1 OFFSET ?)",
            (MAX_RECEIPTS,),
        )

    def ingest_many(
        self, envelopes: Any, *, sender: str, transport: str, bridge_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(sender, str) or not SENDER_RE.fullmatch(sender):
            raise ValueError("authenticated sender must be a fleet node id")
        if not isinstance(transport, str) or transport not in {"fleet", "bridge"}:
            raise ValueError("transport must be fleet or bridge")
        if transport == "bridge":
            if not isinstance(bridge_run_id, str) or not bridge_run_id.strip() or len(bridge_run_id) > 128:
                raise ValueError("bridge run_id must contain 1..128 characters")
            if any(unicodedata.category(ch) == "Cc" for ch in bridge_run_id):
                raise ValueError("bridge run_id must not contain control characters")
            try:
                bridge_run_id.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("bridge run_id must be valid Unicode") from exc
        elif bridge_run_id is not None:
            raise ValueError("bridge_run_id is only valid for bridge transport")
        if not isinstance(envelopes, list) or not 1 <= len(envelopes) <= MAX_BATCH:
            raise ValueError(f"trials must be a list containing 1..{MAX_BATCH} envelopes")

        prepared: list[tuple[dict[str, Any], bytes]] = []
        for envelope in envelopes:
            if not validate_trial_envelope(envelope):
                raise ValueError("invalid avrone.trial/v2 envelope")
            raw = canonical_trial_bytes(envelope)
            prepared.append((envelope, raw))

        results = [
            self._ingest_one(
                envelope, raw, sender=sender, transport=transport, bridge_run_id=bridge_run_id,
            )
            for envelope, raw in prepared
        ]
        statuses = {item["status"] for item in results}
        if len(statuses) > 1:
            overall = "partial"
        elif statuses:
            overall = next(iter(statuses))
        else:
            overall = "rejected"
        return {
            "schema": "avrone.trial-ingest-result/v1",
            "status": overall,
            "accepted": sum(item["status"] == "accepted" for item in results),
            "duplicates": sum(item["status"] == "duplicate" for item in results),
            "non_pass": sum(item["status"] == "non_pass_stored" for item in results),
            "quarantined": sum(
                item["status"] in {"conflict_quarantined", "sender_quarantined"}
                for item in results
            ),
            "results": results,
        }

    @classmethod
    def _verify_sender_quarantine_state(
        cls, db: sqlite3.Connection, sender: str,
    ) -> bool:
        """Fail closed if a sender quarantine row disagrees with its first conflict record."""
        if not isinstance(sender, str) or not SENDER_RE.fullmatch(sender):
            raise TrialStoreError("trial quarantine sender identity integrity check failed")
        state = db.execute(
            "SELECT sender,first_trial_id,reason,quarantined_at,provenance_sha256 "
            "FROM trial_quarantined_senders WHERE sender=?",
            (sender,),
        ).fetchone()
        rows = db.execute(
            "SELECT quarantine_id,trial_id,sender,transport,bridge_run_id,reason,"
            "incoming_sha256,incoming_json,received_at,provenance_sha256 FROM trial_quarantine "
            "WHERE sender=? ORDER BY quarantine_id",
            (sender,),
        ).fetchmany(MAX_QUARANTINE_RECORDS + 1)
        if len(rows) > MAX_QUARANTINE_RECORDS:
            raise TrialStoreError("trial sender quarantine integrity check exceeds the record bound")
        for row in rows:
            cls._verify_quarantine_row(row)
        first = rows[0] if rows else None
        if state is None:
            if first is not None:
                raise TrialStoreError("trial sender quarantine state integrity check failed")
            return False
        if first is None:
            raise TrialStoreError("trial sender quarantine state integrity check failed")
        cls._verify_received_at(state["quarantined_at"], "sender quarantine")
        if (
            state["sender"] != sender
            or state["first_trial_id"] != first["trial_id"]
            or state["reason"] != first["reason"]
            or state["quarantined_at"] != first["received_at"]
        ):
            raise TrialStoreError("trial sender quarantine state integrity check failed")
        expected_state_digest = _provenance_sha256("sender-quarantine-anchor", {
            "sender": state["sender"], "first_trial_id": state["first_trial_id"],
            "reason": state["reason"], "quarantined_at": state["quarantined_at"],
        })
        if state["provenance_sha256"] != expected_state_digest:
            raise TrialStoreError("trial sender quarantine provenance integrity check failed")
        return True


    def _ingest_one(
        self, envelope: dict[str, Any], raw: bytes, *, sender: str, transport: str,
        bridge_run_id: str | None,
    ) -> dict[str, Any]:
        if not validate_trial_envelope(envelope):
            raise ValueError("invalid avrone.trial/v2 envelope")
        digest = hashlib.sha256(raw).hexdigest()
        now = time.time()
        trial_id = envelope["trial_id"]
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            quarantined = self._verify_sender_quarantine_state(db, sender)
            if quarantined:
                status = "sender_quarantined"
            else:
                existing = db.execute(
                    "SELECT canonical_json FROM trials WHERE trial_id=?", (trial_id,)
                ).fetchone()
                source_identity_key = _source_identity_key(envelope)
                conflict_reason = None
                if existing is None:
                    alias = db.execute(
                        "SELECT trial_id FROM trials WHERE source_identity_key=?",
                        (source_identity_key,),
                    ).fetchone()
                    if alias is None:
                        count = db.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
                        if count >= MAX_TRIAL_RECORDS:
                            raise TrialStoreError("trial store capacity reached")
                        db.execute(
                            """
                            INSERT INTO trials(
                                trial_id,source_identity_key,canonical_json,payload_sha256,outcome,system,node,source,
                                run_id,check_id,check_version,check_config_sha256,attempt_id,
                                first_sender,first_transport,bridge_run_id,received_at,provenance_sha256
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                trial_id, source_identity_key, raw, digest, envelope["outcome"], envelope["system"],
                                envelope["node"], envelope["source"], envelope["run_id"],
                                envelope["check_id"], envelope["check_version"],
                                envelope["check_config_sha256"], envelope["attempt_id"],
                                sender, transport, bridge_run_id, now,
                                _provenance_sha256("trial-first-receipt", {
                                    "trial_id": trial_id, "source_identity_key": source_identity_key,
                                    "payload_sha256": digest, "first_sender": sender,
                                    "first_transport": transport, "bridge_run_id": bridge_run_id,
                                    "received_at": now,
                                }),
                            ),
                        )
                        status = "accepted" if envelope["outcome"] in {"pass", "fail"} else "non_pass_stored"
                    else:
                        conflict_reason = (
                            "canonical source/check/attempt identity arrived with a different trial_id"
                        )
                elif bytes(existing["canonical_json"]) == raw:
                    status = "duplicate"
                else:
                    conflict_reason = "same trial_id arrived with a different canonical envelope"

                if conflict_reason is not None:
                    q_count = db.execute("SELECT COUNT(*) FROM trial_quarantine").fetchone()[0]
                    sender_count = db.execute(
                        "SELECT COUNT(*) FROM trial_quarantined_senders"
                    ).fetchone()[0]
                    if q_count >= MAX_QUARANTINE_RECORDS or (
                        sender_count >= MAX_QUARANTINED_SENDERS
                        and not db.execute(
                            "SELECT 1 FROM trial_quarantined_senders WHERE sender=?", (sender,)
                        ).fetchone()
                    ):
                        raise TrialStoreError("trial conflict quarantine capacity reached")
                    db.execute(
                        """
                        INSERT INTO trial_quarantine(
                            trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,incoming_json,received_at,provenance_sha256
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            trial_id, sender, transport, bridge_run_id,
                            conflict_reason, digest, raw, now,
                            _provenance_sha256("conflict-quarantine", {
                                "trial_id": trial_id, "sender": sender, "transport": transport,
                                "bridge_run_id": bridge_run_id, "reason": conflict_reason,
                                "incoming_sha256": digest, "received_at": now,
                            }),
                        ),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO trial_quarantined_senders"
                        "(sender,first_trial_id,reason,quarantined_at,provenance_sha256) VALUES(?,?,?,?,?)",
                        (
                            sender, trial_id, conflict_reason, now,
                            _provenance_sha256("sender-quarantine-anchor", {
                                "sender": sender, "first_trial_id": trial_id,
                                "reason": conflict_reason, "quarantined_at": now,
                            }),
                        ),
                    )
                    status = "conflict_quarantined"
            self._record_receipt(
                db, trial_id=trial_id, sender=sender, transport=transport,
                result=status, payload_sha256=digest, received_at=now,
                bridge_run_id=bridge_run_id,
            )
            db.commit()
            return {"trial_id": trial_id, "outcome": envelope["outcome"], "status": status}
        except sqlite3.Error as exc:
            db.rollback()
            raise TrialStoreError("trial transaction failed") from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


    @staticmethod
    def _valid_bridge_run_id(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            return False
        if any(unicodedata.category(ch) == "Cc" for ch in value):
            return False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True

    @staticmethod
    def _decode_stored_envelope(raw: Any, label: str) -> tuple[dict[str, Any], bytes]:
        """Decode one stored envelope and require its exact canonical byte representation."""
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise TrialStoreError(f"{label} storage integrity check failed")
        payload = bytes(raw)
        if not payload or len(payload) > MAX_ENVELOPE_BYTES:
            raise TrialStoreError(f"{label} storage integrity check failed")
        try:
            envelope = json.loads(payload.decode("utf-8"))
            if not validate_trial_envelope(envelope):
                raise ValueError("invalid stored envelope")
            canonical = canonical_trial_bytes(envelope)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise TrialStoreError(f"{label} storage integrity check failed") from None
        if canonical != payload:
            # Also rejects duplicate keys, non-canonical key order, alternate escaping,
            # and any other byte-level rewrite that parses to the same Python object.
            raise TrialStoreError(f"{label} canonical encoding integrity check failed")
        return envelope, payload

    @staticmethod
    def _verify_received_at(value: Any, label: str) -> None:
        if type(value) not in (int, float):
            raise TrialStoreError(f"{label} provenance integrity check failed")
        try:
            if not math.isfinite(float(value)) or value < 0:
                raise TrialStoreError(f"{label} provenance integrity check failed")
        except (OverflowError, TypeError, ValueError):
            raise TrialStoreError(f"{label} provenance integrity check failed") from None

    @classmethod
    def _verify_trial_row(cls, row: sqlite3.Row) -> None:
        envelope, raw = cls._decode_stored_envelope(row["canonical_json"], "trial")
        if hashlib.sha256(raw).hexdigest() != row["payload_sha256"]:
            raise TrialStoreError("trial payload digest integrity check failed")
        columns = (
            "trial_id", "outcome", "system", "node", "source", "run_id",
            "check_id", "check_version", "check_config_sha256", "attempt_id",
        )
        if any(row[column] != envelope[column] for column in columns):
            raise TrialStoreError("trial row projection integrity check failed")
        try:
            expected_source_key = _source_identity_key(envelope)
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise TrialStoreError("trial source identity integrity check failed") from None
        if row["source_identity_key"] != expected_source_key:
            raise TrialStoreError("trial source identity integrity check failed")
        sender = row["first_sender"]
        transport = row["first_transport"]
        bridge_run_id = row["bridge_run_id"]
        if not isinstance(sender, str) or not SENDER_RE.fullmatch(sender):
            raise TrialStoreError("trial sender provenance integrity check failed")
        if transport not in {"fleet", "bridge"}:
            raise TrialStoreError("trial transport provenance integrity check failed")
        if transport == "bridge":
            if not cls._valid_bridge_run_id(bridge_run_id):
                raise TrialStoreError("trial bridge provenance integrity check failed")
        elif bridge_run_id is not None:
            raise TrialStoreError("trial bridge provenance integrity check failed")
        cls._verify_received_at(row["received_at"], "trial")
        expected_provenance = _provenance_sha256("trial-first-receipt", {
            "trial_id": row["trial_id"], "source_identity_key": row["source_identity_key"],
            "payload_sha256": row["payload_sha256"], "first_sender": sender,
            "first_transport": transport, "bridge_run_id": bridge_run_id,
            "received_at": row["received_at"],
        })
        if row["provenance_sha256"] != expected_provenance:
            raise TrialStoreError("trial first-receipt provenance integrity check failed")

    @classmethod
    def _iter_verified_trial_rows(cls, db: sqlite3.Connection):
        """Stream and verify every bounded trial row before exposing any evidence projection."""
        cursor = db.execute(
            "SELECT trial_id,source_identity_key,canonical_json,payload_sha256,outcome,"
            "system,node,source,run_id,check_id,check_version,check_config_sha256,attempt_id,"
            "first_sender,first_transport,bridge_run_id,received_at,provenance_sha256 "
            "FROM trials ORDER BY trial_id"
        )
        seen = 0
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            seen += len(batch)
            if seen > MAX_TRIAL_RECORDS:
                raise TrialStoreError("trial storage integrity check exceeds the record bound")
            for row in batch:
                cls._verify_trial_row(row)
                yield row

    @classmethod
    def _verify_quarantine_row(cls, row: sqlite3.Row) -> None:
        envelope, raw = cls._decode_stored_envelope(row["incoming_json"], "quarantine")
        if hashlib.sha256(raw).hexdigest() != row["incoming_sha256"]:
            raise TrialStoreError("quarantine payload digest integrity check failed")
        if row["trial_id"] != envelope["trial_id"]:
            raise TrialStoreError("quarantine trial identity integrity check failed")
        sender = row["sender"]
        transport = row["transport"]
        bridge_run_id = row["bridge_run_id"]
        if not isinstance(sender, str) or not SENDER_RE.fullmatch(sender):
            raise TrialStoreError("quarantine sender provenance integrity check failed")
        if transport not in {"fleet", "bridge"}:
            raise TrialStoreError("quarantine transport provenance integrity check failed")
        if transport == "bridge":
            if not cls._valid_bridge_run_id(bridge_run_id):
                raise TrialStoreError("quarantine bridge provenance integrity check failed")
        elif bridge_run_id is not None:
            raise TrialStoreError("quarantine bridge provenance integrity check failed")
        if row["reason"] not in {
            "canonical source/check/attempt identity arrived with a different trial_id",
            "same trial_id arrived with a different canonical envelope",
        }:
            raise TrialStoreError("quarantine reason integrity check failed")
        cls._verify_received_at(row["received_at"], "quarantine")
        expected_provenance = _provenance_sha256("conflict-quarantine", {
            "trial_id": row["trial_id"], "sender": sender, "transport": transport,
            "bridge_run_id": bridge_run_id, "reason": row["reason"],
            "incoming_sha256": row["incoming_sha256"], "received_at": row["received_at"],
        })
        if row["provenance_sha256"] != expected_provenance:
            raise TrialStoreError("quarantine provenance integrity check failed")


    @classmethod
    def _verify_receipt_row(cls, row: sqlite3.Row) -> None:
        if not isinstance(row["trial_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["trial_id"]):
            raise TrialStoreError("trial receipt identity integrity check failed")
        if not isinstance(row["sender"], str) or not SENDER_RE.fullmatch(row["sender"]):
            raise TrialStoreError("trial receipt sender provenance integrity check failed")
        if row["transport"] not in {"fleet", "bridge"} or row["result"] not in _PROVENANCE_RESULTS:
            raise TrialStoreError("trial receipt state integrity check failed")
        if not isinstance(row["payload_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["payload_sha256"]):
            raise TrialStoreError("trial receipt payload digest integrity check failed")
        if row["transport"] == "bridge":
            if not cls._valid_bridge_run_id(row["bridge_run_id"]):
                raise TrialStoreError("trial receipt bridge provenance integrity check failed")
        elif row["bridge_run_id"] is not None:
            raise TrialStoreError("trial receipt bridge provenance integrity check failed")
        cls._verify_received_at(row["received_at"], "receipt")
        expected = _provenance_sha256("delivery-receipt", {
            "trial_id": row["trial_id"], "sender": row["sender"],
            "transport": row["transport"], "bridge_run_id": row["bridge_run_id"],
            "result": row["result"], "payload_sha256": row["payload_sha256"],
            "received_at": row["received_at"],
        })
        if row["provenance_sha256"] != expected:
            raise TrialStoreError("trial receipt provenance integrity check failed")

    def receipt_records(self) -> list[dict[str, Any]]:
        """Return the bounded delivery audit only after validating every receipt digest."""
        db = self._connect()
        try:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT receipt_id,trial_id,sender,transport,bridge_run_id,result,payload_sha256,"
                "received_at,provenance_sha256 FROM trial_receipts ORDER BY receipt_id"
            ).fetchmany(MAX_RECEIPTS + 1)
            if len(rows) > MAX_RECEIPTS:
                raise TrialStoreError("trial receipt integrity check exceeds the record bound")
            records = []
            for row in rows:
                self._verify_receipt_row(row)
                records.append({key: row[key] for key in (
                    "receipt_id", "trial_id", "sender", "transport", "bridge_run_id",
                    "result", "payload_sha256", "received_at", "provenance_sha256",
                )})
            return records
        except sqlite3.Error as exc:
            raise TrialStoreError("trial receipt read failed") from exc
        finally:
            db.close()

    def pool_snapshot(self) -> dict[str, Any]:
        db = self._connect()
        try:
            # Verify non-pass rows too: corrupted status/provenance must not be hidden
            # merely because the row would otherwise be excluded from the pass/fail pool.
            records = []
            for row in self._iter_verified_trial_rows(db):
                if row["outcome"] in {"pass", "fail"}:
                    records.append({
                        "trial_id": row["trial_id"],
                        "source": row["source"],
                        "failed": row["outcome"] == "fail",
                    })
            return pool_trials(records)
        except sqlite3.Error as exc:
            raise TrialStoreError("trial pool read failed") from exc
        finally:
            db.close()

    def non_pass_records(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            fields = (
                "trial_id", "source", "system", "node", "run_id", "check_id",
                "check_version", "check_config_sha256", "attempt_id", "outcome",
                "first_sender", "first_transport", "bridge_run_id", "received_at",
            )
            records = []
            for row in self._iter_verified_trial_rows(db):
                if row["outcome"] not in {"pass", "fail"}:
                    records.append({field: row[field] for field in fields})
            return sorted(records, key=lambda item: (item["received_at"], item["trial_id"]))
        except sqlite3.Error as exc:
            raise TrialStoreError("non-pass trial read failed") from exc
        finally:
            db.close()

    @classmethod
    def _verified_quarantine_records(
        cls, db: sqlite3.Connection,
    ) -> list[dict[str, Any]]:
        """Verify quarantine payloads and their cross-table sender enforcement state."""
        cursor = db.execute(
            "SELECT quarantine_id,trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,"
            "incoming_json,received_at,provenance_sha256 FROM trial_quarantine ORDER BY quarantine_id"
        )
        records = []
        first_by_sender: dict[str, tuple[str, str, float]] = {}
        for row in cursor:
            if len(records) >= MAX_QUARANTINE_RECORDS:
                raise TrialStoreError("trial quarantine integrity check exceeds the record bound")
            cls._verify_quarantine_row(row)
            first_by_sender.setdefault(
                row["sender"],
                (row["trial_id"], row["reason"], row["received_at"]),
            )
            records.append({
                "trial_id": row["trial_id"],
                "sender": row["sender"],
                "transport": row["transport"],
                "bridge_run_id": row["bridge_run_id"],
                "reason": row["reason"],
                "incoming_sha256": row["incoming_sha256"],
                "received_at": row["received_at"],
                "provenance_sha256": row["provenance_sha256"],
            })

        sender_rows = db.execute(
            "SELECT sender,first_trial_id,reason,quarantined_at,provenance_sha256 "
            "FROM trial_quarantined_senders ORDER BY sender"
        ).fetchmany(MAX_QUARANTINED_SENDERS + 1)
        if len(sender_rows) > MAX_QUARANTINED_SENDERS:
            raise TrialStoreError("trial quarantined-sender integrity check exceeds the record bound")
        for state in sender_rows:
            sender = state["sender"]
            if not isinstance(sender, str) or not SENDER_RE.fullmatch(sender):
                raise TrialStoreError("trial quarantined-sender identity integrity check failed")
            first = first_by_sender.pop(sender, None)
            if first is None:
                raise TrialStoreError("trial quarantined-sender state integrity check failed")
            cls._verify_received_at(state["quarantined_at"], "sender quarantine")
            if (
                state["first_trial_id"] != first[0]
                or state["reason"] != first[1]
                or state["quarantined_at"] != first[2]
            ):
                raise TrialStoreError("trial quarantined-sender state integrity check failed")
            expected_state_digest = _provenance_sha256("sender-quarantine-anchor", {
                "sender": sender, "first_trial_id": state["first_trial_id"],
                "reason": state["reason"], "quarantined_at": state["quarantined_at"],
            })
            if state["provenance_sha256"] != expected_state_digest:
                raise TrialStoreError("trial quarantined-sender provenance integrity check failed")
        if first_by_sender:
            raise TrialStoreError("trial quarantine sender enforcement state integrity check failed")
        return records

    def quarantine_records(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            # Use one read snapshot so quarantine rows and sender enforcement state
            # cannot be observed from different commits.
            db.execute("BEGIN")
            return self._verified_quarantine_records(db)
        except sqlite3.Error as exc:
            raise TrialStoreError("trial quarantine read failed") from exc
        finally:
            db.close()
