"""Round 121: a read-only (or wrongly owned) data volume.

v0.113 (probe /workspace/work/fullfs.py MODE=ro):
- every chat/squad died with PermissionError naming the host path of runs/<id>.jsonl
- memory_save answered "memory lock unusable: [Errno 13] ... '/srv/.../memory.jsonl.lock'"
- write_file / artifact_save errors named the random temp file (".f.txt.6u8bd1h0.tmp")
"""
from __future__ import annotations

import asyncio
import errno
import os
import stat

import pytest

from avrone import memstore, safeio, tools
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import AgentContext, TrinityCore

root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")


@pytest.fixture
def ro():
    made = []

    def lock(p):
        p.chmod(0o555)
        made.append(p)
        return p
    yield lock
    for p in made:
        p.chmod(0o755)


def _core(tmp_path):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.workspace.mkdir(parents=True)
    s.llm_mode = "mock"
    from avrone.llm import LLM
    core = TrinityCore(s, LLM(s), LocalRunner(s), Codex(s.data_dir))
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(core.tools)
    return core


@root
def test_runs_complete_with_read_only_log_dir(tmp_path, ro):
    core = _core(tmp_path)
    ro(core.codex.log_dir)
    assert asyncio.run(core.chat("hello")).get("answer")
    assert asyncio.run(core.squad("x", n=2)).get("answer")
    h = core.codex.health()
    assert h["write_errors"] > 0 and h["last_write_error"].startswith(("EACCES", "EPERM"))


@root
def test_memory_lock_error_has_no_host_path(tmp_path, ro):
    d = tmp_path / "d"
    d.mkdir()
    ro(d)
    with pytest.raises(memstore.MemoryRefused) as ei:
        memstore.MemoryStore(d / "memory.jsonl").save("ns", "k", "v")
    assert str(tmp_path) not in str(ei.value) and "memory.jsonl.lock" in str(ei.value)


@root
@pytest.mark.parametrize("writer", ["safeio", "tools"])
def test_atomic_write_errors_name_the_target(tmp_path, ro, writer):
    d = tmp_path / "w"
    d.mkdir()
    ro(d)
    target = d / "f.txt"
    fn = safeio.atomic_write if writer == "safeio" else tools.atomic_write_bytes
    with pytest.raises(PermissionError) as ei:
        fn(target, b"data")
    assert ei.value.filename == str(target), ei.value.filename
    assert not [p for p in os.listdir(d)]


@root
def test_write_file_tool_names_the_file(tmp_path, ro):
    core = _core(tmp_path)
    ro(core.settings.workspace)
    ctx = AgentContext(core=core, run_id="r", name="W-1", depth=0)
    out = asyncio.run(core.tools.call(ctx, "write_file", {"path": "f.txt", "content": "x"}))
    assert out.startswith("ERROR") and "'f.txt'" in out and ".tmp" not in out, out
    assert str(tmp_path) not in out


def test_mkstemp_beside_keeps_errno_class(tmp_path):
    with pytest.raises(FileNotFoundError) as ei:
        safeio.mkstemp_beside(tmp_path / "missing" / "f.txt")
    assert ei.value.errno == errno.ENOENT and ei.value.filename.endswith("f.txt")
