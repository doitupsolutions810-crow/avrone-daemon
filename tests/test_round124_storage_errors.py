"""Round 124: storage failures in tool results name the file and say what ran out.

v0.116 (probe /workspace/work/fullfs.py, RLIMIT_FSIZE=1): write_file, artifact_save and
memory_save answered a bare "ERROR: OSError: [Errno 27] File too large". write() errors carry no
filename, so the agent was not told which file failed or that a retry could not help.
"""
from __future__ import annotations

import asyncio
import errno
import os

import pytest

from avrone import memstore, safeio, tools
from avrone.trinity import AgentContext
from tests.test_round121_readonly_storage import _core

CODES = [errno.ENOSPC, errno.EFBIG, errno.EDQUOT, errno.EROFS, errno.EIO]


class _Boom:
    def __init__(self, code):
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, data):
        raise OSError(self.code, os.strerror(self.code))


def _fail_writes(monkeypatch, code):
    real = os.fdopen

    def fdopen(fd, *a, **k):
        if a and "w" in a[0]:
            os.close(fd)
            return _Boom(code)
        return real(fd, *a, **k)
    monkeypatch.setattr(os, "fdopen", fdopen)


@pytest.mark.parametrize("writer", ["safeio", "tools"])
def test_write_errors_name_the_target(tmp_path, monkeypatch, writer):
    _fail_writes(monkeypatch, errno.EFBIG)
    fn = safeio.atomic_write if writer == "safeio" else tools.atomic_write_bytes
    with pytest.raises(OSError) as ei:
        fn(tmp_path / "f.txt", b"data")
    assert ei.value.errno == errno.EFBIG and ei.value.filename == str(tmp_path / "f.txt")
    assert os.listdir(tmp_path) == []  # temp file removed


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("tool,args,name", [
    ("write_file", {"path": "f.txt", "content": "x"}, "'f.txt'"),
    ("artifact_save", {"name": "a.txt", "content": "x"}, "a.txt"),
])
def test_file_tools_say_what_ran_out(tmp_path, monkeypatch, code, tool, args, name):
    core = _core(tmp_path)
    ctx = AgentContext(core=core, run_id="r", name="W-1", depth=0)
    _fail_writes(monkeypatch, code)
    out = asyncio.run(core.tools.call(ctx, tool, args))
    assert out.startswith("ERROR: could not write") and name in out, out
    assert errno.errorcode[code] in out and "[Errno" not in out and str(tmp_path) not in out


def test_memory_save_names_the_store(tmp_path, monkeypatch):
    core = _core(tmp_path)
    ctx = AgentContext(core=core, run_id="r", name="W-1", depth=0)

    def boom(self, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(memstore.MemoryStore, "save", boom)
    out = asyncio.run(core.tools.call(ctx, "memory_save", {"key": "k", "value": "v"}))
    assert out.startswith("ERROR: could not write") and "memory.jsonl" in out, out
    assert "disk is full" in out and str(tmp_path) not in out


def test_other_errors_unchanged():
    e = FileNotFoundError(errno.ENOENT, "No such file or directory", "x.txt")
    assert tools.error_text(e) .startswith("ERROR: FileNotFoundError: [Errno 2]")
    assert tools.error_text(ValueError("bad")) == "ERROR: ValueError: bad"


def test_with_target_leaves_named_errors():
    e = OSError(errno.ENOSPC, "full", "a")
    assert getattr(safeio, "with_target", lambda e, p: pytest.fail("no with_target"))(e, "b") is e
