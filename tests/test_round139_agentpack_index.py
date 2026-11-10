"""Round 139: one failed read of the agent index no longer erases every agent's record.

v0.131: agentpack.record() read the index with load_index(), which returns {} on any failure.
A transient OSError (EMFILE under load, EIO, EACCES), a file over INDEX_MAX_BYTES, or junk JSON
made record() rewrite index.json with only the agent being recorded. Every other agent's home
record was erased, and a later import of those agents was refused with "belongs to another agent".
"""
from __future__ import annotations

import errno
import json
import os

import pytest

from avrone import agentpack, safeio


def _seed(tmp_path):
    agentpack.record(tmp_path, "a", home="node-a")
    agentpack.record(tmp_path, "b", home="node-b")
    return agentpack._index_path(tmp_path)


@pytest.mark.parametrize("code", [errno.EMFILE, errno.EIO, errno.EACCES])
def test_transient_read_error_does_not_wipe_index(tmp_path, monkeypatch, code):
    p = _seed(tmp_path)
    before = p.read_bytes()
    real = safeio.read_capped

    def boom(path, cap, *a, **k):
        if os.fspath(path) == os.fspath(p):
            raise OSError(code, os.strerror(code))
        return real(path, cap, *a, **k)

    monkeypatch.setattr(safeio, "read_capped", boom)
    with pytest.raises(agentpack.PackError):
        agentpack.record(tmp_path, "c", home="node-c")
    monkeypatch.undo()
    assert p.read_bytes() == before
    assert set(agentpack.load_index(tmp_path)) == {"a", "b"}


def test_junk_index_is_kept_aside(tmp_path):
    p = _seed(tmp_path)
    p.write_text('{"a": {"home": "node-a"}, "b": ')  # cut short by hand or by an old tool
    agentpack.record(tmp_path, "c", home="node-c")
    kept = [f for f in os.listdir(p.parent) if f.startswith("index.json.corrupt-")]
    assert len(kept) == 1
    assert (p.parent / kept[0]).read_text().startswith('{"a"')
    assert set(agentpack.load_index(tmp_path)) == {"c"}


def test_too_big_index_is_kept_aside(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(agentpack, "INDEX_MAX_BYTES", 10)
    agentpack.record(tmp_path, "c", home="node-c")
    kept = [f for f in os.listdir(p.parent) if f.startswith("index.json.corrupt-")]
    assert len(kept) == 1 and (p.parent / kept[0]).read_bytes() == before


def test_failed_save_leaves_no_temp(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    real = os.replace

    def full(src, dst, *a, **k):
        if os.fspath(dst) == os.fspath(p):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError):
        agentpack.record(tmp_path, "c", home="node-c")
    monkeypatch.undo()
    assert [f for f in os.listdir(p.parent) if f.endswith(".tmp")] == []
    assert set(json.loads(p.read_text())) == {"a", "b"}
