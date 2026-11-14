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
