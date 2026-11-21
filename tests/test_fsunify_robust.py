"""Round 41: fsunify against case-insensitive filesystems (macOS APFS, Windows NTFS), NTFS stream and
trailing-dot aliases of the agents/ root, an unusable agent dir, and unresolvable display paths."""
import os
from types import SimpleNamespace

import pytest

from avrone import agentfs, fsunify
from avrone.agentfs import AgentFSError
from tests.test_fsunify import call, env, events  # noqa: F401  (fixture)

ALIASES = ["Agents", "AGENTS", "aGeNtS", "agents.", "agents..", "agents ", "Agents. ", "agents::$INDEX_ALLOCATION",
           "agents:stream", "AGENTS:x:$DATA"]


@pytest.fixture()
def beta(env):
    d = agentfs.agent_dir(env.s.workspace, "beta")
    (d / "secret.txt").write_text("beta-private")
    return d


# ---------------------------------------------------------------- the agents/ root by any alias
@pytest.mark.parametrize("alias", ALIASES)
def test_shared_alias_of_agents_refused_by_resolve(env, alias):
    with pytest.raises(AgentFSError, match="off limits"):
        fsunify.resolve(env.ctx, f"shared/{alias}/beta/secret.txt", "read_file")


@pytest.mark.parametrize("alias", ALIASES)
def test_shared_alias_root_itself_refused(env, alias):
    with pytest.raises(AgentFSError):
        fsunify.resolve(env.ctx, f"shared/{alias}", "list_files")


@pytest.mark.parametrize("alias", ["Agents", "AGENTS", "agents."])
@pytest.mark.parametrize("tool,args", [("read_file", {}), ("write_file", {"content": "pwn"}),
                                       ("list_files", {})])
def test_tools_refuse_alias_and_audit(env, beta, alias, tool, args):
    path = f"shared/{alias}/beta/secret.txt" if tool != "list_files" else f"shared/{alias}/beta"
    out = call(env, tool, path=path, **args)
    assert "beta-private" not in out
    assert "off limits" in out or out.startswith("ERROR")
    assert (beta / "secret.txt").read_text() == "beta-private"
    assert any(e["data"]["tool"] == tool for e in events(env, "agentfs.denied"))


def test_backslash_alias_refused(env):
    with pytest.raises(AgentFSError):
        fsunify.resolve(env.ctx, "shared\\AGENTS\\beta\\x", "read_file")


def test_dot_segment_alias_refused(env):
    with pytest.raises(AgentFSError):
        fsunify.resolve(env.ctx, "shared/./Agents/beta/x", "read_file")


def test_symlink_named_in_other_case_refused(env, beta):
    # a link in the shared workspace whose target names the agents root in another case: realpath on a
    # case-insensitive volume keeps that spelling, so the exact-path check alone missed it
    link = env.ws / "lnk"
    os.symlink(str(env.ws / "AGENTS"), link)
    with pytest.raises(AgentFSError):
        fsunify.resolve(env.ctx, "shared/lnk/beta/secret.txt", "read_file")


def test_symlink_to_real_agents_still_refused(env, beta):
    os.symlink(str(env.ws / "agents"), env.ws / "lnk2")
    with pytest.raises(AgentFSError):
        fsunify.resolve(env.ctx, "shared/lnk2/beta/secret.txt", "read_file")


@pytest.mark.parametrize("name", ["agents-notes", "my-agents", "agentsx", "agent", "Agents_backup", "x/agents"])
def test_similar_names_still_allowed(env, name):
    r = fsunify.resolve(env.ctx, f"shared/{name}/f.txt", "write_file")
    assert r.root == env.ws and not r.in_agent


def test_own_dir_named_agents_subfolder_is_fine(env):
    r = fsunify.resolve(env.ctx, "Agents/notes.txt", "write_file")
    assert r.in_agent and r.path.is_relative_to(env.dir)


def test_fold_rules():
    f = fsunify._fold
    assert f("AGENTS") == f("agents.") == f("agents ") == f("agents::$INDEX_ALLOCATION") == "agents"
    assert f("agentsx") != "agents" and f(".agents") != "agents"


