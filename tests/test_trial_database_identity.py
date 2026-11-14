"""Live SQLite pathname identity and pre-open/post-open race regressions.

A SQLite connection is path-based. Preflight lstat is not enough: an attacker or
accidental maintainer can swap a private DB between validation and open, or
delete it between operations. Fail closed on single-replacement races.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt_id="r188"):
    return make_trial(
        system="cyclekernel", node="node-a", run_id="run188",
        check_id="evidence", check_version="v2",
        attempt_id=attempt_id, outcome="pass",
    )


def initialized(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "trials.sqlite3"
    store = TrialStore(path)
    assert store.ingest_many([trial()], sender="peer-a", transport="fleet")["accepted"] == 1
    return store, path


def test_missing_database_is_not_silently_recreated_on_later_read(tmp_path):
    store, path = initialized(tmp_path)
    path.unlink()

    with pytest.raises(TrialStoreError, match="identity changed since initialization"):
        store.pool_snapshot()

    assert not path.exists()


def test_missing_database_is_not_silently_recreated_on_later_write(tmp_path):
    store, path = initialized(tmp_path)
    path.unlink()

    with pytest.raises(TrialStoreError, match="identity changed since initialization"):
        store.ingest_many([trial("later")], sender="peer-a", transport="fleet")

    assert not path.exists()


def test_database_replaced_by_different_regular_sqlite_is_refused(tmp_path):
    store, path = initialized(tmp_path)
    substitute = path.parent / "substitute.sqlite3"
    TrialStore(substitute)
    old = path.parent / "original.sqlite3"
    path.rename(old)
    substitute.rename(path)

    with pytest.raises(TrialStoreError, match="identity changed since initialization"):
        store.pool_snapshot()

    assert TrialStore(old).pool_snapshot()["runs"] == 1
    assert TrialStore(path).pool_snapshot()["runs"] == 0


def test_parent_swapped_inside_sqlite_connect_is_rejected_and_connection_closed(
    tmp_path, monkeypatch,
):
    store, path = initialized(tmp_path)
    from avrone import trial_receiver

    real_connect = sqlite3.connect
    private = path.parent
    displaced = tmp_path / "moved-private"
    seen = []

    def swap_then_open(*args, **kwargs):
        private.rename(displaced)
        private.mkdir(mode=0o700)
        db = real_connect(*args, **kwargs)
        seen.append(db)
        return db

    monkeypatch.setattr(trial_receiver.sqlite3, "connect", swap_then_open)

    with pytest.raises(TrialStoreError, match="identity changed"):
        store.pool_snapshot()

    assert len(seen) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        seen[0].execute("SELECT 1")
    assert TrialStoreError is not None
    assert not (displaced / "trials.sqlite3").is_symlink()
    # Parent substitution must not return evidence from a newly created DB.
    assert (displaced / "trials.sqlite3").exists()


def test_database_swapped_inside_sqlite_connect_is_rejected_and_connection_closed(
    tmp_path, monkeypatch,
):
    store, path = initialized(tmp_path)
    replacement = path.parent / "replacement.sqlite3"
    TrialStore(replacement)
    original = path.parent / "original.sqlite3"
    from avrone import trial_receiver
    real_connect = sqlite3.connect
    seen = []

    def swap_then_open(*args, **kwargs):
        path.rename(original)
        replacement.rename(path)
        db = real_connect(*args, **kwargs)
        seen.append(db)
        return db

    monkeypatch.setattr(trial_receiver.sqlite3, "connect", swap_then_open)

    with pytest.raises(TrialStoreError, match="identity changed during connection"):
        store.pool_snapshot()

    assert len(seen) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        seen[0].execute("SELECT 1")
    assert original.is_file()
    assert path.is_file()


def test_symlink_planted_inside_sqlite_connect_is_rejected_before_query(
    tmp_path, monkeypatch,
):
    store, path = initialized(tmp_path)
    target = path.parent / "target.sqlite3"
    TrialStore(target)
    target_bytes = target.read_bytes()
    from avrone import trial_receiver
    real_connect = sqlite3.connect
    seen = []

    def plant_then_open(*args, **kwargs):
        path.unlink()
        path.symlink_to(target)
        db = real_connect(*args, **kwargs)
        seen.append(db)
        return db

    monkeypatch.setattr(trial_receiver.sqlite3, "connect", plant_then_open)

    with pytest.raises(TrialStoreError, match="symlink"):
        store.pool_snapshot()

    assert target.read_bytes() == target_bytes
    assert len(seen) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        seen[0].execute("SELECT 1")


def test_normal_reopen_pins_stable_inode_and_retains_idempotency(tmp_path):
    store, path = initialized(tmp_path)
    initial_inode = path.stat().st_ino
    for _ in range(3):
        replay = store.ingest_many([trial()], sender="peer-a", transport="fleet")
        assert replay["duplicates"] == 1
        assert path.stat().st_ino == initial_inode
        assert store.pool_snapshot()["runs"] == 1
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 1
    assert path.stat().st_ino == initial_inode
