"""Round 215: attested AUTOINCREMENT semantics of event sequence numbers.

PRAGMA table_info verifies INTEGER PRIMARY KEY but cannot distinguish it
from INTEGER PRIMARY KEY AUTOINCREMENT. SQLite is allowed to reuse the
greatest deleted rowid without AUTOINCREMENT, undermining the continuity of
retained receipt and quarantine event sequences.
"""
from __future__ import annotations

import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import DB_SCHEMA_VERSION, TrialStore, TrialStoreError


EVENT_TABLES = (
    ("trial_receipts", "receipt_id"),
    ("trial_quarantine", "quarantine_id"),
)


def trial(outcome="pass", attempt="215"):
    return make_trial(
        system="cyclekernel", node="node-215", run_id="run-215",
        check_id="event-sequence", check_version="v2",
        attempt_id=attempt, outcome=outcome,
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    accepted = trial()
    assert store.ingest_many([accepted], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.ingest_many([trial("fail")], sender="peer-b",
                             transport="fleet")["quarantined"] == 1
    assert len(store.receipt_records()) == 2
    assert len(store.quarantine_records()) == 1
    return store, path, accepted


def tampered_ddl(sql: str, method: str) -> str:
    original = "INTEGER PRIMARY KEY AUTOINCREMENT"
    assert original in sql
    if method == "no-autoincrement":
        return sql.replace(original, "INTEGER PRIMARY KEY", 1)
    if method == "misleading-comment":
        return sql.replace(
            original,
            "INTEGER PRIMARY KEY /* receipt_id INTEGER PRIMARY KEY AUTOINCREMENT */",
            1,
        )
    if method == "weaken-required-column":
        return sql.replace("trial_id TEXT NOT NULL", "trial_id TEXT", 1)
    if method == "different-collation":
        return sql.replace(
            "trial_id TEXT NOT NULL", "trial_id TEXT NOT NULL COLLATE NOCASE", 1,
        )
    raise AssertionError(method)


def rebuild(store, table, method):
    assert table in {name for name, _ in EVENT_TABLES}
    db = store._connect()
    temporary = f"previous_{table}"
    try:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()["sql"]
        mutated = tampered_ddl(sql, method)
        assert mutated != sql
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"ALTER TABLE {table} RENAME TO {temporary}")
        db.execute(mutated)
        db.execute(f"INSERT INTO {table} SELECT * FROM {temporary}")
        db.execute(f"DROP TABLE {temporary}")
        db.commit()
    finally:
        db.close()


def totals(store):
    db = store._connect()
    try:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("trials", "trial_receipts", "trial_quarantine",
                          "trial_quarantined_senders")
        )
    finally:
        db.close()


@pytest.mark.parametrize("table,sequence_key", EVENT_TABLES)
@pytest.mark.parametrize(
    "method",
    ["no-autoincrement", "misleading-comment",
     "weaken-required-column", "different-collation"],
)
def test_changed_event_table_schema_fails_closed_on_ingest_and_audits(
    tmp_path, table, sequence_key, method,
):
    store, path, first = seeded(tmp_path)
    before = totals(store)
    rebuild(store, table, method)
    db = store._connect()
    try:
        # A PK-only check cannot distinguish dropped AUTOINCREMENT or an
        # otherwise unexpected event table definition.
        store._assert_primary_key_schema(db)
        with pytest.raises(TrialStoreError, match="event sequence schema integrity"):
            store._assert_event_sequence_schema(db)
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="event sequence schema integrity"):
        store.ingest_many(
            [trial(attempt="new")], sender="peer-c", transport="fleet"
        )
    with pytest.raises(TrialStoreError, match="event sequence schema integrity"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="event sequence schema integrity"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="event sequence schema integrity"):
        TrialStore(path)
    assert totals(store) == before


