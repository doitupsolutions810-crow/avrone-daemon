"""Round 140: a checkpoint save never overwrites steps it could not read.

v0.132: _Store._merge_disk returned quietly on every failure, and the save then wrote only this
store's keys. With a load that had also failed (EMFILE, EIO, EACCES), one transient read error
erased every finished step of the run, and a resume re-ran them. A file that grew past the cap,
or another process's file that no longer parsed, was overwritten the same way.
"""
from __future__ import annotations

import errno
import json
import os

from avrone import checkpoint, safeio


def _seed(path):
    cp = checkpoint.Checkpoint(path)
    for i in range(5):
        assert cp.put(f"step{i}", {"done": i})
    return json.loads(path.read_text())["steps"]


def _fail_reads(monkeypatch, path, code=errno.EIO):
    real = safeio.read_capped

    def boom(p, cap, *a, **k):
        if os.fspath(p) == os.fspath(path):
            raise OSError(code, os.strerror(code))
        return real(p, cap, *a, **k)

    monkeypatch.setattr(safeio, "read_capped", boom)


def test_transient_read_error_keeps_steps(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    before = _seed(path)
    _fail_reads(monkeypatch, path)
    cp = checkpoint.Checkpoint(path)  # load fails, store starts empty
    assert cp.put("late", 1) is False
    monkeypatch.undo()
    steps = json.loads(path.read_text())["steps"]
    assert {k: steps[k] for k in before} == before
    assert checkpoint.Checkpoint(path).put("late", 1) is True  # recovers once reads work
    assert set(json.loads(path.read_text())["steps"]) == set(before) | {"late"}


def test_emfile_keeps_steps(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    before = _seed(path)
    _fail_reads(monkeypatch, path, errno.EMFILE)
    cp = checkpoint.Checkpoint(path)
    cp.put("x", 1)
    monkeypatch.undo()
    assert set(json.loads(path.read_text())["steps"]) >= set(before)


def test_file_corrupted_by_another_writer_is_set_aside(tmp_path):
    path = tmp_path / "run.json"
    _seed(path)
    cp = checkpoint.Checkpoint(path)
    path.write_text('{"version": 1, "steps": {"other": ')  # torn by a non-atomic writer
    assert cp.put("mine", 1)
    kept = [f for f in os.listdir(tmp_path) if f.startswith("run.json.corrupt-")]
    assert len(kept) == 1
