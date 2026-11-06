"""v0.25 robustness: on-disk state that is torn or hand-edited must never stop a node from booting
or a listing from working. fleet.json corrupt -> boot, file moved aside; evolve list_accepted and
_finish_abnormally tolerate junk and write atomically."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import evolve
from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.delenv("AVRONE_NODE_ID", raising=False)


@pytest.mark.parametrize("body", [b"{torn", b"[1, 2]", b"\xff\xfe\x00", b'"just a string"'])
def test_corrupt_fleet_json_boots_fresh(tmp_path, body):
    (tmp_path / "fleet.json").write_bytes(body)
    fm = FleetManager(MagicMock(), tmp_path)
    assert fm.node_id and fm.nodes == {}
    assert list(tmp_path.glob("fleet.json.corrupt-*"))
    fm._save()
    assert json.loads((tmp_path / "fleet.json").read_text())["node_id"] == fm.node_id


def test_junk_fields_in_fleet_json(tmp_path):
    (tmp_path / "fleet.json").write_text(json.dumps({"node_id": 5, "nodes": {"good": {"url": "http://good:8080"}, "bad": 3}}))
    # round 76: a record whose url fails check_node_url ("u") is dropped at load, like register() refuses it
    fm = FleetManager(MagicMock(), tmp_path)
    assert fm.node_id != 5 and set(fm.nodes) == {"good"}
    (tmp_path / "fleet.json").write_text(json.dumps({"node_id": "keep-me", "nodes": ["x"]}))
    fm = FleetManager(MagicMock(), tmp_path)
    assert fm.node_id == "keep-me" and fm.nodes == {}


def test_list_accepted_tolerates_junk(tmp_path):
    d = tmp_path / "evolve"
    d.mkdir()
    (d / "ok.json").write_text(json.dumps({"pid": "ok", "passed": True, "goal": "g"}))
    (d / "list.json").write_text("[1]")
    (d / "nopid.json").write_text(json.dumps({"passed": True}))
    (d / "torn.json").write_text("{")
    out = evolve.list_accepted(SimpleNamespace(data_dir=str(tmp_path)))
    assert [r["pid"] for r in out] == ["ok"] and out[0]["branch"] is None


def _ctx(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r", name="E", depth=0)


@pytest.mark.parametrize("body", ["{torn", "[1]", json.dumps({"loop_id": "L1", "iterations": [1], "goal": "g"})])
def test_finish_abnormally_atomic_and_tolerant(tmp_path, body):
    ctx = _ctx(tmp_path)
    p = evolve._loop_path(ctx, "L1")
    p.write_text(body)
    evolve._finish_abnormally(ctx, "L1", "error", "error: x")
    m = json.loads(p.read_text())
    assert m["status"] == "error" and m["stopped"] == "error: x" and m["loop_id"] == "L1"
    assert not list(p.parent.glob(".*.tmp"))


def test_finish_abnormally_write_failure_is_swallowed(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    p = evolve._loop_path(ctx, "L2")
    monkeypatch.setattr(evolve.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    evolve._finish_abnormally(ctx, "L2", "cancelled", "cancelled")
    assert not p.exists() and not list(p.parent.glob(".*.tmp"))
