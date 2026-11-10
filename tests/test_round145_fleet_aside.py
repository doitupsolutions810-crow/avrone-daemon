"""Round 145: a corrupt fleet.json that can't be moved aside is never overwritten.

v0.137: FleetManager._load_saved ignored a failed rename of the corrupt file. The node started
with an empty registry and its first save replaced the file, so nothing of it was kept.
"""
from __future__ import annotations

import errno
import os
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "x" * 32)
    monkeypatch.setenv("AVRONE_NODE_ID", "n1")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


def _no_aside(monkeypatch):
    real = os.replace

    def deny(src, dst, *a, **k):
        if ".corrupt-" in str(dst) or ".toobig-" in str(dst):
            raise OSError(errno.EACCES, "Permission denied")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", deny)


def test_failed_aside_blocks_overwrite(tmp_path, monkeypatch):
    p = tmp_path / "fleet.json"
    p.write_text("{torn registry")
    _no_aside(monkeypatch)
    fm = fleet_mod.FleetManager(MagicMock(), tmp_path)  # boots: registry kept in memory
    fm._save()
    assert p.read_text() == "{torn registry"
    assert getattr(fm, "save_blocked", 0) >= 1


def test_save_sets_aside_once_rename_works(tmp_path, monkeypatch):
    p = tmp_path / "fleet.json"
    p.write_text("{torn registry")
    _no_aside(monkeypatch)
    fm = fleet_mod.FleetManager(MagicMock(), tmp_path)
    monkeypatch.undo()
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "x" * 32)
    fm._save()
    kept = [f for f in os.listdir(tmp_path) if f.startswith("fleet.json.corrupt-")]
    assert len(kept) == 1 and (tmp_path / kept[0]).read_text() == "{torn registry"
    assert '"node_id":"n1"' in p.read_text()
