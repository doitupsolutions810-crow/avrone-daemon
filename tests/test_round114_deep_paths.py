"""Round 114: paths past PATH_MAX and tool errors that named the host path.

v0.106: an all-tools fuzz over a 1,900-level directory tree and >4096-byte paths
(/workspace/work/deepfuzz.py, SEED 11401: 2,075 calls, no hang or RecursionError) found
edit_file and artifact_save returning the raw `OSError: [Errno 36] File name too long:
'/tmp/.../ws/d/d/d/...'`: about 4 KB of output that also gave away the node's host workspace
location. The registry's error wrapper now names paths relative to the workspace / data dir,
clips long ones, and says plainly when a path is too long.
"""
from __future__ import annotations

import asyncio
import errno
import os

import pytest

from avrone import tools
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import AgentContext, TrinityCore


class _LLM:
    async def chat(self, messages, **kw):
        return '{"final": "ok"}'


def _core(tmp_path):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"
    s.workspace.mkdir(parents=True, exist_ok=True)
    core = TrinityCore(s, _LLM(), LocalRunner(s), Codex(s.data_dir))
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(core.tools)
    return core


def _call(core, name, args):
    ctx = AgentContext(core=core, run_id="r114", name="T", depth=0)
    return asyncio.run(asyncio.wait_for(core.tools.call(ctx, name, args), 20))


LONG_PATH = "/".join(["d"] * 2100) + "/x.txt"  # 4,206 chars: over PATH_MAX


@pytest.mark.parametrize("name,args", [
    ("edit_file", {"path": LONG_PATH, "old": "a", "new": "b"}),
    ("artifact_save", {"name": LONG_PATH, "content": "hi"}),
    ("write_file", {"path": LONG_PATH, "content": "hi"}),
    ("read_file", {"path": LONG_PATH}),
])
def test_over_path_max_is_plain_and_hides_host_path(tmp_path, name, args):
    core = _core(tmp_path)
    if core.tools.get(name) is None:
        pytest.skip(f"{name} not registered")
    out = _call(core, name, args)
    assert out.startswith("ERROR") or "too long" in out.lower() or "not found" in out.lower(), out[:200]
    assert "Errno 36" not in out, out[:200]
    assert str(tmp_path) not in out, "the node's host workspace path leaked into the tool output"
    assert len(out) < 1200, len(out)


def _boom_registry(exc):
    reg = tools.ToolRegistry()

    async def boom(ctx, args):
        raise exc
    reg.register(tools.Tool(name="boom", description="d", args_doc="{}", fn=boom))
    return reg


def test_registry_oserror_path_relative_to_workspace(tmp_path):
    core = _core(tmp_path)
    reg = _boom_registry(PermissionError(errno.EACCES, "Permission denied", str(tmp_path / "ws" / "sub" / "f.txt")))
    ctx = AgentContext(core=core, run_id="r", name="T", depth=0)
    out = asyncio.run(reg.call(ctx, "boom", {}))
    assert out.startswith("ERROR: PermissionError: [Errno 13] Permission denied"), out
    assert "sub/f.txt" in out and str(tmp_path) not in out, out


def test_registry_oserror_outside_workspace_shows_basename(tmp_path):
    core = _core(tmp_path)
    reg = _boom_registry(FileNotFoundError(errno.ENOENT, "No such file or directory", "/opt/secret/place/x.cfg"))
    ctx = AgentContext(core=core, run_id="r", name="T", depth=0)
    out = asyncio.run(reg.call(ctx, "boom", {}))
    assert "x.cfg" in out and "/opt/secret" not in out, out


def test_registry_rename_error_names_both(tmp_path):
    core = _core(tmp_path)
    ws = tmp_path / "ws"
    reg = _boom_registry(OSError(errno.EXDEV, "Invalid cross-device link", str(ws / "a"), None, str(ws / "b")))
    ctx = AgentContext(core=core, run_id="r", name="T", depth=0)
    out = asyncio.run(reg.call(ctx, "boom", {}))
    assert "'a -> b'" in out, out


def test_registry_non_oserror_unchanged(tmp_path):
    core = _core(tmp_path)
    reg = _boom_registry(ValueError("plain"))
    ctx = AgentContext(core=core, run_id="r", name="T", depth=0)
    assert asyncio.run(reg.call(ctx, "boom", {})) == "ERROR: ValueError: plain"


def test_deep_tree_walkers_no_recursion(tmp_path):
    core = _core(tmp_path)
    fd = os.open(core.settings.workspace, os.O_RDONLY)
    for _ in range(1500):
        os.mkdir("d", dir_fd=fd)
        nfd = os.open("d", os.O_RDONLY, dir_fd=fd); os.close(fd); fd = nfd
    lf = os.open("leaf.txt", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd); os.write(lf, b"needle\n")
    os.close(lf); os.close(fd)
    try:
        for name, args in [("list_files", {"path": ".", "depth": 10 ** 6}), ("search_files", {"pattern": "needle"})]:
            if core.tools.get(name) is None:
                continue
            out = _call(core, name, args)
            assert "RecursionError" not in out and "Errno" not in out, (name, out[:200])
    finally:
        _rm_deep(core.settings.workspace)


def _rm_deep(root):
    """pytest's tmp cleanup recurses and gives up on a 1,500-level tree (2,000 warnings): peel
    the top level off at a time, with short paths and no held fds."""
    top, tmp = os.path.join(root, "d"), os.path.join(root, "_t")
    while os.path.isdir(top):
        child = os.path.join(top, "d")
        if os.path.isdir(child):
            os.rename(child, tmp)
        for f in os.listdir(top):
            os.unlink(os.path.join(top, f))
        os.rmdir(top)
        if os.path.isdir(tmp):
            os.rename(tmp, top)
