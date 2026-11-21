"""Round 226: WAL reader snapshots, checkpoint modes and restart privacy edges."""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore


def make_payload(attempt: str, outcome: str = "pass") -> dict:
    return make_trial(
        system="cyclekernel",
        node="node-round226",
        run_id="run-round226",
        check_id="wal-recovery",
        check_version="v2",
        attempt_id=attempt,
        outcome=outcome,
    )


def raw_integrity(path: Path) -> tuple[str, int]:
    db = sqlite3.connect(path)
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        count = db.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
        return integrity, count
    finally:
        db.close()


def test_reader_snapshot_survives_blocked_truncate_checkpoint_and_restart(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    assert store.ingest_many(
        [make_payload("snapshot-before")], sender="peer-a", transport="fleet"
    )["accepted"] == 1

    reader = store._connect()
    checkpoint = None
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1

        assert store.ingest_many(
            [make_payload("snapshot-after")], sender="peer-a", transport="fleet"
        )["accepted"] == 1

        checkpoint = store._connect()
        busy, log_frames, checkpointed_frames = checkpoint.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        # The reader's established snapshot prevents TRUNCATE from completing.
        assert busy == 1
        assert log_frames >= checkpointed_frames
        assert reader.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
        assert raw_integrity(path) == ("ok", 2)
    finally:
        reader.rollback()
        reader.close()
        if checkpoint is not None:
            checkpoint.close()

    final_checkpoint = store._connect()
    try:
        busy, _, _ = final_checkpoint.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        assert busy == 0
        assert final_checkpoint.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        final_checkpoint.close()

    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 2
    assert raw_integrity(path) == ("ok", 2)


@pytest.mark.parametrize("mode", ["PASSIVE", "FULL", "RESTART", "TRUNCATE"])
def test_each_wal_checkpoint_mode_preserves_committed_evidence(tmp_path, mode):
    path = tmp_path / f"private-{mode.lower()}" / "trials.sqlite3"
    store = TrialStore(path)
    for index in range(4):
        result = store.ingest_many(
            [make_payload(f"checkpoint-{mode}-{index}")],
            sender="peer-checkpoint",
            transport="fleet",
        )
        assert result["accepted"] == 1

    db = store._connect()
    try:
        busy, log_frames, checkpointed_frames = db.execute(
            f"PRAGMA wal_checkpoint({mode})"
        ).fetchone()
        assert busy == 0
        assert log_frames >= checkpointed_frames
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()

    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 4
    assert raw_integrity(path) == ("ok", 4)


@pytest.mark.parametrize("mask", [0o022, 0o077, 0o177, 0o777])
def test_repeated_initialization_under_hostile_umasks_keeps_database_private(
    tmp_path, mask
):
    path = tmp_path / f"private-mask-{mask:o}" / "trials.sqlite3"
    previous = os.umask(mask)
    try:
        store = TrialStore(path)
        assert store.ingest_many(
            [make_payload(f"umask-{mask:o}")],
            sender="peer-umask",
            transport="fleet",
        )["accepted"] == 1

        db = store._connect()
        try:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    info = sidecar.lstat()
                    assert stat.S_ISREG(info.st_mode)
                    assert info.st_nlink == 1
                    assert stat.S_IMODE(info.st_mode) == 0o600
        finally:
            db.close()

        # A second initializer must preserve the already-established evidence
        # and must not recreate a permissive database or sidecar.
        reopened = TrialStore(path)
        assert reopened.pool_snapshot()["runs"] == 1
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(previous)
