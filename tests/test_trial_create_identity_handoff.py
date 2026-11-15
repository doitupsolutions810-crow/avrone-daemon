"""Round 214: the inode verified at exclusive first-create must reach SQLite.

Even with O_EXCL/O_NOFOLLOW, a filename could be replaced between verifying
the newly created inode and sqlite3.connect's own preflight. The receiver
must not replace that identity with a fresh lstat and silently trust a new
regular file. Test both winning and losing concurrent first-creator branches.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from avrone import trial_receiver
from avrone.trial_receiver import TrialStore, TrialStoreError


def location(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    return parent / "trials.sqlite3"


def identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


def make_replacement(path):
    replacement = path.with_name("foreign.sqlite3")
    replacement.write_bytes(b"not-original-sqlite")
    replacement.chmod(0o600)
    return replacement


def test_created_inode_swap_before_sqlite_preflight_is_refused(tmp_path, monkeypatch):
    path = location(tmp_path)
    replacement = make_replacement(path)
    displaced = path.with_name("original.sqlite3")
    original_helper = TrialStore._create_private_database_once
    original_connect = sqlite3.connect
    opened_sqlite = []
    created_ids = []

    def swap_after_create(self):
        verified = original_helper(self)
        assert verified is not None
        created_ids.append(verified)
        path.rename(displaced)
        replacement.rename(path)
        assert identity(path) != verified
        return verified

    def monitor_sqlite(*args, **kwargs):
        opened_sqlite.append(True)
        return original_connect(*args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_create_private_database_once", swap_after_create)
        ctx.setattr(trial_receiver.sqlite3, "connect", monitor_sqlite)
        with pytest.raises(TrialStoreError, match="identity changed before SQLite open"):
            TrialStore(path)

    assert created_ids and opened_sqlite == []
    assert displaced.is_file() and identity(displaced) == created_ids[0]
    assert stat.S_IMODE(displaced.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.unlink()
    displaced.rename(path)
    assert TrialStore(path).pool_snapshot()["runs"] == 0


def test_competing_creator_inode_swap_before_sqlite_is_refused(tmp_path, monkeypatch):
    path = location(tmp_path)
    replacement = make_replacement(path)
    displaced = path.with_name("winner.sqlite3")
    original_open = os.open
    original_helper = TrialStore._create_private_database_once
    actual_connect = sqlite3.connect
    winner = []
    sqlite_opened = []

    def competing_creator(file, flags, *args, **kwargs):
        if Path(file) == path and flags & os.O_EXCL and not winner:
            fd = original_open(file, flags, *args, **kwargs)
            winner.append(os.fstat(fd).st_ino)
            os.close(fd)
        return original_open(file, flags, *args, **kwargs)

    def swap_after_losing_exclusive_create(self):
        verified = original_helper(self)
        assert verified is not None
        assert verified[1] == winner[0]
        path.rename(displaced)
        replacement.rename(path)
        return verified

    def monitor_sqlite(*args, **kwargs):
        sqlite_opened.append(True)
        return actual_connect(*args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", competing_creator)
        ctx.setattr(TrialStore, "_create_private_database_once",
                    swap_after_losing_exclusive_create)
        ctx.setattr(trial_receiver.sqlite3, "connect", monitor_sqlite)
        with pytest.raises(TrialStoreError, match="identity changed before SQLite open"):
            TrialStore(path)

    assert winner and sqlite_opened == []
    assert displaced.stat().st_ino == winner[0]
    path.unlink()
    displaced.rename(path)
    assert TrialStore(path).pool_snapshot()["runs"] == 0


def test_created_database_deleted_before_sqlite_open_is_not_silently_recreated(
    tmp_path, monkeypatch,
):
    path = location(tmp_path)
    original_helper = TrialStore._create_private_database_once
    original_connect = sqlite3.connect
    opened_sqlite = []
    created = []

    def delete_after_create(self):
        verified = original_helper(self)
        assert verified is not None
        created.append(verified)
        path.unlink()
        return verified

    def monitor_sqlite(*args, **kwargs):
        opened_sqlite.append(True)
        return original_connect(*args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_create_private_database_once", delete_after_create)
        ctx.setattr(trial_receiver.sqlite3, "connect", monitor_sqlite)
        with pytest.raises(TrialStoreError, match="identity changed before SQLite open"):
            TrialStore(path)

    assert len(created) == 1 and not path.exists()
    assert opened_sqlite == []


def test_successful_new_creation_hands_exact_inode_to_connection(tmp_path, monkeypatch):
    path = location(tmp_path)
    original_helper = TrialStore._create_private_database_once
    observed = []

    def inspect_handoff(self):
        verified = original_helper(self)
        if verified is not None:
            observed.append(verified)
        return verified

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_create_private_database_once", inspect_handoff)
        store = TrialStore(path)
        assert store.pool_snapshot()["runs"] == 0

    assert observed == [identity(path)]
    assert observed[0] == store._database_identity
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_trusted_preexisting_legacy_inode_does_not_trigger_false_new_creation(
    tmp_path, monkeypatch,
):
    path = location(tmp_path)
    original = TrialStore(path)
    assert original.pool_snapshot()["runs"] == 0
    before = identity(path)
    path.chmod(0o644)
    original_helper = TrialStore._create_private_database_once
    handoffs = []

    def inspect_handoff(self):
        found = original_helper(self)
        handoffs.append(found)
        return found

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_create_private_database_once", inspect_handoff)
        reopened = TrialStore(path)
        assert reopened.pool_snapshot()["runs"] == 0

    assert handoffs and all(found is None for found in handoffs)
    assert identity(path) == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mask", [0o022, 0o002, 0o077])
def test_first_inode_handoff_stays_stable_across_process_umasks(
    tmp_path, monkeypatch, mask,
):
    path = location(tmp_path)
    original_helper = TrialStore._create_private_database_once
    observed = []

    def observe(self):
        result = original_helper(self)
        if result is not None:
            observed.append(result)
        return result

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_create_private_database_once", observe)
        previous = os.umask(mask)
        try:
            store = TrialStore(path)
        finally:
            os.umask(previous)

    assert observed == [store._database_identity]
    assert identity(path) == observed[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.pool_snapshot()["runs"] == 0
