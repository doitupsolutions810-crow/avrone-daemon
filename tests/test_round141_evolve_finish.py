"""Round 141: ending a loop abnormally never wipes the loop file it could not read.

v0.133: evolve._finish_abnormally replaced the loop file with {"loop_id", "iterations": []} on any
read failure. A transient EMFILE, EIO or EACCES dropped every iteration and the resume checkpoint,
so evolve_resume had nothing to resume. A torn file was overwritten with no copy kept.
"""
from __future__ import annotations

import errno
import json
import os
from types import SimpleNamespace as S

from avrone import evolve


def _ctx(tmp_path):
    return S(core=S(settings=S(data_dir=tmp_path), codex=None), run_id="r", name="Lead")


def _seed(ctx):
    p = evolve._loop_path(ctx, "loop1")
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = {"loop_id": "loop1", "status": "running", "iterations": [{"i": 0}, {"i": 1}],
            "checkpoint": {"candidates": [[{"path": "a", "old": "x", "new": "y"}]]}}
    p.write_text(json.dumps(meta))
    return p, meta


def test_read_error_keeps_iterations(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    p, meta = _seed(ctx)

    def boom(path, *a, **k):
        if os.fspath(path) == os.fspath(p):
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, *a, **k)

    real = evolve._read_state
    monkeypatch.setattr(evolve, "_read_state", boom)
    evolve._finish_abnormally(ctx, "loop1", "failed", "error")
    monkeypatch.undo()
    assert json.loads(p.read_text()) == meta


def test_torn_file_is_kept_aside(tmp_path):
    ctx = _ctx(tmp_path)
    p, _ = _seed(ctx)
    p.write_text('{"loop_id": "loop1", "iterations": [{"i": 0}, ')
    evolve._finish_abnormally(ctx, "loop1", "failed", "error")
    kept = [f for f in os.listdir(p.parent) if f.startswith(p.name + ".corrupt-")]
    assert len(kept) == 1
    assert json.loads(p.read_text())["status"] == "failed"


def test_normal_finish_still_marks_status(tmp_path):
    ctx = _ctx(tmp_path)
    p, _ = _seed(ctx)
    evolve._finish_abnormally(ctx, "loop1", "cancelled", "cancel")
    m = json.loads(p.read_text())
    assert m["status"] == "cancelled" and len(m["iterations"]) == 2
