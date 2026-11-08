"""Round 101: env_install lock and ledger off the event loop.

v0.93: the per-manager lock was open(lock, "w") on the loop (a FIFO there hung the loop forever,
a symlink there truncated its target), the 16 MB ledger scan ran on the loop twice per install, and
a cancelled install left a worker thread blocked in flock() on an already-closed file.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from avrone import agentkit
from avrone.sovereign import CommandResult


class Runner:
    def __init__(self, ws, delay=0.0):
        self.workspace, self.kind, self.cmds, self.delay = ws, "local", [], delay

    async def run(self, cmd, timeout=None):
        self.cmds.append(cmd)
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(cmd, 0, "ok", "")


def ctx(tmp, delay=0.0):
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    d = tmp / "d"
    d.mkdir(exist_ok=True)
    core = SimpleNamespace(runner=Runner(ws, delay), settings=SimpleNamespace(data_dir=d),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="Agent-1", depth=0)


@pytest.fixture(autouse=True)
def _allow(monkeypatch):
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "allow")


def _in_thread(coro_fn, timeout=5.0):
    box = {}

    def go():
        try:
            box["out"] = asyncio.run(coro_fn())
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return t, box


def test_fifo_lock_does_not_hang(tmp_path):
    c = ctx(tmp_path)
    lock = tmp_path / "d" / ".install-pip.lock"
    os.mkfifo(lock)
    t, box = _in_thread(lambda: agentkit.t_env_install(c, {"package": "requests"}), 4.0)
    if t.is_alive():
        fd = os.open(lock, os.O_RDWR | os.O_NONBLOCK)  # unblock the hung open
        t.join(3)
        os.close(fd)
        pytest.fail("env_install hung on a FIFO lock file")
    assert box.get("out", "").startswith("ERROR") and "install lock" in box["out"]
    assert c.core.runner.cmds == []


def test_symlink_lock_never_truncates_target(tmp_path):
    c = ctx(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    os.symlink(victim, tmp_path / "d" / ".install-pip.lock")
    out = asyncio.run(agentkit.t_env_install(c, {"package": "requests"}))
    assert victim.read_text() == "precious"
    assert out.startswith("ERROR") and c.core.runner.cmds == []


def _hold(lock):
    f = open(lock, "a+b")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def test_lock_wait_times_out(tmp_path, monkeypatch):
    if not hasattr(agentkit, "INSTALL_LOCK_WAIT_S"):
        pytest.fail("no bounded lock wait")
    monkeypatch.setattr(agentkit, "INSTALL_LOCK_WAIT_S", 0.3)
    c = ctx(tmp_path)
    h = _hold(tmp_path / "d" / ".install-pip.lock")
    try:
        out = asyncio.run(asyncio.wait_for(agentkit.t_env_install(c, {"package": "requests"}), 5))
    finally:
        h.close()
    assert out.startswith("ERROR") and "install lock" in out and c.core.runner.cmds == []


def test_cancel_while_waiting_leaves_no_blocked_thread(tmp_path):
    c = ctx(tmp_path)
    h = _hold(tmp_path / "d" / ".install-pip.lock")
    try:
        async def main():
            task = asyncio.create_task(agentkit.t_env_install(c, {"package": "requests"}))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.1)
            return [t for t in threading.enumerate() if t.name.startswith("asyncio_")]
        # asyncio.run shuts the default executor down: a thread still blocked in flock() keeps it
        # waiting until the lock frees (v0.93 hung here)
        t, box = _in_thread(main, 5.0)
        hung = t.is_alive()
    finally:
        h.close()
    if hung:
        t.join(5)
        pytest.fail("cancelled env_install left a worker thread blocked in flock()")
    assert "err" not in box, box.get("err")
    assert [x for x in box["out"] if x.is_alive()] == []
    assert c.core.runner.cmds == []


def test_big_ledger_scan_off_loop(tmp_path):
    c = ctx(tmp_path)
    led = tmp_path / "d" / "resources.jsonl"
    row = json.dumps({"ts": 1, "agent": "A", "action": "pip install", "target": "x" * 40, "ok": True,
                      "detail": "d" * 300}) + "\n"
    with open(led, "w") as f:
        f.write(row * (15 * 1024 * 1024 // len(row)))
    ticks = []

    async def main():
        stop = False

        async def ticker():
            while not stop:
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.005)
        tk = asyncio.create_task(ticker())
        await asyncio.sleep(0.02)
        t0 = time.perf_counter()
        out = await agentkit.t_env_install(c, {"package": "requests"})
        el = time.perf_counter() - t0
        stop = True
        await tk
        return out, el
    out, el = asyncio.run(main())
    assert "installed" in out
    gap = max(b - a for a, b in zip(ticks, ticks[1:]))
    assert gap < 0.3 * el, (gap, el)  # v0.93: one gap was a whole 16 MB scan, about half the call


def test_concurrent_same_package_installs_once(tmp_path):
    c = ctx(tmp_path, delay=0.2)

    async def main():
        return await asyncio.gather(*[agentkit.t_env_install(c, {"package": "requests"}) for _ in range(4)])
    outs = asyncio.run(main())
    installs = [x for x in c.core.runner.cmds if "pip install" in x]
    assert len(installs) == 1
    assert sum("already installed" in o for o in outs) == 3


def test_lock_released_after_install(tmp_path):
    c = ctx(tmp_path)
    assert "installed" in asyncio.run(agentkit.t_env_install(c, {"package": "requests"}))
    with open(tmp_path / "d" / ".install-pip.lock", "a+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
