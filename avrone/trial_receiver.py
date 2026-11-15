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
DB_SCHEMA_VERSION = 2  # v0 is unversioned; v1 adds per-delivery bridge provenance
WAL_MODE_RETRY_SECONDS = 10.0
WAL_MODE_RETRY_INITIAL_SECONDS = 0.01
WAL_MODE_RETRY_MAX_SECONDS = 0.25


SOURCE_IDENTITY_INSERT_TRIGGER_SQL = """
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
SOURCE_IDENTITY_UPDATE_TRIGGER_SQL = """
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


# Use SQL CASE to avoid transferring oversized stored BLOBs into Python.
# A streaming cursor bounds row *count*, but a single corrupted 500 MB BLOB
# could still exhaust process memory before _verify_stored_trial sees it.
# The bound is a trusted module constant, not input from the SQL caller.
BOUNDED_TRIAL_COLUMNS = (
    "trial_id,source_identity_key,"
    f"CASE WHEN length(canonical_json) > {MAX_ENVELOPE_BYTES} "
    "THEN NULL ELSE canonical_json END AS canonical_json,"
    "payload_sha256,outcome,system,node,source,run_id,check_id,"
    "check_version,check_config_sha256,attempt_id,first_sender,"
    "first_transport,bridge_run_id,received_at"
)
BOUNDED_QUARANTINE_COLUMNS = (
    "quarantine_id,trial_id,sender,transport,bridge_run_id,reason,incoming_sha256,"
    f"CASE WHEN length(incoming_json) > {MAX_ENVELOPE_BYTES} "
    "THEN NULL ELSE incoming_json END AS incoming_json,received_at"
)