def test_touches_agents_outside_ws(env):
    assert fsunify._touches_agents(env.ws, env.tmp / "elsewhere") is False


def test_touches_agents_ws_itself(env):
    assert fsunify._touches_agents(env.ws, env.ws) is False


# ---------------------------------------------------------------- unusable agent dir
def test_agent_dir_is_a_file(env, tmp_path):
    f = tmp_path / "ws" / "agents" / "filey"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_dir": f})
    with pytest.raises(AgentFSError, match="not usable|not a directory"):
        fsunify.resolve(ctx, "a.txt", "write_file")
    assert any(e["data"]["reason"] for e in events(env, "agentfs.denied"))


def test_agent_dir_mkdir_oserror(env, monkeypatch, tmp_path):
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_dir": tmp_path / "ws" / "agents" / "ro"})

    def boom(self, *a, **k):
        raise PermissionError(13, "Permission denied", str(self))
    monkeypatch.setattr(fsunify.Path, "mkdir", boom)
    with pytest.raises(AgentFSError) as ei:
        fsunify.agent_root(ctx)
    assert str(tmp_path) not in str(ei.value)


def test_tool_reports_unusable_dir_as_plain_error(env, tmp_path):
    f = tmp_path / "ws" / "agents" / "filey2"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_dir": f})
    out = call(env, "write_file", ctx=ctx, path="a.txt", content="x")
    assert out.startswith("ERROR") and str(tmp_path) not in out


def test_agent_dir_symlink_loop(env, tmp_path):
    loop = tmp_path / "ws" / "agents" / "loop"
    os.symlink(str(loop), loop)
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_dir": loop})
    with pytest.raises(AgentFSError, match="symlink loop|cannot be resolved"):
        fsunify.agent_root(ctx)


def test_agent_fs_disabled_ignores_broken_dir(env, monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_AGENT_FS", "0")
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_dir": tmp_path / "nope" / "x"})
    r = fsunify.resolve(ctx, "q.txt", "write_file")
    assert r.unified is False


# ---------------------------------------------------------------- display never raises
def test_display_symlink_loop_returns_name(env):
    loop = env.ws / "loopy"
    os.symlink(str(loop), loop)
    out = fsunify.display(env.ctx, loop)
    assert out == "loopy"


def test_display_with_broken_agent_dir(env, tmp_path):
    f = tmp_path / "ws" / "agents" / "filey3"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_dir": f})
    assert fsunify.display(ctx, env.ws / "q.txt") == "q.txt"


# ---------------------------------------------------------------- parent/ stays read-only and confined
@pytest.fixture()
def child(env, beta):
    cdir = agentfs.agent_dir(env.s.workspace, "child")
    return SimpleNamespace(**{**vars(env.ctx), "agent_id": "child", "agent_dir": cdir, "parent_dir": beta})


@pytest.mark.parametrize("tool", ["write_file", "edit_file", "delete_file", "move_file", "anything"])
def test_parent_write_tools_refused(env, child, tool):
    with pytest.raises(AgentFSError, match="read-only"):
        fsunify.resolve(child, "parent/secret.txt", tool)


@pytest.mark.parametrize("raw", ["parent/../child/x", "parent//etc/passwd", "parent/~/x", "parent/C:/x"])
def test_parent_escapes_refused(env, child, raw):
    with pytest.raises(AgentFSError):
        fsunify.resolve(child, raw, "read_file")


def test_parent_read_ok(env, child):
    r = fsunify.resolve(child, "parent/secret.txt", "read_file")
    assert r.in_parent and r.path.read_text() == "beta-private"


def test_parent_prefix_case_variant_is_own_dir(env, child):
    r = fsunify.resolve(child, "PARENT/secret.txt", "write_file")
    assert r.in_agent and not r.in_parent


def test_shared_alias_from_child_cannot_reach_parent_for_write(env, child):
    with pytest.raises(AgentFSError):
        fsunify.resolve(child, "shared/Agents/beta/secret.txt", "write_file")
