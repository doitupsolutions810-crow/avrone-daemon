"""Round 212: descriptor-pinned permissions protect the SQLite WAL transition.

The database is made private (0600) before SQLite creates -wal/-shm, but a
path-based chmod can follow a replaced symlink. The target must be the exact
regular, receiver-owned, single-linked inode validated after sqlite3.connect.
These cases mutate the pathname at both sides of os.open/os.fchmod and prove
foreign files never have their permissions silently changed.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from avrone import trial_receiver
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def fixture(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    store = TrialStore(path)
    envelope = make_trial(
        system="cyclekernel", node="node-fd", run_id="run-212",
        check_id="database-fd", check_version="v2",
        attempt_id="seed", outcome="pass",
    )
    assert store.ingest_many([envelope], sender="peer-a", transport="fleet")["accepted"] == 1
    outside = tmp_path / "outside.txt"
    outside.write_text("unrelated foreign file", encoding="utf-8")
    outside.chmod(0o644)
    return store, path, outside


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_mode_finalization_is_descriptor_based_not_path_chmod(
    tmp_path, monkeypatch,
):
    store, path, outside = fixture(tmp_path)
    previous = mode(path)

    def forbidden_path_chmod(*args, **kwargs):
        raise AssertionError("unsafe path-based chmod must not be used")

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "chmod", forbidden_path_chmod)
        store._make_database_private_before_wal()

    assert mode(path) == previous == 0o600
    assert mode(outside) == 0o644
    assert store.pool_snapshot()["runs"] == 1


def test_path_replaced_with_symlink_just_before_descriptor_open_is_refused(
    tmp_path, monkeypatch,
):
    store, path, outside = fixture(tmp_path)
    displaced = path.with_name("original.sqlite3")
    real_open = os.open
    did_swap = []

    def substituted_open(file, flags, *args, **kwargs):
        if Path(file) == path and not did_swap:
            path.rename(displaced)
            path.symlink_to(outside)
            did_swap.append(True)
        return real_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", substituted_open)
        with pytest.raises(TrialStoreError, match="secured before SQLite WAL"):
            store._make_database_private_before_wal()

    assert did_swap
    assert path.is_symlink()
    assert mode(outside) == 0o644
    assert displaced.exists()
    path.unlink()
    displaced.rename(path)
    assert store.pool_snapshot()["runs"] == 1


def test_path_replaced_with_distinct_regular_file_just_before_open_is_refused(
    tmp_path, monkeypatch,
):
    store, path, outside = fixture(tmp_path)
    displaced = path.with_name("original.sqlite3")
    replacement = path.with_name("unrelated.sqlite3")
    replacement.write_bytes(b"not a database")
    replacement.chmod(0o644)
    real_open = os.open
    injected = []

    def substituted_open(file, flags, *args, **kwargs):
        if Path(file) == path and not injected:
            path.rename(displaced)
            replacement.rename(path)
            injected.append(True)
        return real_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", substituted_open)
        with pytest.raises(TrialStoreError, match="identity changed before permission"):
            store._make_database_private_before_wal()

    assert injected
    assert mode(path) == mode(outside) == 0o644
    path.unlink()
    displaced.rename(path)
    assert store.pool_snapshot()["runs"] == 1


def test_hardlinked_replacement_refused_before_fchmod(tmp_path, monkeypatch):
    store, path, outside = fixture(tmp_path)
    displaced = path.with_name("original.sqlite3")
    real_open = os.open
    injected = []

    def substituted_open(file, flags, *args, **kwargs):
        if Path(file) == path and not injected:
            path.rename(displaced)
            os.link(outside, path)
            injected.append(True)
        return real_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", substituted_open)
        with pytest.raises(TrialStoreError, match="single-link"):
            store._make_database_private_before_wal()

    assert injected
    assert path.stat().st_nlink == 2
    assert mode(outside) == 0o644
    path.unlink()
    displaced.rename(path)
    assert store.pool_snapshot()["runs"] == 1


def test_symlink_swap_during_fchmod_does_not_chmod_outside_target(
    tmp_path, monkeypatch,
):
    store, path, outside = fixture(tmp_path)
    displaced = path.with_name("original.sqlite3")
    real_fchmod = os.fchmod

    def swap_after_fd_chmod(fd, requested_mode):
        real_fchmod(fd, requested_mode)
        path.rename(displaced)
        path.symlink_to(outside)

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "fchmod", swap_after_fd_chmod)
        with pytest.raises(TrialStoreError, match="symlink"):
            store._make_database_private_before_wal()

    assert mode(outside) == 0o644
    assert mode(displaced) == 0o600
    path.unlink()
    displaced.rename(path)
    assert store.pool_snapshot()["runs"] == 1


def test_fchmod_permission_error_closes_the_opened_descriptor(
    tmp_path, monkeypatch,
):
    store, path, outside = fixture(tmp_path)
    real_open = os.open
    opened = []

    def capturing_open(file, flags, *args, **kwargs):
        fd = real_open(file, flags, *args, **kwargs)
        if Path(file) == path:
            opened.append(fd)
        return fd

    def forbidden_fchmod(fd, requested_mode):
        raise PermissionError("descriptor chmod refused")

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "open", capturing_open)
        ctx.setattr(trial_receiver.os, "fchmod", forbidden_fchmod)
        with pytest.raises(TrialStoreError, match="cannot be secured before SQLite WAL"):
            store._make_database_private_before_wal()

    assert len(opened) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])
    assert mode(outside) == 0o644
    assert store.pool_snapshot()["runs"] == 1


def test_missing_secure_nofollow_flag_fails_closed(tmp_path, monkeypatch):
    store, path, outside = fixture(tmp_path)
    with monkeypatch.context() as ctx:
        ctx.delattr(trial_receiver.os, "O_NOFOLLOW", raising=False)
        with pytest.raises(TrialStoreError, match="secure descriptor-based"):
            store._make_database_private_before_wal()
    assert mode(path) == 0o600
    assert mode(outside) == 0o644


def test_live_initializer_refuses_symlink_swap_during_private_finalization(
    tmp_path, monkeypatch,
):
    store, path, outside = fixture(tmp_path)
    real_helper = TrialStore._make_database_private_before_wal
    displaced = path.with_name("original.sqlite3")
    attempted = []

    def swap_before_helper(self):
        if self.path == path and not attempted:
            path.rename(displaced)
            path.symlink_to(outside)
            attempted.append(True)
        return real_helper(self)

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_make_database_private_before_wal", swap_before_helper)
        with pytest.raises(TrialStoreError):
            TrialStore(path)

    assert attempted
    assert path.is_symlink()
    assert mode(outside) == 0o644
    path.unlink()
    displaced.rename(path)
    assert TrialStore(path).pool_snapshot()["runs"] == 1

@pytest.mark.parametrize("legacy", [False, True], ids=["new-db", "owned-legacy-0644"])
def test_fresh_and_legacy_initialization_never_use_path_chmod(
    tmp_path, monkeypatch, legacy,
):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    if legacy:
        first = TrialStore(path)
        assert first.pool_snapshot()["runs"] == 0
        path.chmod(0o644)

    def forbidden_path_chmod(*args, **kwargs):
        raise AssertionError("the receiver must not chmod by pathname")

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.os, "chmod", forbidden_path_chmod)
        store = TrialStore(path)
        assert store.pool_snapshot()["runs"] == 0

    assert mode(path) == 0o600
    assert TrialStore(path).pool_snapshot()["runs"] == 0


def test_schema_version_commit_does_not_follow_second_swapped_path(
    tmp_path, monkeypatch,
):
    """The migration commit must not use a second path-based chmod."""
    store, path, outside = fixture(tmp_path)
    original_helper = TrialStore._make_database_private_before_wal
    displaced = path.with_name("original.sqlite3")
    swapped = []

    def replace_after_verified_fchmod(self):
        original_helper(self)
        if self.path == path and not swapped:
            path.rename(displaced)
            path.symlink_to(outside)
            swapped.append(True)

    def forbid_path_chmod(*args, **kwargs):
        raise AssertionError("a second path chmod would follow the planted symlink")

    with monkeypatch.context() as ctx:
        ctx.setattr(TrialStore, "_make_database_private_before_wal",
                    replace_after_verified_fchmod)
        ctx.setattr(trial_receiver.os, "chmod", forbid_path_chmod)
        # The final identity check must refuse a constructor whose pathname
        # was swapped after the pinned chmod step, even though no outsider
        # permissions were changed.
        with pytest.raises(TrialStoreError):
            TrialStore(path)

    assert swapped
    assert mode(outside) == 0o644
    assert path.is_symlink()
    path.unlink()
    displaced.rename(path)
    assert store.pool_snapshot()["runs"] == 1
