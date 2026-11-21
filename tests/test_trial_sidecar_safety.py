"""Regression matrix for SQLite sidecar file substitution and path races.

The receiver uses WAL. Reject planted symlinks, devices, directories and hard
links for its -wal/-shm/-journal neighbors *before* SQLite has an opportunity
to open them; repeat the check after database open.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def prepared(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "trials.sqlite3"
    store = TrialStore(path)
    trial = make_trial(
        system="cyclekernel", node="node-a",
        run_id="sidecar-run", check_id="storage", check_version="v2",
        attempt_id="attempt-sidecar", outcome="pass",
    )
    assert store.ingest_many([trial], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 1
    return store, path


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_preexisting_sidecar_symlink_is_refused_without_touching_target(tmp_path, suffix):
    store, path = prepared(tmp_path)
    sidecar = path.with_name(path.name + suffix)
    assert not sidecar.exists(), "expected all SQLite connections to be closed"
    target = tmp_path / "external.txt"
    sentinel = b"unrelated-external-data:must-stay-unchanged"
    target.write_bytes(sentinel)
    sidecar.symlink_to(target)

    with pytest.raises(TrialStoreError, match="sidecar"):
        store.pool_snapshot()

    assert sidecar.is_symlink()
    assert target.read_bytes() == sentinel
    sidecar.unlink()
    assert store.pool_snapshot()["runs"] == 1


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_preexisting_sidecar_hardlink_is_refused_without_mutation(tmp_path, suffix):
    store, path = prepared(tmp_path)
    sidecar = path.with_name(path.name + suffix)
    assert not sidecar.exists()
    target = tmp_path / "external.bin"
    sentinel = b"linked-file-must-not-change"
    target.write_bytes(sentinel)
    os.link(target, sidecar)
    assert target.stat().st_nlink == 2

    with pytest.raises(TrialStoreError, match="sidecar"):
        store.pool_snapshot()

    assert target.read_bytes() == sentinel
    assert sidecar.stat().st_nlink == 2
    sidecar.unlink()
    assert store.pool_snapshot()["runs"] == 1


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecar_directory_is_rejected_before_sqlite_open(tmp_path, suffix):
    store, path = prepared(tmp_path)
    sidecar = path.with_name(path.name + suffix)
    assert not sidecar.exists()
    sidecar.mkdir(mode=0o700)

    with pytest.raises(TrialStoreError, match="sidecar"):
        store.pool_snapshot()

    assert sidecar.is_dir()
    sidecar.rmdir()
    assert store.pool_snapshot()["runs"] == 1


def test_sidecar_planted_between_precheck_and_sqlite_open_is_detected(
    tmp_path, monkeypatch,
):
    store, path = prepared(tmp_path)
    sidecar = path.with_name(path.name + "-journal")
    assert not sidecar.exists()
    external = tmp_path / "unrelated"
    sentinel = b"do-not-touch-external-target"
    external.write_bytes(sentinel)

    from avrone import trial_receiver
    real_connect = sqlite3.connect
    connections = []

    def inject_after_open(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        sidecar.symlink_to(external)
        connections.append(db)
        return db

    monkeypatch.setattr(trial_receiver.sqlite3, "connect", inject_after_open)
    with pytest.raises(TrialStoreError, match="sidecar"):
        store.pool_snapshot()

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert sidecar.is_symlink()
    assert external.read_bytes() == sentinel
