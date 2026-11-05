"""Round 69: every fleet heartbeat rewrote the whole registry on the event loop.

FleetManager.register() runs for every heartbeat of every child (default every 15 s) and ended in
_save(): json.dumps(indent=2) of every node record + write + replace, called synchronously from
the async /api/fleet/register handler. At 256 peers x 60 KB records (inside the documented bounds:
DEFAULT_MAX_NODES, MAX_NODE_RECORD_BYTES) that is 145 ms per heartbeat, so 256 peers kept the loop
busy ~2.5 s of every second. Now a heartbeat refresh (known node, same url) saves at most once per
FLEET_SAVE_DEBOUNCE_S; new nodes, url changes and forget() still save at once, stop() flushes,
and the handler runs register() off the loop.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import avrone.fleet as fleet_mod
from avrone.fleet import FleetManager


def _fm(d: Path) -> FleetManager:
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(d)))
    return FleetManager(core, data_dir=d)


@pytest.fixture
def d(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "r69-" + "k" * 40)
    monkeypatch.delenv("AVRONE_FLEET_ALLOW", raising=False)
    monkeypatch.setenv("AVRONE_FLEET_MAX_NODES", "256")
    return tmp_path


def _reg(fm, i, url=None, **extra):
    return fm.register({"node_id": f"n{i}", "url": url or f"http://10.0.0.{i % 250 + 1}:8000", **extra})


def _count_saves(fm, monkeypatch):
    n = {"saves": 0}
    real = fm._save

    def counting():
        n["saves"] += 1
        real()
    monkeypatch.setattr(fm, "_save", counting)
    return n


def test_heartbeats_do_not_rewrite_the_registry_each_time(d, monkeypatch):
    fm = _fm(d)
    for i in range(20):
        _reg(fm, i)
    n = _count_saves(fm, monkeypatch)
    for _ in range(10):
        for i in range(20):
            _reg(fm, i, load=3)
    assert n["saves"] <= 1, n  # was 200


def test_heartbeat_cost_at_documented_bounds(d):
    fm = _fm(d)
    big = {f"k{i}": "v" * 50 for i in range(900)}  # ~60 KB, under MAX_NODE_RECORD_BYTES
    real = fm._save
    fm._save = lambda: None  # quick setup: one save at the end instead of 256
    for i in range(256):
        _reg(fm, i, capabilities=big)
    fm._save = real
    fm._save()
    t0 = time.perf_counter()
    for i in range(40):
        _reg(fm, i, capabilities=big)
    per = (time.perf_counter() - t0) / 40
    assert per < 0.03, f"{per * 1000:.0f} ms per heartbeat"  # was ~145 ms


def test_new_node_and_url_change_save_immediately(d, monkeypatch):
    fm = _fm(d)
    _reg(fm, 1)
    n = _count_saves(fm, monkeypatch)
    _reg(fm, 2)
    assert n["saves"] == 1 and "n2" in json.loads((d / "fleet.json").read_text())["nodes"]
    fm.nodes["n1"]["last_seen"] = 0  # offline, so a move to a new url is allowed
    _reg(fm, 1, url="http://10.9.9.9:8000")
    assert n["saves"] == 2
    assert json.loads((d / "fleet.json").read_text())["nodes"]["n1"]["url"] == "http://10.9.9.9:8000"


def test_refresh_after_window_is_saved(d, monkeypatch):
    fm = _fm(d)
    fm._saved_at = time.monotonic() - 60
    _reg(fm, 1)
    _reg(fm, 1, load=7)  # debounced
    assert json.loads((d / "fleet.json").read_text())["nodes"]["n1"]["load"] != 7
    monkeypatch.setattr(fleet_mod, "FLEET_SAVE_DEBOUNCE_S", 0.0)
    _reg(fm, 1, load=8)
    assert json.loads((d / "fleet.json").read_text())["nodes"]["n1"]["load"] == 8


def test_stop_flushes_pending_refresh(d):
    fm = _fm(d)
    _reg(fm, 1)
    _reg(fm, 1, load=9)
    asyncio.run(fm.stop())
    assert json.loads((d / "fleet.json").read_text())["nodes"]["n1"]["load"] == 9
    assert _fm(d).nodes["n1"]["load"] == 9


def test_forget_still_saves_at_once(d):
    fm = _fm(d)
    fm._saved_at = time.monotonic() - 60
    _reg(fm, 1)
    _reg(fm, 1, load=2)
    fm.nodes["n1"]["last_seen"] = 0
    fm.forget("n1")
    assert "n1" not in json.loads((d / "fleet.json").read_text())["nodes"]


def test_register_endpoint_runs_off_the_loop():
    import inspect
    from avrone import server
    src = inspect.getsource(server.fleet_register)
    assert "to_thread" in src
