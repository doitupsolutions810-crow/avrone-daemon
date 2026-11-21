"""Round 142: a checkpoint that exists but can't be read right now is not a free id.

v0.133: checkpoint._read returned None on any OSError. check_compatible took that as "no
checkpoint", so a different task (or owner) passed the check and then resumed the stored steps,
and fleet's stamp_owner rewrote the file as {"steps": {}, "owner": ...}, erasing every step.
"""
from __future__ import annotations

import errno
import json

import pytest

from avrone import checkpoint, safeio


def _seed(tmp_path, owner="fleet:a"):
    p = tmp_path / "ck.json"
    p.write_text(json.dumps({"version": checkpoint.VERSION, "owner": owner,
                             "steps": {"meta": {"mode": "deep", "task": "task A"}, "r0": "done"}}))
    return p


def _flaky(monkeypatch, p):
    real = safeio.read_capped

    def boom(path, cap):
        if str(path) == str(p):
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, cap)
    monkeypatch.setattr(safeio, "read_capped", boom)


def test_check_compatible_refuses_on_read_error(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    _flaky(monkeypatch, p)
    with pytest.raises(checkpoint.CheckpointConflict):
        checkpoint.check_compatible(p, "task B", "deep", owner="fleet:a")


def test_stamp_owner_keeps_steps_on_read_error(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    before = p.read_text()
    _flaky(monkeypatch, p)
    with pytest.raises(checkpoint.CheckpointConflict):
        checkpoint.stamp_owner(p, "fleet:a")
    monkeypatch.undo()
    assert p.read_text() == before


def test_missing_and_corrupt_still_fresh(tmp_path):
    assert checkpoint.check_compatible(tmp_path / "none.json", "t", "deep") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{torn")
    assert checkpoint.check_compatible(bad, "t", "deep") is None


def test_same_task_still_resumes(tmp_path):
    p = _seed(tmp_path)
    meta = checkpoint.check_compatible(p, "task A", "deep", owner="fleet:a")
    assert meta["task"] == "task A"
    checkpoint.stamp_owner(p, "fleet:a")
    assert json.loads(p.read_text())["steps"]["r0"] == "done"
