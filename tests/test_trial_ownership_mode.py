"""Storage owner, mode-drift and post-connect file-permission adversarial regressions.

Existing public-mode legacy DB files may be normalized by a fresh receiver
during initialization within an already-private directory. A running receiver
must never silently continue after a database, sidecar or directory owner /
database file mode changes.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def _trial(attempt_id="189"):
    return make_trial(
        system="cyclekernel",
        node="node-a",
        run_id="run-mode",
        check_id="permissions",
        check_version="v2",
        attempt_id=attempt_id,
        outcome="pass",
    )


def _private_store(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    store = TrialStore(path)
    first = store.ingest_many([_trial()], sender="peer-a", transport="fleet")
    assert first["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return store, path


@pytest.mark.parametrize(
    "mode", [
        pytest.param(0o644, id="world-readable"),
        pytest.param(0o666, id="world-writable"),
        pytest.param(0o640, id="group-readable"),
        pytest.param(0o400, id="owner-readonly"),
        pytest.param(0o700, id="unexpected-execute-bits"),
    ]
)
def test_live_database_mode_drift_denies_reads_and_writes_then_recovers(
    tmp_path, mode,
):
    store, path = _private_store(tmp_path)
    sentinel = path.read_bytes()
    path.chmod(mode)

    with pytest.raises(TrialStoreError, match="database permissions changed"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="database permissions changed"):
        store.ingest_many([_trial("new")], sender="peer-a", transport="fleet")

    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert path.read_bytes() == sentinel

    path.chmod(0o600)
    assert store.ingest_many([_trial("new")], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 2


def test_fresh_receiver_restores_existing_legacy_file_mode_in_private_parent(tmp_path):
    _, path = _private_store(tmp_path)
    path.chmod(0o644)
    reopened = TrialStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert reopened.pool_snapshot()["runs"] == 1


def test_mode_drift_during_sqlite_connect_closes_untrusted_connection(
    tmp_path, monkeypatch,
):
    store, path = _private_store(tmp_path)
    from avrone import trial_receiver

    real_connect = sqlite3.connect
    opened = []

    def changed_mode_during_open(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        path.chmod(0o644)
        opened.append(conn)
        return conn

    monkeypatch.setattr(trial_receiver.sqlite3, "connect", changed_mode_during_open)
    with pytest.raises(TrialStoreError, match="database permissions changed"):
        store.pool_snapshot()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")

    path.chmod(0o600)
    assert store.pool_snapshot()["runs"] == 1


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX effective UID required")
def test_existing_parent_with_simulated_wrong_owner_is_rejected_without_db_creation(
    tmp_path, monkeypatch,
):
    parent = tmp_path / "other-user"
    parent.mkdir(mode=0o700)
    path = parent / "trials.sqlite3"
    original_mode = stat.S_IMODE(parent.stat().st_mode)
    real_uid = os.geteuid()
    with monkeypatch.context() as ctx:
        ctx.setattr(os, "geteuid", lambda: real_uid + 1)
        with pytest.raises(TrialStoreError, match="owned by the receiver"):
            TrialStore(path)

    assert not path.exists()
    assert stat.S_IMODE(parent.stat().st_mode) == original_mode


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX effective UID required")
def test_existing_store_refuses_simulated_euid_change_and_recovers(tmp_path, monkeypatch):
    store, _ = _private_store(tmp_path)
    real_uid = os.geteuid()
    with monkeypatch.context() as ctx:
        ctx.setattr(os, "geteuid", lambda: real_uid + 1)
        with pytest.raises(TrialStoreError, match="owned by the receiver"):
            store.pool_snapshot()
    assert store.pool_snapshot()["runs"] == 1


def _spoof_owner_for_path(monkeypatch, selected: Path):
    """Spoof only the target lstat owner; do not actually chown files on CI."""
    real_lstat = Path.lstat

    def changed_owner(self):
        info = real_lstat(self)
        if self != selected:
            return info
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_nlink=info.st_nlink,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_uid=info.st_uid + 1,
        )

    monkeypatch.setattr(Path, "lstat", changed_owner)


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX effective UID required")
def test_db_file_owner_drift_is_denied_before_sqlite_open(tmp_path, monkeypatch):
    store, path = _private_store(tmp_path)
    with monkeypatch.context() as ctx:
        _spoof_owner_for_path(ctx, path)
        with pytest.raises(TrialStoreError, match="trial database must be owned"):
            store.pool_snapshot()
    assert store.pool_snapshot()["runs"] == 1


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX effective UID required")
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecar_owner_mismatch_fails_closed_for_each_suffix(tmp_path, monkeypatch, suffix):
    store, path = _private_store(tmp_path)
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"untrusted-sidecar-sentinel")
    before = sidecar.read_bytes()
    with monkeypatch.context() as ctx:
        _spoof_owner_for_path(ctx, sidecar)
        with pytest.raises(TrialStoreError, match="sidecar must be owned"):
            store.pool_snapshot()
    assert sidecar.read_bytes() == before
    sidecar.unlink()
    assert store.pool_snapshot()["runs"] == 1


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX effective UID required")
def test_sidecar_owner_swap_between_precheck_and_sqlite_connect_is_rejected(
    tmp_path, monkeypatch,
):
    store, path = _private_store(tmp_path)
    target = Path(str(path) + "-journal")
    from avrone import trial_receiver
    real_connect = sqlite3.connect
    opened = []

    def planted_after_open(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        target.write_bytes(b"untrusted-sidecar-sentinel")
        opened.append(db)
        return db

    # The selected file exists only after opening; its spoofed owner must
    # be checked in the *post*-open guard and the connection must be closed.
    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.sqlite3, "connect", planted_after_open)
        _spoof_owner_for_path(ctx, target)
        with pytest.raises(TrialStoreError, match="sidecar must be owned"):
            store.pool_snapshot()

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert target.read_bytes() == b"untrusted-sidecar-sentinel"
    target.unlink()
    assert store.pool_snapshot()["runs"] == 1


def test_healthy_private_store_retains_mode_owner_and_idempotency(tmp_path):
    store, path = _private_store(tmp_path)
    for _ in range(3):
        result = store.ingest_many([_trial()], sender="peer-a", transport="fleet")
        assert result["duplicates"] == 1
        assert store.pool_snapshot()["runs"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if hasattr(os, "geteuid"):
        assert path.stat().st_uid == os.geteuid()
