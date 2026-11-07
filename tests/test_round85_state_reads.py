"""Round 85: boot and tool reads of data_dir JSON never block, follow links or read unbounded.

Before: a FIFO at fleet.json blocked FleetManager() (node boot) forever in read_text(); a FIFO at
fleet_nonces.json blocked the loader and the gateway_view tool; a FIFO at agentpacks/index.json hung
every pack tool. fleet.json and the nonce snapshot were read whole at any size, and a symlinked
nonce journal was read through.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


def _bg(fn, timeout=5.0):
    out: dict = {}

    def go():
        try:
            out["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "blocked"
    return out


def _core(d):
    return SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(d)), active={})


def test_read_capped_semantics(tmp_path):
    from avrone import safeio
    if not hasattr(safeio, "read_capped"):
        pytest.fail("read_capped missing")
    p = tmp_path / "a.json"
    p.write_bytes(b"x" * 10)
    assert safeio.read_capped(p, 10) == b"x" * 10
    with pytest.raises(safeio.FileTooBig):
        safeio.read_capped(p, 9)
    assert safeio.read_capped(tmp_path / "missing", 5) is None
    os.mkfifo(tmp_path / "f")
    assert _bg(lambda: safeio.read_capped(tmp_path / "f", 5)).get("v") is None
    os.symlink(p, tmp_path / "l")
    assert safeio.read_capped(tmp_path / "l", 50) is None


def test_fifo_fleet_json_does_not_block_boot(tmp_path):
    from avrone.fleet import FleetManager
    os.mkfifo(tmp_path / "fleet.json")
    r = _bg(lambda: FleetManager(_core(tmp_path), data_dir=tmp_path))
    assert "e" not in r, r
    assert r["v"].nodes == {} or len(r["v"].nodes) == 0


def test_fifo_nonce_snapshot_does_not_block(tmp_path):
    from avrone.fleet import FleetManager
    os.mkfifo(tmp_path / "fleet_nonces.json")
    r = _bg(lambda: FleetManager(_core(tmp_path), data_dir=tmp_path))
    assert "e" not in r, r


def test_oversized_fleet_json_is_set_aside(tmp_path, monkeypatch):
    from avrone import fleet as fl
    from avrone.fleet import FleetManager
    if not hasattr(fl, "FLEET_JSON_MAX_BYTES"):
        pytest.fail("no cap")
    monkeypatch.setattr(fl, "FLEET_JSON_MAX_BYTES", 1000)
    (tmp_path / "fleet.json").write_text(json.dumps({"nodes": {}, "pad": "x" * 5000}))
    fm = FleetManager(_core(tmp_path), data_dir=tmp_path)
    assert len(fm.nodes) == 0
    assert any(p.name.startswith("fleet.json.corrupt-") for p in tmp_path.iterdir())


def test_symlinked_fleet_json_not_read_or_renamed(tmp_path):
    from avrone.fleet import FleetManager
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"nodes": {}}))
    os.symlink(elsewhere, tmp_path / "fleet.json")
    FleetManager(_core(tmp_path), data_dir=tmp_path)
    assert elsewhere.read_text() == json.dumps({"nodes": {}})
    assert not any(p.name.startswith("fleet.json.corrupt-") for p in tmp_path.iterdir())


def test_fifo_pack_index_does_not_hang(tmp_path):
    from avrone import agentpack
    d = agentpack.pack_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    os.mkfifo(d / "index.json")
    r = _bg(lambda: agentpack.load_index(tmp_path))
    assert r.get("v") == {}


def test_statehealth_check_json_special_and_big(tmp_path, monkeypatch):
    from avrone import tools_statehealth as sh
    os.mkfifo(tmp_path / "f.json")
    r = _bg(lambda: sh._check_json(tmp_path / "f.json"))
    assert "e" not in r
    big = tmp_path / "big.json"
    big.write_text("[" + "1," * 10 + "1]")
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 5)
    assert sh._check_json(big) is None
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100000 + "]" * 100000)
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 10 ** 7)
    assert sh._check_json(deep) == "invalid json"