# The chronology of retained receipts and quarantine events depends on
# AUTOINCREMENT rather than ordinary INTEGER PRIMARY KEY (whose greatest
# deleted rowid can be recycled). Verify whole canonical table definitions,
# not a regex that could match a misleading SQL comment or an inert suffix.
EXPECTED_EVENT_SEQUENCE_TABLE_SQL = {
    "trial_receipts": """
        CREATE TABLE trial_receipts (
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
    "trial_quarantine": """
        CREATE TABLE trial_quarantine (
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
}


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
        # Never chmod a pre-existing parent such as /tmp. Private storage
        # is required, and the same invariant is checked on every connection.
        self._parent_identity: tuple[int, int] | None = None
        self._database_identity: tuple[int, int] | None = None
        # Allow legacy database modes during the one-time trusted migration;
        # every later open must find the finalized private 0600 database.
        self._initialized = False
        self._assert_private_parent()
        self._assert_database_file()
        self._initialize()
        self._initialized = True

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

    @staticmethod
    def _assert_file_owner(info: os.stat_result, label: str) -> None:
        """Reject cross-user stores/sidecars on POSIX, including UID drift."""
        euid = getattr(os, "geteuid", None)
        if callable(euid) and info.st_uid != euid():
            raise TrialStoreError(f"{label} must be owned by the receiver process")

    def _assert_private_parent(self) -> None:
        """Refuse permission drift instead of chmodding an existing shared directory."""
        try:
            info = self.path.parent.lstat()
        except OSError as exc:
            raise TrialStoreError("trial data directory permissions cannot be inspected") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise TrialStoreError(
                "trial data directory must already be private (mode 0700); "
                "refusing to change an existing directory"
            )
        self._assert_file_owner(info, "trial data directory")
        identity = (info.st_dev, info.st_ino)
        if self._parent_identity is None:
            self._parent_identity = identity
        elif self._parent_identity != identity:
            raise TrialStoreError("trial data directory identity changed since initialization")

    def _assert_database_file(self) -> tuple[int, int] | None:
        """Return the regular single-link SQLite file identity, or None if absent."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TrialStoreError("trial database path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise TrialStoreError("trial database must not be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise TrialStoreError("trial database must be a regular file")
        if info.st_nlink != 1:
            raise TrialStoreError("trial database must not be a hard link")
        self._assert_file_owner(info, "trial database")
        if self._initialized and stat.S_IMODE(info.st_mode) != 0o600:
            raise TrialStoreError("trial database permissions changed after initialization (expected 0600)")
        return info.st_dev, info.st_ino

    def _assert_sidecar_files(self) -> None:
        """Do not let SQLite open planted symlink, device or multiply-linked sidecars.

        SQLite may use -wal, -shm and -journal next to the database filename.
        Legitimate absent or single-link regular sidecars are accepted.
        """
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.path) + suffix)
            try:
                info = sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise TrialStoreError("trial database sidecar cannot be inspected") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TrialStoreError(
                    "trial database sidecar must be a regular single-link file"
                )
            self._assert_file_owner(info, "trial database sidecar")
            # SQLite inherits WAL/SHM mode from the private 0600 database.
            # A running receiver must not silently continue if permissions
            # drift after trusted migration, even in a 0700 directory.
            if self._initialized and stat.S_IMODE(info.st_mode) != 0o600:
                raise TrialStoreError(
                    "trial database sidecar permissions changed after initialization "
                    "(expected 0600)"
                )

    def _create_private_database_once(self) -> tuple[int, int] | None:
        """Create a missing database at 0600 before SQLite can open it.

        sqlite3.connect(path) defaults to creating an absent database using
        ambient process umask. That leaves a short 0644 exposure under umask
        022 before our pre-WAL fchmod runs. Create exclusively at 0600 with
        O_NOFOLLOW and inspect the file descriptor before SQLite opens it.
        Competing initializers are allowed: exactly one can win O_EXCL and
        the others must revalidate the winner's regular single-link inode.
        A previously pinned store must never recreate a missing database.
        """
        if self._database_identity is not None:
            return
        if self._assert_database_file() is not None:
            return
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise TrialStoreError("secure exclusive database creation is unavailable")
        flags = (
            os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = None
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            # The winner may be an independent process initializing the same
            # directory. Validate what actually won instead of chmodding it
            # or overwriting a competing receiver's evidence. Because this
            # filename was absent just before O_EXCL, a newly raced-in 0644
            # file is not a trusted *preexisting* legacy database: refuse it.
            raced_identity = self._assert_database_file()
            if raced_identity is None:
                raise TrialStoreError("trial database vanished during exclusive creation")
            try:
                raced_info = self.path.lstat()
            except OSError as exc:
                raise TrialStoreError(
                    "raced database cannot be inspected securely"
                ) from exc
            if (raced_info.st_dev, raced_info.st_ino) != raced_identity:
                raise TrialStoreError(
                    "competing trial database identity changed during inspection"
                )
            if stat.S_IMODE(raced_info.st_mode) != 0o600:
                raise TrialStoreError(
                    "competing trial database creation must be private (0600)"
                )
            return raced_identity
        except OSError as exc:
            raise TrialStoreError(
                "trial database cannot be created securely"
            ) from exc
        try:
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise TrialStoreError(
                    "new trial database must be a private regular single-link file"
                )
            self._assert_file_owner(info, "trial database")
            if self._assert_database_file() != identity:
                raise TrialStoreError(
                    "trial database identity changed during exclusive creation"
                )
            return identity
        finally:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        self._assert_no_symlink_components(self.path.parent, "trial data directory")
        self._assert_private_parent()
        self._assert_sidecar_files()
        # For a new store, atomically create an owned 0600 file before SQLite
        # itself can create a permissive 0644 file under the process umask.
        created_identity = self._create_private_database_once()
        before = self._assert_database_file()
        # Carry the verified first-created (or winning) inode identity all
        # the way to the sqlite3.connect preflight. Otherwise, a new file
        # could be swapped after our O_EXCL/descriptor checks but before
        # "before" is sampled, and would be silently accepted as trusted.
        if created_identity is not None and before != created_identity:
            raise TrialStoreError(
                "trial database identity changed before SQLite open"
            )
        # A running receiver cannot silently accept a deleted or replaced
        # database and start a fresh evidence history under the same filename.
        if self._database_identity is not None and before != self._database_identity:
            raise TrialStoreError("trial database identity changed since initialization")
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            # Revalidate after sqlite3.connect, which may have followed a changed
            # pathname between the first lstat and its own file open.
            self._assert_no_symlink_components(self.path.parent, "trial data directory")
            self._assert_private_parent()
            self._assert_sidecar_files()
            after = self._assert_database_file()
            if after is None or (before is not None and after != before) or (
                self._database_identity is not None and after != self._database_identity
            ):
                raise TrialStoreError("trial database identity changed during connection")
            if self._database_identity is None:
                self._database_identity = after
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except (sqlite3.Error, TrialStoreError, OSError) as exc:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
            if isinstance(exc, TrialStoreError):
                raise
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

    @staticmethod
    def _assert_source_identity_unique_index(db: sqlite3.Connection) -> None:
        """Refuse a spoofed or weakened index under the expected SQLite name.

        CREATE UNIQUE INDEX IF NOT EXISTS is not a schema-integrity proof:
        another index with that name might be nonunique, partial, or target the
        wrong columns, allowing multiple canonical aliases to enter the pool.
        """
        for info in db.execute("PRAGMA index_list('trials')"):
            if info["name"] != "trial_source_identity_key":
                continue
            columns = [
                col["name"]
                for col in db.execute("PRAGMA index_info('trial_source_identity_key')")
            ]
            if (info["unique"] != 1
                    or info["partial"] != 0
                    or columns != ["source_identity_key"]):
                raise TrialStoreError("trial source identity unique index integrity check failed")
            return
        raise TrialStoreError("trial source identity unique index is missing")

    @staticmethod
    def _assert_source_identity_triggers(db: sqlite3.Connection) -> None:
        """Prove the expected source-key guards still have their actual DDL.

        SQLite's CREATE TRIGGER IF NOT EXISTS silently tolerates a same-name,
        no-op replacement. Compare canonicalized sqlite_master definitions, not
        merely trigger names. SQLite strips "IF NOT EXISTS" when storing SQL.
        """
        for name, expected_sql in (
            ("trial_source_identity_key_required_insert", SOURCE_IDENTITY_INSERT_TRIGGER_SQL),
            ("trial_source_identity_key_required_update", SOURCE_IDENTITY_UPDATE_TRIGGER_SQL),
        ):
            actual = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name=? AND tbl_name='trials'",
                (name,),
            ).fetchone()
            expected = expected_sql.replace(
                "CREATE TRIGGER IF NOT EXISTS", "CREATE TRIGGER", 1,
            )
            if actual is None or not isinstance(actual["sql"], str) or (
                " ".join(actual["sql"].split())
                != " ".join(expected.split())
            ):
                raise TrialStoreError(
                    "trial source identity trigger integrity check failed: " + name
                )

    @staticmethod
    def _assert_primary_key_schema(db: sqlite3.Connection) -> None:
        """Refuse same-name tables whose primary keys have been weakened.

        CREATE TABLE IF NOT EXISTS verifies neither constraints nor key types.
        On a recreated table, a missing trial_id or receipt_id PRIMARY KEY can
        permit duplicate first-seen evidence or ambiguous audit identities.
        """
        for table, column, expected_type in (
            ("trials", "trial_id", "TEXT"),
            ("trial_receipts", "receipt_id", "INTEGER"),
            ("trial_quarantine", "quarantine_id", "INTEGER"),
            ("trial_quarantined_senders", "sender", "TEXT"),
        ):
            columns = list(db.execute(f"PRAGMA table_info({table})"))
            primary_keys = [row for row in columns if row["pk"]]
            if (
                len(primary_keys) != 1
                or primary_keys[0]["name"] != column
                or primary_keys[0]["pk"] != 1
                or primary_keys[0]["type"].upper() != expected_type
            ):
                raise TrialStoreError(
                    "trial database primary key integrity check failed: " + table
                )

    @staticmethod
    def _assert_event_sequence_schema(db: sqlite3.Connection) -> None:
        """Attest SQLite event-table DDL including AUTOINCREMENT semantics.

        PRAGMA table_info only proves INTEGER PRIMARY KEY, not that an
        INTEGER rowid cannot be reused after deleting the highest event.
        sqlite_master.sql persists the table declaration (without
        IF NOT EXISTS). Comparing every column rejects no-op comment
        substitutions, reordered constraints, and downgraded ID semantics.
        """
        for table, expected in EXPECTED_EVENT_SEQUENCE_TABLE_SQL.items():
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            actual_sql = None if ddl is None else ddl["sql"]
            # A trusted legacy table may have been upgraded by
            # ALTER TABLE ... ADD COLUMN bridge_run_id TEXT. SQLite appends
            # that column *after* received_at, whereas fresh databases place
            # it before result/reason. Both layouts are safe when every
            # column and constraint matches a known canonical definition.
            # SQLite also rewrites whitespace around punctuation during
            # ALTER; normalize only harmless whitespace, not SQL tokens.
            def normalized(statement: str) -> str:
                return re.sub(
                    r"\s*([(),])\s*", r"\1",
                    " ".join(statement.split()).upper(),
                )

            without_bridge = expected.replace("bridge_run_id TEXT,", "")
            appended_bridge = without_bridge.replace(
                "received_at REAL NOT NULL",
                "received_at REAL NOT NULL, bridge_run_id TEXT",
            )
            expected_layouts = {
                normalized(expected), normalized(appended_bridge),
            }
            if (
                not isinstance(actual_sql, str)
                or normalized(actual_sql) not in expected_layouts
            ):
                raise TrialStoreError(
                    "trial event sequence schema integrity check failed: " + table
                )
            # sqlite_sequence can be edited separately from sqlite_master.
            # Without this check, deleting/replacing its row or rewinding
            # seq can undermine monotone event provenance despite a valid
            # AUTOINCREMENT table declaration. An unused table legitimately
            # has no sqlite_sequence entry; a used table cannot.
            key = "receipt_id" if table == "trial_receipts" else "quarantine_id"
            largest = db.execute(
                f"SELECT MAX({key}) FROM {table}"
            ).fetchone()[0]
            sequences = list(db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name=? LIMIT 2",
                (table,),
            ))
            if (
                len(sequences) > 1
                or (largest is not None and not sequences)
                or (
                    sequences
                    and (
                        type(sequences[0]["seq"]) is not int
                        or sequences[0]["seq"] < 1
                        or (
                            largest is not None
                            and sequences[0]["seq"] < largest
                        )
                    )
                )
            ):
                raise TrialStoreError(
                    "trial event sequence state integrity check failed: " + table
                )

    def _make_database_private_before_wal(self) -> None:
        """Set 0600 on the pinned database inode, never on an unchecked path.

        SQLite's WAL/SHM files inherit the main DB mode, so this must run
        before switching journal_mode=WAL. os.chmod(path) would follow a
        symlink planted after _connect() and could change unrelated files.
        The verified O_NOFOLLOW descriptor is also checked against the
        database inode observed after sqlite3.connect.
        """
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None or not hasattr(os, "fchmod"):
            raise TrialStoreError(
                "secure descriptor-based database permissions are unavailable"
            )
        flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        fd = None
        try:
            self._assert_no_symlink_components(
                self.path.parent, "trial data directory"
            )
            self._assert_private_parent()
            fd = os.open(self.path, flags)
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TrialStoreError(
                    "trial database descriptor must be a regular single-link file"
                )
            self._assert_file_owner(info, "trial database")
            if (
                self._database_identity is None
                or identity != self._database_identity
                or self._assert_database_file() != identity
            ):
                raise TrialStoreError(
                    "trial database identity changed before permission finalization"
                )
            os.fchmod(fd, 0o600)
            # Detect a pathname replacement during fchmod. The verified
            # descriptor ensures a swapped target was never chmodded.
            if self._assert_database_file() != identity:
                raise TrialStoreError(
                    "trial database identity changed during permission finalization"
                )
        except OSError as exc:
            raise TrialStoreError(
                "trial database cannot be secured before SQLite WAL mode"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)

    def _initialize(self) -> None:
        db = self._connect()
        try:
            # Refuse a future schema before changing journal mode or database metadata.
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > DB_SCHEMA_VERSION:
                raise TrialStoreError("trial database schema is newer than this receiver")
            # Make the SQLite inode private before WAL/SHM creation. Pin the
            # inode and refuse path swaps rather than following unchecked
            # symlinks with os.chmod(path).
            self._make_database_private_before_wal()
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

            self._assert_primary_key_schema(db)
            self._assert_event_sequence_schema(db)

            if version < DB_SCHEMA_VERSION:
                # Do not fetch all canonical trial BLOBs into Python memory
                # during legacy migration. Bound the number of records and
                # avoid even transferring a giant malformed BLOB via CASE.
                rows = db.execute(
                    "SELECT trial_id, "
                    "CASE WHEN length(canonical_json) > ? THEN NULL "
                    "ELSE canonical_json END AS canonical_json, "
                    "source_identity_key FROM trials ORDER BY trial_id LIMIT ?",
                    (MAX_ENVELOPE_BYTES, MAX_TRIAL_RECORDS + 1),
                )
                migrated = 0
                for row in rows:
                    if migrated >= MAX_TRIAL_RECORDS:
                        raise TrialStoreError(
                            "existing trial count exceeds source identity migration capacity"
                        )
                    try:
                        raw = row["canonical_json"]
                        if not isinstance(raw, bytes):
                            raise ValueError("legacy trial evidence is not a bounded byte envelope")
                        envelope = json.loads(raw.decode("utf-8"))
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
                    migrated += 1
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
            self._assert_source_identity_unique_index(db)
            db.execute(SOURCE_IDENTITY_INSERT_TRIGGER_SQL)
            db.execute(SOURCE_IDENTITY_UPDATE_TRIGGER_SQL)
            self._assert_source_identity_triggers(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS trial_receipts_trial_id ON trial_receipts(trial_id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS trial_quarantine_trial_id ON trial_quarantine(trial_id)"
            )
            db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            # DB permissions were pinned and finalized before enabling WAL;
            # never follow the pathname a second time after migration.
            db.commit()
            # Detect replacement between the pre-WAL descriptor verification
            # and the end of schema initialization. SQLite itself still uses a
            # pathname-based open, so this check is defense in depth, not a
            # substitute for an openat-style SQLite VFS.
            self._assert_no_symlink_components(
                self.path.parent, "trial data directory"
            )
            self._assert_private_parent()
            if self._assert_database_file() != self._database_identity:
                raise TrialStoreError(
                    "trial database identity changed during initialization"
                )
        except TrialStoreError:
            db.rollback()
            raise
        except (OSError, sqlite3.Error) as exc:
            db.rollback()
            raise TrialStoreError("trial database initialization failed") from exc
        finally:
            db.close()

    @staticmethod
    def _verify_stored_provenance(*, sender: Any, transport: Any,
                                  bridge_run_id: Any, received_at: Any) -> None:
        """Validate bounded receiver-authenticated metadata during evidence reads."""
        if not isinstance(sender, str) or not SENDER_RE.fullmatch(sender):
            raise ValueError("stored sender is invalid")
        if transport not in {"fleet", "bridge"}:
            raise ValueError("stored transport is invalid")
        if transport == "bridge":
            if (not isinstance(bridge_run_id, str)
                    or not bridge_run_id.strip()
                    or len(bridge_run_id) > 128
                    or any(unicodedata.category(ch) == "Cc" for ch in bridge_run_id)):
                raise ValueError("stored bridge provenance is invalid")
            try:
                bridge_run_id.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("stored bridge provenance is not valid Unicode") from exc
        elif bridge_run_id is not None:
            raise ValueError("fleet transport must not have bridge provenance")
        if (isinstance(received_at, bool)
                or not isinstance(received_at, (int, float))
                or not math.isfinite(received_at)
                or received_at < 0):
            raise ValueError("stored receipt time is invalid")

    @staticmethod
    def _verify_stored_trial(row: sqlite3.Row) -> dict[str, Any]:
        """Detect stored projection/digest mismatches before presenting evidence.

        This catches corruption and inconsistent edits. It is not a cryptographic
        attestation against a privileged writer able to replace all fields.
        """
        fields = (
            "trial_id", "system", "node", "source", "run_id", "check_id",
            "check_version", "check_config_sha256", "attempt_id", "outcome",
        )
        try:
            raw = row["canonical_json"]
            if not isinstance(raw, bytes) or len(raw) > MAX_ENVELOPE_BYTES:
                raise ValueError("stored canonical JSON must be bounded bytes")
            envelope = json.loads(raw.decode("utf-8"))
            if canonical_trial_bytes(envelope) != raw:
                raise ValueError("stored envelope is not canonical")
            if row["payload_sha256"] != hashlib.sha256(raw).hexdigest():
                raise ValueError("stored digest differs from canonical bytes")
            if row["source_identity_key"] != _source_identity_key(envelope):
                raise ValueError("stored source identity differs from canonical bytes")
            if any(row[field] != envelope[field] for field in fields):
                raise ValueError("stored trial projection differs from canonical bytes")
            TrialStore._verify_stored_provenance(
                sender=row["first_sender"],
                transport=row["first_transport"],
                bridge_run_id=row["bridge_run_id"],
                received_at=row["received_at"],
            )
            return envelope
        except (TypeError, ValueError, KeyError, UnicodeError, RecursionError,
                OverflowError, sqlite3.Error) as exc:
            raise TrialStoreError("stored trial evidence integrity check failed") from exc

    def _verified_trial_rows(self, db: sqlite3.Connection) -> list[dict[str, Any]]:
        """Verify one trial at a time and retain only its small report projection.

        Do not materialize up to 100,000 canonical_json BLOBs in Python memory.
        A LIMIT max+1 sentinel prevents silently truncating corrupt oversized
        tables while the cursor retains only one canonical row at a time.
        """
        fields = (
            "trial_id", "source", "system", "node", "run_id", "check_id",
            "check_version", "check_config_sha256", "attempt_id", "outcome",
            "first_sender", "first_transport", "bridge_run_id", "received_at",
        )
        verified: list[dict[str, Any]] = []
        trial_ids: set[str] = set()
        source_keys: set[str] = set()
        cursor = db.execute(
            f"SELECT {BOUNDED_TRIAL_COLUMNS} FROM trials ORDER BY trial_id LIMIT ?",
            (MAX_TRIAL_RECORDS + 1,),
        )
        for row in cursor:
            if len(verified) >= MAX_TRIAL_RECORDS:
                raise TrialStoreError("stored trial count exceeds receiver capacity")
            self._verify_stored_trial(row)
            trial_id = row["trial_id"]
            if trial_id in trial_ids:
                raise TrialStoreError("stored duplicate trial identity")
            trial_ids.add(trial_id)
            key = row["source_identity_key"]
            if key in source_keys:
                raise TrialStoreError("stored trial source identity alias collision")
            source_keys.add(key)
            verified.append({field: row[field] for field in fields})
        return verified

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
            # Persist a private snapshot reconstructed from the validated canonical bytes.
            # Callers may mutate a shared envelope before the per-row transaction runs;
            # never pair those old bytes with later-mutated metadata or outcome fields.
            prepared.append((json.loads(raw), raw))

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
        try:
            now = time.time()
        except (OSError, OverflowError, ValueError) as exc:
            raise TrialStoreError("receiver clock could not provide a receipt timestamp") from exc
        # SQLite may silently turn NaN into NULL. Never commit a receipt,
        # first-seen trial, or quarantine marker with invalid chronology.
        # Check before opening a transaction so no partial evidence is written.
        # Use exact built-in number types: a float/int subclass may override
        # comparisons or conversion, and SQLite INTEGERs must fit in int64.
        # Checking the integer bound before math.isfinite avoids OverflowError
        # when the clock returns an enormous Python integer (e.g. 10**1000).
        if (
            type(now) not in (int, float)
            or now < 0
            or (type(now) is int and now > (1 << 63) - 1)
            or not math.isfinite(now)
        ):
            raise TrialStoreError("receiver clock returned an invalid receipt timestamp")
        trial_id = envelope["trial_id"]
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            # Startup schema validation alone is insufficient: a running
            # receiver might outlive a replaced/removed uniqueness index.
            # Check while holding SQLite's writer lock, preventing a DDL swap
            # between the index proof and inserting first-seen evidence.
            self._assert_primary_key_schema(db)
            self._assert_event_sequence_schema(db)
            self._assert_source_identity_unique_index(db)
            self._assert_source_identity_triggers(db)
            quarantined = self._assert_sender_quarantine_state(db, sender)
            if quarantined:
                marker = db.execute(
                    "SELECT quarantined_at FROM trial_quarantined_senders WHERE sender=?",
                    (sender,),
                ).fetchone()
                if marker is None or now < marker["quarantined_at"]:
                    raise TrialStoreError(
                        "receiver clock precedes sender quarantine evidence"
                    )
                status = "sender_quarantined"
            else:
                existing = db.execute(
                    f"SELECT {BOUNDED_TRIAL_COLUMNS} FROM trials WHERE trial_id=?", (trial_id,)
                ).fetchone()
                if existing is not None:
                    self._verify_stored_trial(existing)
                source_identity_key = _source_identity_key(envelope)
                conflict_reason = None
                if existing is None:
                    alias = db.execute(
                        f"SELECT {BOUNDED_TRIAL_COLUMNS} FROM trials WHERE source_identity_key=?",
                        (source_identity_key,),
                    ).fetchone()
                    if alias is not None:
                        self._verify_stored_trial(alias)
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
                    if now < existing["received_at"]:
                        raise TrialStoreError(
                            "receiver clock precedes original trial evidence"
                        )
                    status = "duplicate"
                else:
                    conflict_reason = "same trial_id arrived with a different canonical envelope"

                if conflict_reason is not None:
                    # A conflict is causally later than its first-seen
                    # canonical trial, even if the system wall clock steps
                    # backward. Refuse a new inconsistent record before any
                    # quarantine or delivery receipt can be persisted.
                    original = existing if existing is not None else alias
                    if original is None or now < original["received_at"]:
                        raise TrialStoreError(
                            "receiver clock precedes original trial evidence"
                        )
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

    def _verify_receipt_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> None:
        """Fail closed on retained receipt claims unsupported by stored evidence.

        The receiver intentionally prunes old receipts. Only retained rows are
        verified; this does not reconstruct deleted history or independently
        authenticate a privileged writer who can alter all SQLite tables.
        """
        try:
            receipt_id = row["receipt_id"]
            if type(receipt_id) is not int or receipt_id <= 0:
                raise ValueError("invalid receipt sequence")
            trial_id = row["trial_id"]
            digest = row["payload_sha256"]
            if not isinstance(trial_id, str) or re.fullmatch(r"[0-9a-f]{64}", trial_id) is None:
                raise ValueError("invalid receipt trial identity")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("invalid receipt digest")
            self._verify_stored_provenance(
                sender=row["sender"], transport=row["transport"],
                bridge_run_id=row["bridge_run_id"], received_at=row["received_at"],
            )
            status = row["result"]
            if status in {"accepted", "duplicate", "non_pass_stored"}:
                first = db.execute(
                    f"SELECT {BOUNDED_TRIAL_COLUMNS} FROM trials WHERE trial_id=?", (trial_id,)
                ).fetchone()
                if first is None:
                    raise ValueError("receipt has no stored trial")
                self._verify_stored_trial(first)
                if first["payload_sha256"] != digest:
                    raise ValueError("receipt digest disagrees with first-seen evidence")
                if status == "duplicate" and row["received_at"] < first["received_at"]:
                    raise ValueError("duplicate receipt precedes its first-seen trial")
                if status == "accepted" and first["outcome"] not in {"pass", "fail"}:
                    raise ValueError("accepted receipt contradicts outcome")
                if status == "non_pass_stored" and first["outcome"] in {"pass", "fail"}:
                    raise ValueError("non-pass receipt contradicts outcome")
                if status != "duplicate" and (
                    row["sender"] != first["first_sender"]
                    or row["transport"] != first["first_transport"]
                    or row["bridge_run_id"] != first["bridge_run_id"]
                    or row["received_at"] != first["received_at"]
                ):
                    raise ValueError("first receipt provenance disagrees with trial")
            elif status == "conflict_quarantined":
                rows = db.execute(
                    f"SELECT {BOUNDED_QUARANTINE_COLUMNS} FROM trial_quarantine WHERE sender=? "
                    "ORDER BY quarantine_id LIMIT 2",
                    (row["sender"],),
                ).fetchall()
                if len(rows) != 1:
                    raise ValueError("conflict receipt has no unique quarantine evidence")
                conflict = rows[0]
                self._assert_quarantine_conflict_semantics(db, conflict)
                self._assert_sender_quarantine_state(db, row["sender"])
                if (
                    conflict["trial_id"] != trial_id
                    or conflict["incoming_sha256"] != digest
                    or conflict["transport"] != row["transport"]
                    or conflict["bridge_run_id"] != row["bridge_run_id"]
                    or conflict["received_at"] != row["received_at"]
                ):
                    raise ValueError("receipt disagrees with preserved conflict")
            elif status == "sender_quarantined":
                if not self._assert_sender_quarantine_state(db, row["sender"]):
                    raise ValueError("blocked receipt has no quarantine marker")
                marker = db.execute(
                    "SELECT quarantined_at FROM trial_quarantined_senders WHERE sender=?",
                    (row["sender"],),
                ).fetchone()
                if marker is None or row["received_at"] < marker["quarantined_at"]:
                    raise ValueError("blocked receipt predates sender quarantine")
                # The original rejected envelope is intentionally not retained
                # for repeat deliveries. Only the digest's syntax is verifiable.
            else:
                raise ValueError("unrecognized receipt result")
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError,
                OverflowError, sqlite3.Error, TrialStoreError) as exc:
            raise TrialStoreError("stored trial receipt integrity check failed") from exc

    @staticmethod
    def _audit_sql_authorizer(
        operation: int, name: str | None, argument: str | None,
        database: str | None, context: str | None,
    ) -> int:
        """Restrict audit SQL to reads after the WAL snapshot is pinned.

        query_only=ON can itself be disabled from the same SQLite connection.
        An authorizer installed after BEGIN prevents an accidental helper from
        disabling that guard, mutating tables, attaching a database, changing
        schema, or ending/restarting the snapshot through SQL. Permit only
        SELECT/READ/safe SQL functions and a read of PRAGMA query_only for
        diagnostics. This is defense in depth, not a sandbox for hostile Python.
        """
        if operation == sqlite3.SQLITE_PRAGMA:
            if name is not None and name.lower() == "query_only" and argument is None:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if operation == sqlite3.SQLITE_FUNCTION:
            if (argument or "").lower() in {
                "load_extension", "readfile", "writefile", "eval",
            }:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if operation in {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_RECURSIVE,
        }:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    @staticmethod
    def _begin_readonly_audit(db: sqlite3.Connection) -> None:
        """Pin a read-only WAL snapshot and install a restrictive SQL authorizer.

        Enforce query_only=ON before BEGIN. Once BEGIN starts, deny SQL that
        could disable query_only, mutate evidence, or close/restart the read
        transaction. Closing the connection releases its snapshot regardless
        of the authorizer and leaves independent writer handles unaffected.
        """
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        db.set_authorizer(TrialStore._audit_sql_authorizer)

    def receipt_records(self) -> list[dict[str, Any]]:
        """Return retained delivery receipts only after validating every row."""
        db = self._connect()
        try:
            # Audit cross-table evidence using a read-only pinned snapshot.
            self._begin_readonly_audit(db)
            self._assert_event_sequence_schema(db)
            rows = db.execute(
                "SELECT receipt_id,trial_id,sender,transport,bridge_run_id,"
                "result,payload_sha256,received_at "
                "FROM trial_receipts ORDER BY receipt_id LIMIT ?",
                (MAX_RECEIPTS + 1,),
            )
            retained: list[dict[str, Any]] = []
            receipt_ids: set[int] = set()
            first_seen_trials: set[str] = set()
            conflict_senders: set[str] = set()
            earlier_duplicates: set[str] = set()
            earlier_blocked_senders: set[str] = set()
            for row in rows:
                if len(retained) >= MAX_RECEIPTS:
                    raise TrialStoreError("stored trial receipt count exceeds capacity")
                self._verify_receipt_row(db, row)
                receipt_id = row["receipt_id"]
                if receipt_id in receipt_ids:
                    raise TrialStoreError("stored duplicate receipt identity")
                receipt_ids.add(receipt_id)
                # A first-seen trial has exactly one initial delivery,
                # accepted (pass/fail) or non_pass_stored (skipped).
                # Duplicate deliveries are separate receipts and are allowed
                # without a retained original because old receipts are
                # pruned. Two still-retained first-seen receipts for one
                # trial are never a valid history, however.
                if row["result"] in {"accepted", "non_pass_stored"}:
                    if row["trial_id"] in first_seen_trials:
                        raise TrialStoreError(
                            "stored duplicate first-seen trial receipt"
                        )
                    # Receipt IDs are delivery sequence numbers. A first-seen
                    # receipt cannot arrive after a previously retained
                    # duplicate for the same trial. If the first receipt was
                    # pruned, only duplicates remain and that is valid.
                    if row["trial_id"] in earlier_duplicates:
                        raise TrialStoreError(
                            "stored duplicate receipt precedes first-seen receipt"
                        )
                    first_seen_trials.add(row["trial_id"])
                elif row["result"] == "duplicate":
                    earlier_duplicates.add(row["trial_id"])
                # The first conflict quarantines its sender. Later deliveries
                # from that sender must be sender_quarantined, not another
                # conflict_quarantined event. If the first receipt was pruned,
                # retained blocked receipts are still valid.
                if row["result"] == "conflict_quarantined":
                    if row["sender"] in conflict_senders:
                        raise TrialStoreError(
                            "stored duplicate first-conflict sender receipt"
                        )
                    if row["sender"] in earlier_blocked_senders:
                        raise TrialStoreError(
                            "stored blocked receipt precedes first-conflict receipt"
                        )
                    conflict_senders.add(row["sender"])
                elif row["result"] == "sender_quarantined":
                    earlier_blocked_senders.add(row["sender"])
                retained.append(dict(row))
            return retained
        except sqlite3.Error as exc:
            raise TrialStoreError("trial receipt read failed") from exc
        finally:
            db.close()

    def pool_snapshot(self) -> dict[str, Any]:
        db = self._connect()
        try:
            self._begin_readonly_audit(db)
            rows = self._verified_trial_rows(db)
            return pool_trials([
                {"trial_id": row["trial_id"], "source": row["source"],
                 "failed": row["outcome"] == "fail"}
                for row in rows if row["outcome"] in {"pass", "fail"}
            ])
        except sqlite3.Error as exc:
            raise TrialStoreError("trial pool read failed") from exc
        finally:
            db.close()

    def non_pass_records(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            self._begin_readonly_audit(db)
            fields = (
                "trial_id", "source", "system", "node", "run_id", "check_id",
                "check_version", "check_config_sha256", "attempt_id", "outcome",
                "first_sender", "first_transport", "bridge_run_id", "received_at",
            )
            rows = self._verified_trial_rows(db)
            non_pass = (row for row in rows if row["outcome"] not in {"pass", "fail"})
            return [
                {field: row[field] for field in fields}
                for row in sorted(non_pass, key=lambda r: (r["received_at"], r["trial_id"]))
            ]
        except sqlite3.Error as exc:
            raise TrialStoreError("non-pass trial read failed") from exc
        finally:
            db.close()

    @staticmethod
    def _verify_quarantine_row(row: sqlite3.Row) -> None:
        """Refuse malformed conflict evidence instead of displaying an unverified digest."""
        try:
            event_id = row["quarantine_id"]
            if type(event_id) is not int or event_id <= 0:
                raise ValueError("quarantine event identifier is invalid")
            raw = row["incoming_json"]
            if not isinstance(raw, bytes) or len(raw) > MAX_ENVELOPE_BYTES:
                raise ValueError("quarantine envelope is not bounded bytes")
            envelope = json.loads(raw.decode("utf-8"))
            if canonical_trial_bytes(envelope) != raw:
                raise ValueError("quarantine envelope is not canonical")
            if row["incoming_sha256"] != hashlib.sha256(raw).hexdigest():
                raise ValueError("quarantine digest does not match incoming bytes")
            if row["trial_id"] != envelope["trial_id"]:
                raise ValueError("quarantine trial identity differs from incoming bytes")
            if not isinstance(row["sender"], str) or not SENDER_RE.fullmatch(row["sender"]):
                raise ValueError("quarantine sender is invalid")
            if row["transport"] not in {"fleet", "bridge"}:
                raise ValueError("quarantine transport is invalid")
            if row["reason"] not in {
                "canonical source/check/attempt identity arrived with a different trial_id",
                "same trial_id arrived with a different canonical envelope",
            }:
                raise ValueError("quarantine reason is unrecognized")
            TrialStore._verify_stored_provenance(
                sender=row["sender"],
                transport=row["transport"],
                bridge_run_id=row["bridge_run_id"],
                received_at=row["received_at"],
            )
        except (TypeError, ValueError, KeyError, UnicodeError, RecursionError,
                OverflowError, sqlite3.Error) as exc:
            raise TrialStoreError("stored trial quarantine integrity check failed") from exc

    def _assert_quarantine_conflict_semantics(
        self, db: sqlite3.Connection, row: sqlite3.Row,
    ) -> None:
        """Verify that a conflict reason follows from the preserved original.

        Same-ID mismatches take precedence over canonical source aliases in
        _ingest_one. An audit reason is trustworthy only if a still-valid
        original record proves that exact classification.
        """
        self._verify_quarantine_row(row)
        incoming = json.loads(row["incoming_json"])
        exact = db.execute(
            f"SELECT {BOUNDED_TRIAL_COLUMNS} FROM trials WHERE trial_id=?", (row["trial_id"],)
        ).fetchone()
        if exact is not None:
            self._verify_stored_trial(exact)
            if (
                row["reason"] != "same trial_id arrived with a different canonical envelope"
                or bytes(exact["canonical_json"]) == row["incoming_json"]
            ):
                raise TrialStoreError("stored trial quarantine conflict reason is inconsistent")
            if row["received_at"] < exact["received_at"]:
                raise TrialStoreError("stored quarantine precedes first-seen evidence")
            return
        alias = db.execute(
            f"SELECT {BOUNDED_TRIAL_COLUMNS} FROM trials WHERE source_identity_key=?",
            (_source_identity_key(incoming),),
        ).fetchone()
        if alias is None:
            raise TrialStoreError("stored trial quarantine conflict lacks original evidence")
        self._verify_stored_trial(alias)
        if (
            row["reason"] != "canonical source/check/attempt identity arrived with a different trial_id"
            or alias["trial_id"] == row["trial_id"]
        ):
            raise TrialStoreError("stored trial quarantine conflict reason is inconsistent")
        if row["received_at"] < alias["received_at"]:
            raise TrialStoreError("stored quarantine precedes first-seen evidence")

    @staticmethod
    def _verify_sender_quarantine_pair(marker: sqlite3.Row | None,
                                       row: sqlite3.Row | dict[str, Any] | None,
                                       *, already_verified: bool = False) -> None:
        """Reject missing, forged or mismatched sender-quarantine markers."""
        if (marker is None) != (row is None):
            raise TrialStoreError("stored sender quarantine state is inconsistent")
        if row is None:
            return
        if not already_verified:
            TrialStore._verify_quarantine_row(row)
        if (
            not isinstance(marker["sender"], str)
            or not SENDER_RE.fullmatch(marker["sender"])
            or marker["sender"] != row["sender"]
            or marker["first_trial_id"] != row["trial_id"]
            or marker["reason"] != row["reason"]
            or marker["quarantined_at"] != row["received_at"]
        ):
            raise TrialStoreError("stored sender quarantine state is inconsistent")

    def _assert_sender_quarantine_state(self, db: sqlite3.Connection, sender: str) -> bool:
        """Validate this sender's marker before blocking or admitting a delivery.

        Limit result rows to detect duplicate conflict evidence without scanning
        every quarantined sender on the hot ingest path.
        """
        marker = db.execute(
            "SELECT sender,first_trial_id,reason,quarantined_at "
            "FROM trial_quarantined_senders WHERE sender=?", (sender,)
        ).fetchone()
        conflicts = db.execute(
            f"SELECT {BOUNDED_QUARANTINE_COLUMNS} FROM trial_quarantine "
            "WHERE sender=? ORDER BY quarantine_id LIMIT 2",
            (sender,),
        ).fetchall()
        if len(conflicts) > 1:
            raise TrialStoreError("stored sender quarantine state is inconsistent")
        self._verify_sender_quarantine_pair(marker, conflicts[0] if conflicts else None)
        if conflicts:
            self._assert_quarantine_conflict_semantics(db, conflicts[0])
        return marker is not None

    def quarantine_records(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            # Pin a read-only snapshot of conflict, trial and sender state.
            self._begin_readonly_audit(db)
            self._assert_event_sequence_schema(db)
            rows = db.execute(
                f"SELECT {BOUNDED_QUARANTINE_COLUMNS} FROM trial_quarantine "
                "ORDER BY quarantine_id LIMIT ?",
                (MAX_QUARANTINE_RECORDS + 1,),
            )
            public = (
                "trial_id", "sender", "transport", "bridge_run_id",
                "reason", "incoming_sha256", "received_at",
            )
            minimal = ("trial_id", "sender", "reason", "received_at")
            conflict_by_sender: dict[str, dict[str, Any]] = {}
            seen_event_ids: set[int] = set()
            audited: list[dict[str, Any]] = []
            for row in rows:
                if len(audited) >= MAX_QUARANTINE_RECORDS:
                    raise TrialStoreError("stored quarantine count exceeds capacity")
                self._assert_quarantine_conflict_semantics(db, row)
                if row["quarantine_id"] in seen_event_ids:
                    raise TrialStoreError("stored duplicate quarantine event identity")
                seen_event_ids.add(row["quarantine_id"])
                if row["sender"] in conflict_by_sender:
                    raise TrialStoreError("stored sender quarantine state is inconsistent")
                # Original BLOB is validated and discarded before the next row.
                conflict_by_sender[row["sender"]] = {k: row[k] for k in minimal}
                audited.append({k: row[k] for k in public})
            markers = db.execute(
                "SELECT sender,first_trial_id,reason,quarantined_at "
                "FROM trial_quarantined_senders ORDER BY sender LIMIT ?",
                (MAX_QUARANTINED_SENDERS + 1,),
            )
            checked = 0
            for marker in markers:
                if checked >= MAX_QUARANTINED_SENDERS:
                    raise TrialStoreError("stored quarantined sender count exceeds capacity")
                checked += 1
                self._verify_sender_quarantine_pair(
                    marker, conflict_by_sender.get(marker["sender"]),
                    already_verified=True,
                )
            if checked != len(conflict_by_sender):
                raise TrialStoreError("stored sender quarantine state is inconsistent")
            return audited
        except sqlite3.Error as exc:
            raise TrialStoreError("trial quarantine read failed") from exc
        finally:
            db.close()