@pytest.mark.parametrize("table,sequence_key", EVENT_TABLES)
def test_without_autoincrement_sqlite_can_recycle_deleted_event_number(
    tmp_path, table, sequence_key,
):
    store, _, _ = seeded(tmp_path)
    rebuild(store, table, "no-autoincrement")
    db = store._connect()
    try:
        max_id = db.execute(
            f"SELECT MAX({sequence_key}) FROM {table}"
        ).fetchone()[0]
        row = db.execute(
            f"SELECT * FROM {table} WHERE {sequence_key}=?", (max_id,)
        ).fetchone()
        names = list(row.keys())
        values = [None if name == sequence_key else row[name] for name in names]
        db.execute(f"DELETE FROM {table} WHERE {sequence_key}=?", (max_id,))
        placeholders = ",".join("?" for _ in values)
        db.execute(
            f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})",
            values,
        )
        db.commit()
        assert db.execute(
            f"SELECT MAX({sequence_key}) FROM {table}"
        ).fetchone()[0] == max_id
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="event sequence schema integrity"):
        store.receipt_records()


def test_valid_autoincrement_sequence_survives_last_row_deletion(tmp_path):
    store, path, original = seeded(tmp_path)
    db = store._connect()
    try:
        store._assert_event_sequence_schema(db)
        maximum = db.execute(
            "SELECT MAX(receipt_id) FROM trial_receipts"
        ).fetchone()[0]
        row = db.execute(
            "SELECT trial_id,sender,transport,bridge_run_id,result,"
            "payload_sha256,received_at FROM trial_receipts "
            "WHERE receipt_id=?", (maximum,),
        ).fetchone()
        db.execute("DELETE FROM trial_receipts WHERE receipt_id=?", (maximum,))
        db.execute(
            "INSERT INTO trial_receipts(trial_id,sender,transport,"
            "bridge_run_id,result,payload_sha256,received_at)"
            " VALUES(?,?,?,?,?,?,?)", tuple(row),
        )
        db.commit()
        latest = db.execute(
            "SELECT MAX(receipt_id) FROM trial_receipts"
        ).fetchone()[0]
        assert latest > maximum
    finally:
        db.close()

    assert len(store.receipt_records()) == 2
    assert len(TrialStore(path).quarantine_records()) == 1


def test_healthy_triggers_and_indexes_remain_compatible_with_ddl_attestation(
    tmp_path,
):
    store, path, first = seeded(tmp_path)
    db = store._connect()
    try:
        store._assert_primary_key_schema(db)
        store._assert_source_identity_unique_index(db)
        store._assert_source_identity_triggers(db)
        store._assert_event_sequence_schema(db)
    finally:
        db.close()
    assert store.ingest_many([first], sender="peer-c",
                             transport="fleet")["duplicates"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 1
    assert len(store.quarantine_records()) == 1

@pytest.mark.parametrize("table,sequence_key", EVENT_TABLES)
@pytest.mark.parametrize(
    "corruption",
    [
        "delete-sequence-entry", "zero-sequence", "negative-sequence",
        "string-sequence", "null-sequence", "duplicate-sequence",
        "lag-retained-maximum",
    ],
)
def test_sqlite_sequence_drift_rejected_even_with_correct_table_ddl(
    tmp_path, table, sequence_key, corruption,
):
    store, path, _ = seeded(tmp_path)
    # Populate at least two events in both tables. A sequence of 1 then
    # becomes provably smaller than the largest retained event identifier.
    assert store.ingest_many(
        [trial("fail")], sender="peer-c", transport="fleet"
    )["quarantined"] == 1
    before = totals(store)
    db = store._connect()
    try:
        store._assert_primary_key_schema(db)
        store._assert_event_sequence_schema(db)
        max_id = db.execute(
            f"SELECT MAX({sequence_key}) FROM {table}"
        ).fetchone()[0]
        assert max_id >= 2

        if corruption == "delete-sequence-entry":
            db.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        elif corruption == "duplicate-sequence":
            db.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)",
                (table, max_id + 1),
            )
        else:
            altered = {
                "zero-sequence": 0,
                "negative-sequence": -1,
                "string-sequence": "not-an-integer",
                "null-sequence": None,
                "lag-retained-maximum": 1,
            }[corruption]
            db.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name=?",
                (altered, table),
            )
        db.commit()
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="event sequence state integrity"):
        store.receipt_records()
    with pytest.raises(TrialStoreError, match="event sequence state integrity"):
        store.quarantine_records()
    with pytest.raises(TrialStoreError, match="event sequence state integrity"):
        store.ingest_many(
            [trial(attempt="new-attempt")], sender="peer-d", transport="fleet"
        )
    with pytest.raises(TrialStoreError, match="event sequence state integrity"):
        TrialStore(path)
    assert totals(store) == before


