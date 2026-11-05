"""Round 67: symlink loops in agent and workspace paths.

Path.resolve() raises RuntimeError("Symlink loop from '<abs host path>'") on Python 3.12 (OSError
ELOOP on 3.13). confine(), is_agent_dir(), agent_dir() and tools._safe_path only caught
AgentFSError/ValueError/OSError, so an agent that planted `l1 -> l2 -> l1` got
`ERROR: RuntimeError: Symlink loop from '/srv/avrone/ws/agents/x/l1'` from read_file, write_file,
edit_file and image_inspect: the host's absolute workspace path leaked, the refusal was not audited
as agentfs.denied, and DockerRunner._agent_cwd raised a raw RuntimeError out of runner.run.
Now every one of them refuses with a plain "path has a symlink loop".
"""
from __future__ import annotations

import asyncio
import os

import pytest

from avrone import agentfs
from avrone.tools import _safe_path


@pytest.fixture
def loopy(tmp_path):
    ws = tmp_path / "ws"
    ad = agentfs.agent_dir(ws, "a")
    for d in (ad, ws):
        os.symlink("l2", d / "l1")
        os.symlink("l1", d / "l2")
    return ws, ad


@pytest.mark.parametrize("rel", ["l1", "l1/x", "l2/deep/er", "sub/../l1"])
def test_confine_refuses_loop_as_agentfs_error(loopy, rel):
    ws, ad = loopy
    with pytest.raises(agentfs.AgentFSError) as e:
        agentfs.confine(ad, rel)
    assert str(ws) not in str(e.value)


def test_confine_loop_message_is_plain(loopy):
    _, ad = loopy
    with pytest.raises(agentfs.AgentFSError, match="symlink loop"):
        agentfs.confine(ad, "l1/x")


def test_is_agent_dir_false_for_loop(loopy):
    ws, _ = loopy
    os.symlink("z2", agentfs.agents_root(ws) / "z1")
    os.symlink("z1", agentfs.agents_root(ws) / "z2")
    assert agentfs.is_agent_dir(ws, agentfs.agents_root(ws) / "z1") is False


def test_agent_dir_with_looping_root_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink("b", ws / "a")
    os.symlink("a", ws / "b")
    with pytest.raises(agentfs.AgentFSError):
        agentfs.agent_dir(ws / "a", "x", create=False)


@pytest.mark.parametrize("rel", ["l1", "l1/x", "/l2/x"])
def test_safe_path_loop_is_valueerror_without_host_path(loopy, rel):
    ws, _ = loopy
    with pytest.raises(ValueError) as e:
        _safe_path(ws, rel)
    assert not isinstance(e.value, RuntimeError) and str(ws) not in str(e.value)


def test_docker_runner_agent_cwd_loop_is_refused_cleanly(loopy):
    from types import SimpleNamespace
    from avrone.sovereign import DockerRunner
    ws, _ = loopy
    os.symlink("q2", agentfs.agents_root(ws) / "q1")
    os.symlink("q1", agentfs.agents_root(ws) / "q2")
    r = DockerRunner(SimpleNamespace(workspace=ws, docker_image="i", allow_network=False, command_timeout=5))
    with pytest.raises(agentfs.AgentFSError):
        r._agent_cwd(agentfs.agents_root(ws) / "q1")


def test_tools_return_plain_refusal(tmp_path, monkeypatch):
    from avrone.codex import Codex
    from avrone.config import Settings
    from avrone.sovereign import LocalRunner
    from avrone.tools_extra import register_extra_tools
    from avrone.trinity import AgentContext, TrinityCore

    class L:
        async def chat(self, m, **k):
            return '{"final":"ok"}'
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"
    core = TrinityCore(s, L(), LocalRunner(s), Codex(s.data_dir))
    register_extra_tools(core.tools)
    ws = s.workspace
    ad = agentfs.agent_dir(ws, "a")
    for d in (ad, ws):
        os.symlink("l2", d / "l1")
        os.symlink("l1", d / "l2")
    bad = []
    for agent_dir in (ad, None):
        ctx = AgentContext(core=core, run_id="r", name="A", depth=0)
        ctx.agent_dir = agent_dir
        for tool, args in (("read_file", {"path": "l1/x"}), ("write_file", {"path": "l1/x", "content": "c"}),
                           ("edit_file", {"path": "l1", "old": "a", "new": "b"}),
                           ("read_file", {"path": "shared/l1/x"}), ("write_file", {"path": "shared/l1/x", "content": "c"})):
            out = asyncio.run(core.tools.call(ctx, tool, args))
            if "RuntimeError" in out or str(ws.resolve()) in out or str(ws) in out:
                bad.append((tool, agent_dir is not None, out[:150]))
    assert not bad, bad
