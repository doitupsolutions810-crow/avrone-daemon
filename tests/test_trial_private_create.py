"""Round 213: create the SQLite inode privately before sqlite3.connect.

Private-before-WAL in R205 and FD-pinned fchmod in R212 narrow the exposure,
but sqlite3.connect may first create an absent database at 0644 under a 022
umask. The absent-file path must instead use O_CREAT|O_EXCL|O_NOFOLLOW at
0600, reconcile competing winners safely, and never touch a foreign file.
"""
from __future__ import annotations

import errno
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from avrone import trial_receiver
from avrone.trial_receiver import TrialStore, TrialStoreError


def private_path(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory / "trials.sqlite3"


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("mask", [0o022, 0o002, 0o000, 0o077])
def test_first_sqlite_connect_observes_exclusively_created_private_inode(
    tmp_path, monkeypatch, mask,
):
    path = private_path(tmp_path)
    original_connect = sqlite3.connect
    inspected = []

    def verifying_connect(database, *args, **kwargs):
        if str(database) == str(path):
            assert path.is_file()
            assert mode(path) == 0o600
            assert path.stat().st_nlink == 1
            inspected.append((path.stat().st_dev, path.stat().st_ino))
        return original_connect(database, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.sqlite3, "connect", verifying_connect)
        previous = os.umask(mask)
        try:
            store = TrialStore(path)
        finally:
            os.umask(previous)

    assert len(inspected) == 1
    assert mode(path) == 0o600
    assert store._database_identity == inspected[0]
    assert store.pool_snapshot()["runs"] == 0


def test_atomic_creation_uses_exclusive_nofollow_and_private_mode(
    tmp_path, monkeypatch,
):
    path = private_path(tmp_path)
    actual_open = os.open
    captured = []

    def inspected_open(file, flags, *args, **kwargs):
        if Path(file) == path:
            captured.append((flags, args))
        return actual_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", inspected_open)
        store = TrialStore(path)

    assert len(captured) >= 2
    create_flags, create_args = captured[0]
    assert create_flags & os.O_EXCL
    assert create_flags & os.O_CREAT
    assert create_flags & os.O_NOFOLLOW
    assert create_args[0] == 0o600
    assert all((flags & os.O_NOFOLLOW) for flags, _ in captured)
    assert store.pool_snapshot()["runs"] == 0


def test_competing_exclusive_creator_revalidates_winning_owned_inode(
    tmp_path, monkeypatch,
):
    path = private_path(tmp_path)
    actual_open = os.open
    winner = []

    def raced_open(file, flags, *args, **kwargs):
        if Path(file) == path and flags & os.O_EXCL and not winner:
            fd = actual_open(file, flags, *args, **kwargs)
            winner.append(os.fstat(fd).st_ino)
            os.close(fd)
            # Now the receiver loses O_EXCL to the already-created file.
        return actual_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", raced_open)
        store = TrialStore(path)

    assert winner and path.stat().st_ino == winner[0]
    assert mode(path) == 0o600
    assert store.pool_snapshot()["runs"] == 0
    assert TrialStore(path).pool_snapshot()["runs"] == 0


@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_competing_planted_foreign_file_rejected_without_chmod_or_overwrite(
    tmp_path, monkeypatch, plant,
):
    path = private_path(tmp_path)
    external = tmp_path / "foreign"
    external.write_text("do-not-mutate")
    external.chmod(0o644)
    actual_open = os.open
    planted = []

    def raced_open(file, flags, *args, **kwargs):
        if Path(file) == path and flags & os.O_EXCL and not planted:
            if plant == "symlink":
                path.symlink_to(external)
            else:
                os.link(external, path)
            planted.append(True)
        return actual_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", raced_open)
        with pytest.raises(TrialStoreError, match="symlink|hard link"):
            TrialStore(path)

    assert planted
    assert mode(external) == 0o644
    assert external.read_text() == "do-not-mutate"
    assert path.is_symlink() if plant == "symlink" else path.stat().st_nlink == 2


def test_creation_inode_replaced_after_open_is_rejected_and_descriptor_closed(
    tmp_path, monkeypatch,
):
    path = private_path(tmp_path)
    external = tmp_path / "outside"
    external.write_text("do-not-modify")
    external.chmod(0o644)
    displaced = path.with_name("original.sqlite3")
    actual_open = os.open
    opened = []
    swapped = []

    def raced_open(file, flags, *args, **kwargs):
        fd = actual_open(file, flags, *args, **kwargs)
        if Path(file) == path and flags & os.O_EXCL and not swapped:
            opened.append(fd)
            path.rename(displaced)
            path.symlink_to(external)
            swapped.append(True)
        return fd

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", raced_open)
        with pytest.raises(TrialStoreError, match="symlink"):
            TrialStore(path)

    assert swapped
    assert displaced.is_file() and mode(displaced) == 0o600
    assert mode(external) == 0o644
    with pytest.raises(OSError) as exc:
        os.fstat(opened[0])
    assert exc.value.errno == errno.EBADF


def test_exclusive_create_permission_error_fails_before_sqlite_connect(
    tmp_path, monkeypatch,
):
    path = private_path(tmp_path)
    actual_open = os.open
    opened_sqlite = []

    def denied_open(file, flags, *args, **kwargs):
        if Path(file) == path and flags & os.O_EXCL:
            raise PermissionError("no access")
        return actual_open(file, flags, *args, **kwargs)

    actual_connect = sqlite3.connect

    def inspected_sqlite(*args, **kwargs):
        opened_sqlite.append(True)
        return actual_connect(*args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", denied_open)
        ctx.setattr(trial_receiver.sqlite3, "connect", inspected_sqlite)
        with pytest.raises(TrialStoreError, match="cannot be created securely"):
            TrialStore(path)

    assert opened_sqlite == []
    assert not path.exists()


def test_without_nofollow_secure_creation_fails_closed(tmp_path, monkeypatch):
    path = private_path(tmp_path)
    with monkeypatch.context() as ctx:
        ctx.delattr(trial_receiver.os, "O_NOFOLLOW", raising=False)
        with pytest.raises(TrialStoreError, match="exclusive database creation is unavailable"):
            TrialStore(path)
    assert not path.exists()


def test_running_receiver_never_recreates_deleted_evidence_database(
    tmp_path, monkeypatch,
):
    path = private_path(tmp_path)
    store = TrialStore(path)
    path.unlink()
    actual_open = os.open
    attempted_create = []

    def spy_open(file, flags, *args, **kwargs):
        if Path(file) == path and flags & os.O_CREAT:
            attempted_create.append(True)
        return actual_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", spy_open)
        with pytest.raises(TrialStoreError, match="identity changed since initialization"):
            store.pool_snapshot()

    assert attempted_create == []
    assert not path.exists()


def test_owned_legacy_database_remains_migratable_and_is_private_before_wal(
    tmp_path, monkeypatch,
):
    path = private_path(tmp_path)
    first = TrialStore(path)
    assert first.pool_snapshot()["runs"] == 0
    path.chmod(0o644)
    expected_inode = path.stat().st_ino
    original_enable = TrialStore._enable_wal
    verified = []

    def inspect_transition(db):
        verified.append(mode(path))
        assert mode(path) == 0o600
        return original_enable(db)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_enable_wal", staticmethod(inspect_transition))
        reopened = TrialStore(path)

    assert expected_inode == path.stat().st_ino
    assert verified == [0o600]
    assert reopened.pool_snapshot()["runs"] == 0

@pytest.mark.parametrize(
    "raced_mode", [0o644, 0o666, 0o400, 0o700],
    ids=["world-readable", "group-world-writable", "read-only", "executable"],
)
def test_competing_new_database_with_unsafe_mode_is_refused_before_sqlite_open(
    tmp_path, monkeypatch, raced_mode,
):
    path = private_path(tmp_path)
    actual_open = os.open
    actual_connect = sqlite3.connect
    created = []
    sqlite_calls = []

    def planted_winner(file, flags, *args, **kwargs):
        if Path(file) == path and flags & os.O_EXCL and not created:
            fd = actual_open(file, flags, *args, **kwargs)
            os.close(fd)
            path.chmod(raced_mode)
            created.append(True)
        return actual_open(file, flags, *args, **kwargs)

    def monitored_connect(*args, **kwargs):
        sqlite_calls.append(True)
        return actual_connect(*args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", planted_winner)
        ctx.setattr(trial_receiver.sqlite3, "connect", monitored_connect)
        with pytest.raises(TrialStoreError, match="competing trial database creation must be private"):
            TrialStore(path)

    assert created
    assert sqlite_calls == []
    assert mode(path) == raced_mode
    assert path.stat().st_nlink == 1
