"""Round 104: cross-process flocks have a deadline (AVRONE_LOCK_WAIT_S).

v0.96: memstore, capgate receipts, the bridge journal, task_board and checkpoints all did a
blocking fcntl.flock(LOCK_EX). A peer process that hung while holding one of those locks pinned
every worker thread that touched the file forever; with the default executor's workers all stuck,
every to_thread call on the node stopped. Each test holds the lock from a second open file
description (flock conflicts across those, even in one process) and runs the op in a thread with a
join timeout, so old code fails instead of hanging the suite. The holder is released at the end,
which also lets a stuck old-code thread finish.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentkit, bridge, capgate, checkpoint, memstore, safeio


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch):
    monkeypatch.setenv("AVRONE_LOCK_WAIT_S", "0.3")


class Holder:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def release(self):
        os.close(self.fd)


def _bounded(fn, limit=5.0):
    box = {}

    def go():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(limit)
    return (not t.is_alive()), box, time.monotonic() - t0


def test_flock_wait_helper(tmp_path):
    f = tmp_path / "x.lock"
    h = Holder(f)
    fd = os.open(f, os.O_RDWR)
    try:
        t0 = time.monotonic()
        assert safeio.flock_wait(fd, 0.2) is False
        assert 0.15 < time.monotonic() - t0 < 2
        h.release()
        assert safeio.flock_wait(fd, 0.2) is True
    finally:
        os.close(fd)


def test_lock_wait_env_junk(monkeypatch):
    for v, want in (("nan", 30.0), ("-1", 30.0), ("abc", 30.0), ("1e9", 3600.0), ("0", 0.0), ("2.5", 2.5)):
        monkeypatch.setenv("AVRONE_LOCK_WAIT_S", v)
        assert safeio.lock_wait_s() == want


def test_memstore_refuses_when_lock_held(tmp_path):
    ms = memstore.MemoryStore(tmp_path / "memory.jsonl")
    h = Holder(ms.lock_path)
    try:
        done, box, _ = _bounded(lambda: ms.save("ns", "k", "v"))
        assert done, "memory save blocked forever on a held lock"
        assert isinstance(box.get("e"), memstore.MemoryRefused) and "locked by another process" in str(box["e"])
    finally:
        h.release()
    assert ms.save("ns", "k", "v")  # works once the peer lets go
    assert ms.load("ns")["k"]["value"] == "v"


def test_capgate_record_gives_up_unlocked_never(tmp_path):
    p = capgate.receipts_path(tmp_path)
    h = Holder(Path(str(p) + ".lock"))
    try:
        done, box, _ = _bounded(lambda: capgate.record(tmp_path, caller="a", tool="t", args={}, outcome="ok"))
        assert done, "receipt append blocked forever on a held chain lock"
        assert isinstance(box.get("e"), OSError)
        assert not p.exists() or p.read_text() == ""  # nothing appended without the lock
    finally:
        h.release()
    capgate.record(tmp_path, caller="a", tool="t", args={}, outcome="ok")
    assert capgate.verify_chain(tmp_path).get("ok")


def test_bridge_post_kept_in_memory(tmp_path):
    b = bridge.Bridge("run-x", data_dir=tmp_path)
    h = Holder(Path(str(b.path) + ".lock"))
    try:
        done, box, _ = _bounded(lambda: b.post("A", "finding", "hello"))
        assert done, "bridge post blocked forever on a held journal lock"
        assert "e" not in box
    finally:
        h.release()
    b.post("A", "finding", "second")
    assert "second" in b.path.read_text()


def _board_ctx(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path / "d"),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="A", depth=0)


def test_task_board_busy_error(tmp_path):
    c = _board_ctx(tmp_path)
    p = agentkit._board(c)
    h = Holder(p.with_name(p.name + ".lock"))
    try:
        done, box, _ = _bounded(lambda: asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": "x"})))
        assert done, "task_board blocked forever on a held board lock"
        assert str(box.get("v", "")).startswith("ERROR") and "locked" in box["v"]
    finally:
        h.release()
    assert "#1" in asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": "y"}))


def test_checkpoint_save_fails_cleanly(tmp_path):
    path = tmp_path / "run.json"
    ck = checkpoint.Checkpoint(path)
    h = Holder(Path(str(path) + ".lock"))
    try:
        done, box, _ = _bounded(lambda: ck.put("k", 1))
        assert done, "checkpoint put blocked forever on a held lock"
        assert box.get("v") is False
    finally:
        h.release()
    assert ck.put("k", 2) is True


def test_uncontended_paths_fast(tmp_path):
    ms = memstore.MemoryStore(tmp_path / "m" / "memory.jsonl")
    t0 = time.monotonic()
    for i in range(200):
        ms.save("ns", f"k{i}", "v")
    assert time.monotonic() - t0 < 10
