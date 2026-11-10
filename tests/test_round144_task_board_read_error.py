"""Round 144: a task board that can't be read right now is never set aside or replaced.

v0.136: any OSError reading the board counted as corrupt. The board was moved aside as
*.corrupt-<ts> and an add wrote a board holding only the new task, so every agent lost the
run's tasks. If the set-aside rename itself failed, the write replaced the board anyway.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
from types import SimpleNamespace as S

from avrone import agentkit, safeio


def _ctx(tmp, run_id="run1", name="A"):
    return S(run_id=run_id, name=name,
             core=S(settings=S(data_dir=tmp / "d"), codex=S(emit=lambda *a, **k: None)))


def _add(c, title):
    return asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": title}))


def test_transient_read_error_changes_nothing(tmp_path, monkeypatch):
    c = _ctx(tmp_path)
    _add(c, "one")
    _add(c, "two")
    p = agentkit._board(c)
    before = p.read_text()
    real = safeio.read_capped

    def boom(path, cap):
        if str(path) == str(p):
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, cap)
    monkeypatch.setattr(safeio, "read_capped", boom)
    out = _add(c, "three")
    monkeypatch.undo()
    assert out.startswith("ERROR")
    assert p.read_text() == before
    assert not [f for f in os.listdir(p.parent) if ".corrupt-" in f]


def test_failed_set_aside_does_not_overwrite(tmp_path, monkeypatch):
    c = _ctx(tmp_path)
    _add(c, "one")
    p = agentkit._board(c)
    p.write_text("[{torn")
    real = os.replace

    def no_aside(src, dst, *a, **k):
        if ".corrupt-" in str(dst):
            raise OSError(errno.EACCES, "Permission denied")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", no_aside)
    out = _add(c, "two")
    monkeypatch.undo()
    assert out.startswith("ERROR")
    assert p.read_text() == "[{torn"


def test_corrupt_board_still_set_aside(tmp_path):
    c = _ctx(tmp_path)
    _add(c, "one")
    p = agentkit._board(c)
    p.write_text("[{torn")
    assert "#1" in _add(c, "two")
    assert [f for f in os.listdir(p.parent) if ".corrupt-" in f]
    assert [t["title"] for t in json.loads(p.read_text())] == ["two"]
