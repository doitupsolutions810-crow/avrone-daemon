"""Round 119: a full disk (or quota, read-only remount, I/O error) under the CodeX logs no longer
kills runs.

v0.111: Codex._append let OSError(ENOSPC) out of emit(), so every chat, squad and deep run on
the node died with a raw "[Errno 28] No space left on device" (probe /workspace/work/enospc.py).
"""
from __future__ import annotations

import asyncio
import errno
import json
import os

import pytest

from avrone.codex import Codex


def _fail_writes(monkeypatch, under, code=errno.ENOSPC, partial=False, times=None):
    real = os.write
    state = {"n": 0}

    def w(fd, b):
        try:
            tgt = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            tgt = ""
        if tgt.startswith(str(under)) and (times is None or state["n"] < times):
            state["n"] += 1
            if partial and len(b) > 4:
                real(fd, bytes(b[:4]))  # a torn write: some bytes land, then the disk fills
            raise OSError(code, os.strerror(code))
        return real(fd, b)
    monkeypatch.setattr(os, "write", w)
    return state


needs_proc = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")


@needs_proc
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO, errno.EROFS, getattr(errno, "EDQUOT", errno.ENOSPC)])
def test_emit_survives_storage_errors(tmp_path, monkeypatch, code):
    cx = Codex(tmp_path / "d")
    _fail_writes(monkeypatch, tmp_path)
    ev = cx.emit("r1", "agent.step", "A", text="hello")
    assert ev.kind == "agent.step"
    h = cx.health()
    assert h["write_errors"] == 1 and h["emitted"] == 1 and h["last_write_error"]


@needs_proc
def test_subscribers_still_get_events(tmp_path, monkeypatch):
    cx = Codex(tmp_path / "d")
    q: asyncio.Queue = asyncio.Queue()
    sub = getattr(cx, "subscribe", None)
    if sub is None:
        pytest.skip("no subscribe API")
    try:
        q = sub()
    except TypeError:
        pytest.skip("subscribe signature differs")
    _fail_writes(monkeypatch, tmp_path)
    cx.emit("r1", "agent.step", "A", text="x")
    assert q.qsize() >= 1


@needs_proc
def test_torn_write_then_recovery_keeps_log_parseable(tmp_path, monkeypatch):
    cx = Codex(tmp_path / "d")
    cx.emit("r2", "a", "A", n=1)
    _fail_writes(monkeypatch, tmp_path, partial=True, times=1)
    cx.emit("r2", "b", "A", n=2)   # torn: 4 bytes land, then ENOSPC
    cx.emit("r2", "c", "A", n=3)   # disk has room again
    lines = (cx.log_dir / "r2.jsonl").read_text().splitlines()
    kinds = []
    for ln in lines:
        try:
            kinds.append(json.loads(ln)["kind"])
        except ValueError:
            pass
    assert kinds == ["a", "c"], lines


def test_other_errors_still_raise(tmp_path, monkeypatch):
    cx = Codex(tmp_path / "d")
    real_open = os.open

    def deny(path, *a, **k):
        if str(path).startswith(str(cx.log_dir)):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(PermissionError):
        cx.emit("r3", "a", "A")


@needs_proc
def test_runs_complete_on_full_disk(tmp_path, monkeypatch):
    from avrone.config import Settings
    from avrone.sovereign import LocalRunner
    from avrone.trinity import TrinityCore
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"; s.llm_mode = "mock"
    s.workspace.mkdir(parents=True)
    from avrone.llm import LLM
    core = TrinityCore(s, LLM(s), LocalRunner(s), Codex(s.data_dir))
    _fail_writes(monkeypatch, tmp_path / "d")
    r = asyncio.run(core.chat("hello"))
    assert r.get("answer")
    r = asyncio.run(core.squad("build x", n=2))
    assert r.get("answer")
    assert core.codex.health()["write_errors"] > 0
