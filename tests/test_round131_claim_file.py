"""Round 131: checkpoint.claim refuses a non-regular <file>.claim.

v0.123: claim() opened <ckpt>.claim with os.open(O_RDWR|O_CREAT) and no O_NOFOLLOW.
A symlink planted there (the data dir is writable by an agent's shell) was followed,
so a dangling link created its target anywhere the daemon could write and the flock
landed on that file. A FIFO, socket or directory was swallowed as "no flock here"
and the checkpoint was marked busy with no cross-process exclusion.

Round 102 closed the same hole for <file>.lock and fleet_nonces.json.lock. .claim
was left on the old open.
"""
from __future__ import annotations

import fcntl
import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from avrone import checkpoint


def _claim_path(path: Path) -> Path:
    return Path(str(path) + ".claim")


def test_dangling_symlink_creates_nothing_and_is_not_busy(tmp_path):
    outside = tmp_path / "outside" / "planted"
    outside.parent.mkdir()
    path = tmp_path / "job.json"
    os.symlink(outside, _claim_path(path))
    with pytest.raises(checkpoint.CheckpointConflict, match="not a regular file"):
        checkpoint.claim(path)
    assert not outside.exists()
    assert not checkpoint.is_busy(path)
    assert _claim_path(path).is_symlink()


def test_symlink_to_existing_file_does_not_open_it(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("do-not-touch")
    before = secret.stat()
    path = tmp_path / "job.json"
    os.symlink(secret, _claim_path(path))
    held = os.open(secret, os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        box: dict = {}

        def go():
            try:
                box["rel"] = checkpoint.claim(path)
            except BaseException as e:  # noqa: BLE001 - the assertion is the exception type
                box["e"] = e

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(2)
        assert not t.is_alive(), "claim blocked on the symlink target"
        err = box.get("e")
        assert isinstance(err, checkpoint.CheckpointConflict)
        assert "not a regular file" in str(err)
        assert "rel" not in box
        assert not checkpoint.is_busy(path)
        assert secret.read_text() == "do-not-touch"
        assert secret.stat().st_ino == before.st_ino
        assert secret.stat().st_size == before.st_size
    finally:
        os.close(held)


def test_fifo_claim_does_not_block_or_succeed(tmp_path):
    path = tmp_path / "job.json"
    os.mkfifo(_claim_path(path))
    box: dict = {}

    def go():
        try:
            box["rel"] = checkpoint.claim(path)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(2)
    if t.is_alive():
        # Unblock a parked open so the thread can die, then fail.
        try:
            os.open(_claim_path(path), os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            pass
        t.join(2)
        pytest.fail("claim blocked on a FIFO")
    assert isinstance(box.get("e"), checkpoint.CheckpointConflict)
    assert "not a regular file" in str(box["e"])
    assert not checkpoint.is_busy(path)


def test_directory_and_socket_are_not_claims(tmp_path):
    path = tmp_path / "job.json"
    os.mkdir(_claim_path(path))
    with pytest.raises(checkpoint.CheckpointConflict, match="not a regular file"):
        checkpoint.claim(path)
    assert not checkpoint.is_busy(path)
    os.rmdir(_claim_path(path))

    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(os.fspath(_claim_path(path)))
        with pytest.raises(checkpoint.CheckpointConflict, match="not a regular file"):
            checkpoint.claim(path)
    finally:
        sock.close()
    assert not checkpoint.is_busy(path)


def test_refused_claim_can_be_retried_once_the_path_is_regular(tmp_path):
    path = tmp_path / "job.json"
    os.symlink(tmp_path / "missing", _claim_path(path))
    with pytest.raises(checkpoint.CheckpointConflict):
        checkpoint.claim(path)
    _claim_path(path).unlink()
    rel = checkpoint.claim(path)
    try:
        assert checkpoint.is_busy(path)
        with pytest.raises(checkpoint.CheckpointConflict, match="in use by a running job"):
            checkpoint.claim(path)
    finally:
        rel()
    assert not checkpoint.is_busy(path)
    again = checkpoint.claim(path)
    again()
    assert not checkpoint.is_busy(path)


def test_regular_claim_is_cloexec_mode_600_and_exclusive(tmp_path):
    path = tmp_path / "nested" / "job.json"
    rel = checkpoint.claim(path)
    try:
        claim = _claim_path(path)
        assert claim.is_file()
        assert stat.S_IMODE(claim.stat().st_mode) == 0o600
        found = False
        for name in os.listdir("/proc/self/fd"):
            fd = int(name)
            try:
                if os.path.samefile(f"/proc/self/fd/{fd}", claim):
                    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
                    assert flags & fcntl.FD_CLOEXEC
                    found = True
            except OSError:
                continue
        assert found
        with pytest.raises(checkpoint.CheckpointConflict, match="in use"):
            checkpoint.claim(path)
    finally:
        rel()
    assert not checkpoint.is_busy(path)


def test_claim_swapped_for_symlink_after_release_creates_nothing(tmp_path):
    path = tmp_path / "job.json"
    rel = checkpoint.claim(path)
    rel()
    claim = _claim_path(path)
    claim.unlink()
    os.symlink(tmp_path / "elsewhere", claim)
    with pytest.raises(checkpoint.CheckpointConflict, match="not a regular file"):
        checkpoint.claim(path)
    assert not (tmp_path / "elsewhere").exists()
    assert not checkpoint.is_busy(path)


def test_concurrent_claims_exactly_one_winner(tmp_path):
    path = tmp_path / "job.json"
    wins: list = []
    errors: list = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            wins.append(checkpoint.claim(path))
        except checkpoint.CheckpointConflict as e:
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    assert len(errors) == 7
    assert all("in use" in str(e) for e in errors)
    wins[0]()
    assert not checkpoint.is_busy(path)
