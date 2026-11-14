"""Round 205: fail closed on SQLite WAL/SHM/journal file permission drift.

R188 checks sidecars are regular, single-link, and not symlinks. R189 pins
their owner. This matrix additionally rejects a running receiver's sidecars
whose mode differs from 0600, even if they remain under a private directory.
"""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError


def trial(attempt="205"):
    return make_trial(
        system="cyclekernel", node="node-205",
        run_id="run-sidecar-mode", check_id="privacy", check_version="v2",
        attempt_id=attempt, outcome="pass",
    )


def prepared(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "trials.sqlite3"
    store = TrialStore(path)
    assert store.ingest_many([trial()], sender="peer-a", transport="fleet")["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 1
    return store, path


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize(
    "mode", [0o644, 0o666, 0o604, 0o400, 0o700],
    ids=["world-readable", "world-writable", "world-owner-mixed", "read-only", "executable"],
)
def test_planted_sidecar_mode_is_denied_before_sqlite_open(tmp_path, suffix, mode):
    store, path = prepared(tmp_path)
    sidecar = path.with_name(path.name + suffix)
    assert not sidecar.exists()
    sentinel = b"never-open-planted-sidecar"
    sidecar.write_bytes(sentinel)
    sidecar.chmod(mode)
    with pytest.raises(TrialStoreError, match="sidecar permissions"):
        store.pool_snapshot()
    assert sidecar.read_bytes() == sentinel
    assert stat.S_IMODE(sidecar.stat().st_mode) == mode

    sidecar.unlink()
    assert store.pool_snapshot()["runs"] == 1
    assert store.ingest_many([trial("second")], sender="peer-b", transport="fleet")["accepted"] == 1


def test_real_sqlite_wal_and_shm_inherit_private_database_mode(tmp_path):
    store, path = prepared(tmp_path)
    db = store._connect()
    try:
        db.execute("INSERT INTO trial_receipts("
                   "trial_id,sender,transport,bridge_run_id,result,"
                   "payload_sha256,received_at"
                   ") SELECT trial_id,'peer-c','fleet',NULL,'duplicate',"
                   "payload_sha256,received_at FROM trials LIMIT 1")
        db.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            assert sidecar.is_file()
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
        assert store.pool_snapshot()["runs"] == 1
    finally:
        db.close()
    assert store.pool_snapshot()["runs"] == 1


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_mode_drift_after_connect_fails_and_closes_untrusted_connection(
    tmp_path, monkeypatch, suffix,
):
    store, path = prepared(tmp_path)
    sidecar = path.with_name(path.name + suffix)
    assert not sidecar.exists()
    from avrone import trial_receiver
    real_connect = sqlite3.connect
    opened = []
    sentinel = b"post-open-injected-private-sidecar"

    def inject_after_open(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        sidecar.write_bytes(sentinel)
        sidecar.chmod(0o644)
        opened.append(connection)
        return connection

    with monkeypatch.context() as ctx:
        ctx.setattr(trial_receiver.sqlite3, "connect", inject_after_open)
        with pytest.raises(TrialStoreError, match="sidecar permissions"):
            store.pool_snapshot()

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert sidecar.read_bytes() == sentinel
    sidecar.unlink()
    assert store.pool_snapshot()["runs"] == 1


def test_private_sidecar_mode_check_does_not_chmod_existing_file(tmp_path):
    store, path = prepared(tmp_path)
    sidecar = path.with_name(path.name + "-journal")
    sentinel = b"file-should-not-be-mutated"
    sidecar.write_bytes(sentinel)
    sidecar.chmod(0o666)
    with pytest.raises(TrialStoreError, match="sidecar permissions"):
        store.pool_snapshot()
    assert sidecar.read_bytes() == sentinel
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o666
    sidecar.unlink()
    assert store.pool_snapshot()["runs"] == 1

def test_fresh_database_is_private_before_wal_transition_under_umask_022(
    tmp_path, monkeypatch,
):
    """Regression: a fresh DB may begin as 0644, but WAL must start at 0600."""
    private = tmp_path / "umask-private"
    private.mkdir(mode=0o700)
    path = private / "trials.sqlite3"
    real_enable_wal = TrialStore._enable_wal
    observed = []

    def inspected_wal_transition(db):
        observed.append(stat.S_IMODE(path.stat().st_mode))
        assert observed[-1] == 0o600
        return real_enable_wal(db)

    monkeypatch.setattr(
        TrialStore, "_enable_wal", staticmethod(inspected_wal_transition)
    )
    old_mask = os.umask(0o022)
    try:
        store = TrialStore(path)
    finally:
        os.umask(old_mask)
    assert observed == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.ingest_many(
        [trial("new-under-022")], sender="peer-a", transport="fleet"
    )["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 1


def test_legacy_0644_owned_database_is_private_before_wal_transition(
    tmp_path, monkeypatch,
):
    """A trusted legacy file must not spawn publicly readable WAL/SHM files."""
    store, path = prepared(tmp_path)
    path.chmod(0o644)
    observed = []
    real_enable_wal = TrialStore._enable_wal

    def inspected_wal_transition(db):
        observed.append(stat.S_IMODE(path.stat().st_mode))
        assert observed[-1] == 0o600
        return real_enable_wal(db)

    monkeypatch.setattr(
        TrialStore, "_enable_wal", staticmethod(inspected_wal_transition)
    )
    reopened = TrialStore(path)
    assert observed == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert reopened.pool_snapshot()["runs"] == 1
