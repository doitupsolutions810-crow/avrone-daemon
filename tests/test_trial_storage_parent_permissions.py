"""Regression tests: receiver must never chmod a shared pre-existing directory.

Creating /tmp/trials.db used to change /tmp to 0700, affecting unrelated
processes. Refuse unsafe parent modes before opening or creating SQLite files.
"""
from __future__ import annotations

import os
import stat

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize(
    "parent_mode",
    [
        pytest.param(0o755, id="world-readable"),
        pytest.param(0o750, id="group-traversable"),
        pytest.param(0o770, id="group-writable"),
        pytest.param(0o777, id="world-writable"),
        pytest.param(0o711, id="world-traversable"),
        pytest.param(0o700 | stat.S_ISVTX, id="sticky-bit"),
    ],
)
def test_existing_nonprivate_parent_is_rejected_without_chmod_or_db_creation(
    tmp_path, parent_mode,
):
    shared = tmp_path / "existing-parent"
    shared.mkdir(mode=0o700)
    shared.chmod(parent_mode)
    sentinel = shared / "other-users-data.txt"
    original = b"must-never-be-touched"
    sentinel.write_bytes(original)
    database = shared / "trials.sqlite3"
    before = _mode(shared)

    with pytest.raises(TrialStoreError, match="must already be private"):
        TrialStore(database)

    assert not database.exists()
    assert _mode(shared) == before
    assert sentinel.read_bytes() == original


def test_existing_nonprivate_parent_does_not_touch_existing_database(tmp_path):
    shared = tmp_path / "existing-parent"
    shared.mkdir(mode=0o700)
    database = shared / "trials.sqlite3"
    sentinel = b"not-a-database\x00original-bytes"
    database.write_bytes(sentinel)
    shared.chmod(0o755)

    with pytest.raises(TrialStoreError, match="must already be private"):
        TrialStore(database)

    assert database.read_bytes() == sentinel
    assert _mode(shared) == 0o755


def test_existing_private_parent_is_accepted_and_never_chmodded(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    real_chmod = os.chmod
    touched = []

    def record_chmod(target, mode, *args, **kwargs):
        touched.append((os.fspath(target), mode))
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", record_chmod)
    store = TrialStore(path)
    assert _mode(parent) == 0o700
    assert _mode(path) == 0o600
    assert not any(target == os.fspath(parent) for target, _ in touched)

    trial = make_trial(
        system="cyclekernel", node="node-a", run_id="run-dir",
        check_id="storage", check_version="v2", attempt_id="attempt-187",
        outcome="pass",
    )
    assert store.ingest_many([trial], sender="peer-a", transport="fleet")["accepted"] == 1
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 1
    assert not any(target == os.fspath(parent) for target, _ in touched)


def test_new_private_parent_is_created_with_private_permissions(tmp_path):
    path = tmp_path / "fresh" / "leaf" / "trials.sqlite3"
    assert not path.parent.exists()
    TrialStore(path)
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600


def test_new_parent_with_restrictive_umask_remains_private(tmp_path):
    parent = tmp_path / "umask-leaf"
    path = parent / "trials.sqlite3"
    old_umask = os.umask(0o077)
    try:
        TrialStore(path)
    finally:
        os.umask(old_umask)
    assert _mode(parent) == 0o700
    assert _mode(path) == 0o600


def test_shared_parent_existing_sqlite_is_not_modified(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    TrialStore(path)
    original = path.read_bytes()
    parent.chmod(0o777)

    with pytest.raises(TrialStoreError, match="must already be private"):
        TrialStore(path)

    assert path.read_bytes() == original
    assert _mode(parent) == 0o777


@pytest.mark.parametrize(
    "unsafe_mode",
    [
        pytest.param(0o755, id="world-readable-after-open"),
        pytest.param(0o777, id="world-writable-after-open"),
        pytest.param(0o770, id="group-writable-after-open"),
        pytest.param(0o500, id="owner-no-write-after-open"),
    ],
)
def test_permission_drift_fails_closed_for_existing_store_then_recovers(
    tmp_path, unsafe_mode,
):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    store = TrialStore(path)
    trial = make_trial(
        system="cyclekernel", node="node-a", run_id="run-drift",
        check_id="storage", check_version="v2", attempt_id="drift-187",
        outcome="pass",
    )
    before = path.read_bytes()
    parent.chmod(unsafe_mode)

    with pytest.raises(TrialStoreError, match="must already be private"):
        store.ingest_many([trial], sender="peer-a", transport="fleet")
    with pytest.raises(TrialStoreError, match="must already be private"):
        store.pool_snapshot()

    assert path.read_bytes() == before
    assert _mode(parent) == unsafe_mode
    parent.chmod(0o700)
    assert store.ingest_many([trial], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 1


def test_private_parent_inode_replacement_is_rejected_until_store_reopened(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    store = TrialStore(path)
    displaced = tmp_path / "displaced-private"
    parent.rename(displaced)
    parent.mkdir(mode=0o700)
    assert _mode(parent) == 0o700
    assert parent.stat().st_ino != displaced.stat().st_ino

    with pytest.raises(TrialStoreError, match="identity changed"):
        store.pool_snapshot()

    assert not path.exists()
    assert (displaced / "trials.sqlite3").exists()
    # A new receiver instance can intentionally open a different private
    # directory, but a live receiver may not silently switch evidence stores.
    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 0


def test_parent_swapped_to_symlink_after_open_is_rejected(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    store = TrialStore(path)
    displaced = tmp_path / "moved-private"
    parent.rename(displaced)
    parent.symlink_to(displaced, target_is_directory=True)

    with pytest.raises(TrialStoreError, match="symlink"):
        store.pool_snapshot()

    assert (displaced / "trials.sqlite3").exists()