def test_unused_event_tables_have_no_sequence_rows_and_initialize_normally(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    db = store._connect()
    try:
        rows = list(db.execute(
            "SELECT name,seq FROM sqlite_sequence "
            "WHERE name IN ('trial_receipts','trial_quarantine')"
        ))
        assert rows == []
        store._assert_event_sequence_schema(db)
    finally:
        db.close()
    assert store.receipt_records() == []
    assert store.quarantine_records() == []
    assert TrialStore(path).pool_snapshot()["runs"] == 0


def _make_event_tables_real_legacy(store, tables):
    """Rebuild selected event tables without bridge_run_id, as a v1 DB did."""
    from avrone.trial_receiver import EXPECTED_EVENT_SEQUENCE_TABLE_SQL
    db = store._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        for table in tables:
            old_table = "former_" + table
            columns = [
                item["name"] for item in db.execute(
                    f"PRAGMA table_info({table})"
                ) if item["name"] != "bridge_run_id"
            ]
            assert len(columns) == (7 if table == "trial_receipts" else 8)
            db.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
            legacy_ddl = EXPECTED_EVENT_SEQUENCE_TABLE_SQL[table].replace(
                "bridge_run_id TEXT,", ""
            )
            db.execute(legacy_ddl)
            fields = ",".join(columns)
            db.execute(
                f"INSERT INTO {table} ({fields}) "
                f"SELECT {fields} FROM {old_table}"
            )
            db.execute(f"DROP TABLE {old_table}")
        db.execute("PRAGMA user_version=1")
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "legacy_tables",
    [
        ("trial_receipts",),
        ("trial_quarantine",),
        ("trial_receipts", "trial_quarantine"),
    ],
    ids=["receipts-only", "quarantine-only", "both-event-tables"],
)
def test_trusted_legacy_bridge_column_append_is_attested_after_migration(
    tmp_path, legacy_tables,
):
    """Real ALTER ADD COLUMN appends bridge_run_id at the end of the DDL."""
    store, path, original = seeded(tmp_path)
    before_receipts = store.receipt_records()
    before_quarantines = store.quarantine_records()
    _make_event_tables_real_legacy(store, legacy_tables)

    db = store._connect()
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        for table in legacy_tables:
            names = [x["name"] for x in db.execute(
                f"PRAGMA table_info({table})"
            )]
            assert "bridge_run_id" not in names
    finally:
        db.close()

    migrated = TrialStore(path)
    db = migrated._connect()
    try:
        migrated._assert_event_sequence_schema(db)
        for table in legacy_tables:
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (table,)
            ).fetchone()[0].upper()
            assert ddl.index("BRIDGE_RUN_ID") > ddl.index("RECEIVED_AT")
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
    finally:
        db.close()

    assert migrated.receipt_records() == before_receipts
    assert migrated.quarantine_records() == before_quarantines
    assert TrialStore(path).pool_snapshot()["runs"] == 1
    assert TrialStore(path).receipt_records() == before_receipts


@pytest.mark.parametrize("table", ["trial_receipts", "trial_quarantine"])
def test_migrated_legacy_event_schema_still_rejects_constraint_downgrades(
    tmp_path, table,
):
    """Permit exactly the legacy order; reject weakening even one constraint."""
    store, path, _ = seeded(tmp_path)
    _make_event_tables_real_legacy(store, (table,))
    migrated = TrialStore(path)
    db = migrated._connect()
    try:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (table,)
        ).fetchone()[0]
        assert "bridge_run_id" in ddl
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"ALTER TABLE {table} RENAME TO prior_event")
        downgraded = ddl.replace("trial_id TEXT NOT NULL", "trial_id TEXT")
        assert downgraded != ddl
        db.execute(downgraded)
        columns = [x["name"] for x in db.execute("PRAGMA table_info(prior_event)")]
        cols = ",".join(columns)
        db.execute(
            f"INSERT INTO {table} ({cols}) SELECT {cols} FROM prior_event"
        )
        db.execute("DROP TABLE prior_event")
        db.commit()
    finally:
        db.close()

    with pytest.raises(TrialStoreError, match="event sequence schema integrity"):
        TrialStore(path)
