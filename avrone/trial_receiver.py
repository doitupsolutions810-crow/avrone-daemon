"""Durable, idempotent receiver for authenticated AVRONE trial envelopes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any

from .evidence_pool import canonical_source_key, pool_trials
from .trial_contract import canonical_trial_bytes, validate_trial_envelope

SENDER_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,62}\Z")
MAX_BATCH = 10
MAX_TRIAL_RECORDS = 100_000
MAX_RECEIPTS = 100_000
MAX_QUARANTINE_RECORDS = 1_000
MAX_QUARANTINED_SENDERS = 1_000
DB_SCHEMA_VERSION = 2  # v0 is unversioned; v1 adds per-delivery bridge provenance


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

    def _initialize(self) -> None:
        db = self._connect()
        try:
            # Refuse a future schema before changing journal mode or database metadata.
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > DB_SCHEMA_VERSION:
                raise TrialStoreError("trial database schema is newer than this receiver")
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise TrialStoreError("SQLite WAL mode is unavailable")
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
                    received_at REAL NOT NULL
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
                    received_at REAL NOT NULL
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
                    received_at REAL NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS trial_quarantined_senders (
                    sender TEXT PRIMARY KEY,
                    first_trial_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at REAL NOT NULL
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
                },
                "trial_receipts": {
                    "receipt_id", "trial_id", "sender", "transport", "bridge_run_id", "result",
                    "payload_sha256", "received_at",
                },
                "trial_quarantine": {
                    "quarantine_id", "trial_id", "sender", "transport", "bridge_run_id", "reason",
                    "incoming_sha256", "incoming_json", "received_at",
                },
                "trial_quarantined_senders": {
                    "sender", "first_trial_id", "reason", "quarantined_at",
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
                missing = required - present
                if missing:
                    names = ", ".join(sorted(missing))
                    raise TrialStoreError(f"trial database schema is missing required columns in {table}: {names}")

            if version < DB_SCHEMA_VERSION:
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
        db.execute(
            "INSERT INTO trial_receipts(trial_id,sender,transport,bridge_run_id,result,payload_sha256,received_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (trial_id, sender, transport, bridge_run_id, result, payload_sha256, received_at),
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
            quarantined = db.execute(
                "SELECT 1 FROM trial_quarantined_senders WHERE sender=?", (sender,)
            ).fetchone()
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
                                first_sender,first_transport,bridge_run_id,received_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                trial_id, source_identity_key, raw, digest, envelope["outcome"], envelope["system"],
                                envelope["node"], envelope["source"], envelope["run_id"],
                                envelope["check_id"], envelope["check_version"],
                                envelope["check_config_sha256"], envelope["attempt_id"],
                                sender, transport, bridge_run_id, now,
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
                            trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,incoming_json,received_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            trial_id, sender, transport, bridge_run_id,
                            conflict_reason, digest, raw, now,
                        ),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO trial_quarantined_senders"
                        "(sender,first_trial_id,reason,quarantined_at) VALUES(?,?,?,?)",
                        (
                            sender, trial_id, conflict_reason, now,
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

    def pool_snapshot(self) -> dict[str, Any]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT trial_id,source,outcome FROM trials "
                "WHERE outcome IN ('pass','fail') ORDER BY trial_id"
            ).fetchall()
            return pool_trials([
                {"trial_id": row["trial_id"], "source": row["source"],
                 "failed": row["outcome"] == "fail"}
                for row in rows
            ])
        except sqlite3.Error as exc:
            raise TrialStoreError("trial pool read failed") from exc
        finally:
            db.close()

    def non_pass_records(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT trial_id,source,system,node,run_id,check_id,check_version,"
                "check_config_sha256,attempt_id,outcome,first_sender,first_transport,"
                "bridge_run_id,received_at FROM trials "
                "WHERE outcome NOT IN ('pass','fail') ORDER BY received_at,trial_id"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise TrialStoreError("non-pass trial read failed") from exc
        finally:
            db.close()

    def quarantine_records(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,received_at "
                "FROM trial_quarantine ORDER BY quarantine_id"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise TrialStoreError("trial quarantine read failed") from exc
        finally:
            db.close()
