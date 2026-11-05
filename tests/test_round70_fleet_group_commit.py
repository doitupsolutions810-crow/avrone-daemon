"""Round 70: a fleet join storm rewrote the whole growing registry once per joining peer.

Every new node saves fleet.json at once (membership is durable), and each save serialized every
record with indent=2 while holding the state lock, so N concurrent joins meant N full rewrites
one after another: 256 peers x 60 KB took ~18 s, with each registration waiting on all earlier
ones. Now _save() is a group commit: concurrent savers wait for one leader, which writes the newest
snapshot until it covers them all, compact JSON is ~3x faster to encode, and register()/forget()
save outside the state lock. A register() still returns only once fleet.json holds its node.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.fleet import FleetManager

BIG = {f"k{i}": "v" * 50 for i in range(900)}  # ~60 KB record


def _fm(d: Path) -> FleetManager:
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(d)))
    return FleetManager(core, data_dir=d)


@pytest.fixture
def d(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "r70-" + "j" * 40)
    monkeypatch.delenv("AVRONE_FLEET_ALLOW", raising=False)
    monkeypatch.setenv("AVRONE_FLEET_MAX_NODES", "256")
    return tmp_path


def _join(fm, i, check=True):
    fm.register({"node_id": f"n{i}", "url": f"http://10.0.{i // 250}.{i % 250 + 1}:8000", "capabilities": BIG})
    if not check:
        return True
    return json.loads(fm.path.read_text())["nodes"].get(f"n{i}") is not None  # durable on return


def test_concurrent_join_storm_coalesces_and_is_durable(d, monkeypatch):
    fm = _fm(d)
    writes = {"n": 0}
    real = fm._write_registry

    def counting(text):
        writes["n"] += 1
        real(text)
    monkeypatch.setattr(fm, "_write_registry", counting)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(32) as ex:
        ok = list(ex.map(lambda i: _join(fm, i, check=i % 32 == 0), range(256)))
    took = time.perf_counter() - t0
    assert all(ok)  # every register() returned only after its node was on disk
    assert len(json.loads(fm.path.read_text())["nodes"]) == 256
    assert writes["n"] < 128, writes  # was 256 full rewrites
    assert took < 6, f"{took:.1f} s"  # was ~18 s (0.7 s now)


def test_compact_encoding_and_reload(d):
    fm = _fm(d)
    for i in range(3):
        _join(fm, i)
    raw = fm.path.read_text()
    assert "\n" not in raw  # compact (indent=2 was 3x slower to encode at 16 MB)
    assert set(_fm(d).nodes) == {"n0", "n1", "n2"}


def test_failed_leader_write_hands_off_to_a_waiter(d, monkeypatch):
    fm = _fm(d)
    real = fm._write_registry
    state = {"fail": 1}
    gate = threading.Event()

    def flaky(text):
        if state["fail"]:
            state["fail"] -= 1
            gate.wait(2)  # let other savers queue up behind this leader
            raise OSError("disk hiccup")
        real(text)
    monkeypatch.setattr(fm, "_write_registry", flaky)
    errs = []

    def join(i):
        try:
            return _join(fm, i)
        except OSError as e:
            errs.append(e)
            return None
    with ThreadPoolExecutor(8) as ex:
        futs = [ex.submit(join, i) for i in range(8)]
        time.sleep(0.2)
        gate.set()
        res = [f.result(10) for f in futs]
    assert len(errs) == 1  # only the failed leader's caller sees the error
    assert sum(r is True for r in res) == 7
    assert fm._save_busy is False
    _join(fm, 99)  # later saves still work
    assert "n99" in json.loads(fm.path.read_text())["nodes"]


def test_save_called_while_holding_state_lock_does_not_deadlock(d):
    fm = _fm(d)
    done = []

    def other():
        for i in range(20):
            _join(fm, 100 + i)
        done.append(1)
    t = threading.Thread(target=other)
    with fm._lock:
        t.start()
        for i in range(5):
            fm.nodes = {**fm.nodes, f"x{i}": {"node_id": f"x{i}", "url": "http://10.1.1.1:1", "last_seen": 0}}
            fm._save()
    t.join(20)
    assert done and not t.is_alive()
    disk = json.loads(fm.path.read_text())["nodes"]
    assert {"x0", "x4", "n119"} <= set(disk)


def test_forget_is_durable_on_return(d):
    fm = _fm(d)
    _join(fm, 1)
    fm.nodes["n1"]["last_seen"] = 0
    fm.forget("n1")
    assert "n1" not in json.loads(fm.path.read_text())["nodes"]
