"""Round 143: a transient read error on fleet.json never gives the node a new identity.

v0.135: FleetManager._load_saved treated any OSError as a corrupt file. The file was moved aside
and the node booted with a fresh random node_id and no peers, so a single EMFILE/EIO/EACCES at
boot turned it into a different node (fleet checkpoints and peers are keyed by that id).
"""
from __future__ import annotations

import errno
import json
import os
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod, safeio


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "x" * 32)
    monkeypatch.delenv("AVRONE_NODE_ID", raising=False)


def _seed(tmp_path):
    p = tmp_path / "fleet.json"
    p.write_text(json.dumps({"node_id": "keep-me-1", "nodes": {}}))
    return p


def test_transient_error_keeps_identity(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    real, calls = safeio.read_capped, []

    def flaky(path, cap):
        if str(path) == str(p) and not calls:
            calls.append(1)
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, cap)
    monkeypatch.setattr(safeio, "read_capped", flaky)
    fm = fleet_mod.FleetManager(MagicMock(), tmp_path)
    assert fm.node_id == "keep-me-1"
    assert not [f for f in os.listdir(tmp_path) if ".corrupt-" in f]


def test_persistent_error_refuses_to_boot(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    real = safeio.read_capped

    def boom(path, cap):
        if str(path) == str(p):
            raise OSError(errno.EIO, "Input/output error")
        return real(path, cap)
    monkeypatch.setattr(safeio, "read_capped", boom)
    with pytest.raises(RuntimeError, match="node identity"):
        fleet_mod.FleetManager(MagicMock(), tmp_path)
    monkeypatch.undo()
    assert json.loads(p.read_text())["node_id"] == "keep-me-1"


def test_corrupt_still_set_aside(tmp_path):
    (tmp_path / "fleet.json").write_text("{torn")
    fm = fleet_mod.FleetManager(MagicMock(), tmp_path)
    assert fm.node_id != "keep-me-1"
    assert [f for f in os.listdir(tmp_path) if f.startswith("fleet.json.corrupt-")]


def test_oversized_set_aside_as_toobig(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(fleet_mod, "FLEET_JSON_MAX_BYTES", 8)
    fleet_mod.FleetManager(MagicMock(), tmp_path)
    assert [f for f in os.listdir(tmp_path) if f.startswith("fleet.json.toobig-")]
