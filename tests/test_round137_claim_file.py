"""Round 137: checkpoint.claim refuses a non-regular or shared <file>.claim.

v0.123: claim() opened <ckpt>.claim with os.open(O_RDWR|O_CREAT) and no O_NOFOLLOW.
A symlink planted there (the data dir is writable by an agent's shell) was followed,
so a dangling link created its target anywhere the daemon could write and the flock
landed on that file. A FIFO, socket or directory was swallowed as "no flock here"
and the checkpoint was marked busy with no cross-process exclusion. A hard link
(nlink > 1) is a regular file, so O_NOFOLLOW still opened it and the flock covered
the other name.

Round 102 closed the same symlink hole for <file>.lock and fleet_nonces.json.lock.
Main's round 131 is the sqlite error text. This file is round 137 so the two
round-131 names are not the same change.
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


def _refuse(path: Path, match: str) -> None:
    with pytest.raises(checkpoint.CheckpointConflict, match=match):
        checkpoint.claim(path)
    assert not checkpoint.is_busy(path)


def test_relative_symlink_and_chain_and_loop_create_nothing(tmp_path):
    outside = tmp_path / "outside" / "planted"
    outside.parent.mkdir()
    path = tmp_path / "job.json"
    os.symlink("../outside/planted", _claim_path(path))
    _refuse(path, "not a regular file")
    assert not outside.exists()
    _claim_path(path).unlink()

    mid = tmp_path / "mid"
    os.symlink(outside, mid)
    os.symlink(mid, _claim_path(path))
    _refuse(path, "not a regular file")
    assert not outside.exists()
    _claim_path(path).unlink()
    mid.unlink()

    a = tmp_path / "loop-a"
    b = tmp_path / "loop-b"
    os.symlink(b, a)
    os.symlink(a, b)
    os.symlink(a, _claim_path(path))
    _refuse(path, "not a regular file")


def test_symlink_to_checkpoint_json_devnull_and_directory(tmp_path):
    path = tmp_path / "job.json"
    path.write_text('{"version":1,"steps":{"meta":{"task":"keep"}}}')
    before = path.read_text()
    os.symlink(path, _claim_path(path))
    _refuse(path, "not a regular file")
    assert path.read_text() == before
    assert path.is_file() and not path.is_symlink()
    _claim_path(path).unlink()

    os.symlink("/dev/null", _claim_path(path))
    _refuse(path, "not a regular file")
    _claim_path(path).unlink()

    os.symlink(tmp_path, _claim_path(path))
    _refuse(path, "not a regular file")


def test_hard_link_does_not_lock_the_other_name(tmp_path):
    other = tmp_path / "notes.dat"
    other.write_text("KEEP")
    os.link(other, _claim_path(tmp_path / "job.json"))
    held = os.open(other, os.O_RDWR)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _refuse(tmp_path / "job.json", "linked to another name")
        assert other.read_text() == "KEEP"
        assert other.stat().st_nlink == 2
        # The exclusive lock we took is still ours: the claim did not join this inode.
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(held)


def test_other_process_holding_claim_conflicts(tmp_path):
    path = tmp_path / "job.json"
    ctx = __import__("multiprocessing").get_context("fork")
    ready, done = ctx.Event(), ctx.Event()

    def hold():
        rel = checkpoint.claim(str(path))
        ready.set()
        assert done.wait(20)
        rel()

    proc = ctx.Process(target=hold)
    proc.start()
    assert ready.wait(10)
    try:
        with pytest.raises(checkpoint.CheckpointConflict, match="another process"):
            checkpoint.claim(path)
        assert not checkpoint.is_busy(path)
    finally:
        done.set()
        proc.join(10)
    assert proc.exitcode == 0
    rel = checkpoint.claim(path)
    rel()


def test_double_release_and_many_cycles(tmp_path):
    path = tmp_path / "job.json"
    for _ in range(100):
        rel = checkpoint.claim(path)
        rel()
        rel()
        assert not checkpoint.is_busy(path)
    assert _claim_path(path).is_file()
    assert stat.S_IMODE(_claim_path(path).stat().st_mode) == 0o600


def test_distinct_checkpoints_do_not_block_each_other(tmp_path):
    rels = []
    try:
        for i in range(32):
            rels.append(checkpoint.claim(tmp_path / f"job-{i}.json"))
        assert all(checkpoint.is_busy(tmp_path / f"job-{i}.json") for i in range(32))
    finally:
        for rel in rels:
            rel()
    assert not any(checkpoint.is_busy(tmp_path / f"job-{i}.json") for i in range(32))


def test_embedded_nul_is_checkpoint_error(tmp_path):
    with pytest.raises(checkpoint.CheckpointError, match="not usable"):
        checkpoint.claim(str(tmp_path / "job") + "\0.json")


def test_parent_is_a_file_does_not_crash(tmp_path):
    parent = tmp_path / "not-a-dir"
    parent.write_text("x")
    rel = checkpoint.claim(parent / "job.json")
    try:
        assert checkpoint.is_busy(parent / "job.json")
        assert parent.read_text() == "x"
    finally:
        rel()
    assert not checkpoint.is_busy(parent / "job.json")


def test_name_at_filesystem_limit(tmp_path):
    # "<stem>.json.claim" must fit in NAME_MAX (255). 240-char stem -> 251-byte claim name.
    stem = "j" * 240
    path = tmp_path / f"{stem}.json"
    rel = checkpoint.claim(path)
    try:
        claim = _claim_path(path)
        assert claim.is_file()
        assert len(claim.name) == 251
        assert stat.S_ISREG(claim.stat().st_mode)
    finally:
        rel()
    # One byte over the limit: the open fails and the claim stays in-process only.
    too = tmp_path / f"{'k' * 250}.json"
    rel = checkpoint.claim(too)
    try:
        assert checkpoint.is_busy(too)
        assert _claim_path(too).name not in os.listdir(tmp_path)
    finally:
        rel()


def test_fifo_swapped_to_regular_then_symlink_to_fifo(tmp_path):
    path = tmp_path / "job.json"
    os.mkfifo(_claim_path(path))
    _refuse(path, "not a regular file")
    _claim_path(path).unlink()
    rel = checkpoint.claim(path)
    rel()
    _claim_path(path).unlink()
    fifo = tmp_path / "real.fifo"
    os.mkfifo(fifo)
    os.symlink(fifo, _claim_path(path))
    box: dict = {}

    def go():
        try:
            box["rel"] = checkpoint.claim(path)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(2)
    assert not t.is_alive(), "claim blocked on a symlink to a FIFO"
    assert isinstance(box.get("e"), checkpoint.CheckpointConflict)
    assert not checkpoint.is_busy(path)


def test_release_and_reclaim_under_threads(tmp_path):
    path = tmp_path / "job.json"
    stop = threading.Event()
    errors: list = []

    def churn():
        while not stop.is_set():
            try:
                rel = checkpoint.claim(path)
            except checkpoint.CheckpointConflict:
                continue
            try:
                if not checkpoint.is_busy(path):
                    errors.append("busy flag missing while held")
            finally:
                rel()

    threads = [threading.Thread(target=churn) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(1.5)
    stop.set()
    for t in threads:
        t.join(5)
    assert not errors
    assert not checkpoint.is_busy(path)
    rel = checkpoint.claim(path)
    rel()
