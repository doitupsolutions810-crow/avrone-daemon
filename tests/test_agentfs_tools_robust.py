"""Round 59: agent fs tools edge cases and argnum.tool_bool."""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path

import pytest

from avrone import agentfs_tools as A
from avrone.agentfs import AgentFSError
from avrone.argnum import tool_bool


class _Codex:
    def __init__(self):
        self.events = []

    def emit(self, run_id, kind, name, **data):
        self.events.append((kind, data))


class _Core:
    def __init__(self):
        self.codex = _Codex()


class _Ctx:
    def __init__(self, d: Path):
        self.core = _Core()
        self.run_id = "r"
        self.name = "n"
        self.agent_id = "a1"
        self.agent_dir = str(d)


@pytest.fixture
def ctx(tmp_path):
    return _Ctx(tmp_path / "ag")


def run(fn, ctx, **a):
    return asyncio.run(fn(ctx, a))


def root(ctx) -> Path:
    return Path(ctx.agent_dir).resolve()


# ---- tool_bool ----------------------------------------------------------------------------------

@pytest.mark.parametrize("v", [True, 1, -3, 2.5, "true", "TRUE", " yes ", "y", "on", "1", "t", float("inf")])
def test_tool_bool_true(v):
    assert tool_bool(v, False) is True


@pytest.mark.parametrize("v", [False, 0, 0.0, -0.0, "false", "False", " NO ", "n", "off", "0", "", "  ", "none",
                               "null", "f"])
def test_tool_bool_false(v):
    assert tool_bool(v, True) is False


@pytest.mark.parametrize("v", [None, float("nan"), "maybe", "tru", ["x"], {"a": 1}, object(), b"true", "2", "yes!"])
@pytest.mark.parametrize("default", [True, False])
def test_tool_bool_default(v, default):
    assert tool_bool(v, default) is default


def test_tool_bool_returns_real_bool():
    for v in (1, 5, "yes", 1.0):
        assert type(tool_bool(v)) is bool


# ---- string booleans: "false" never means yes ---------------------------------------------------

def test_append_false_string_overwrites(ctx):
    run(A.t_fs_write, ctx, path="a.txt", content="x")
    run(A.t_fs_write, ctx, path="a.txt", content="y", append="false")
    assert (root(ctx) / "a.txt").read_text() == "y"
    run(A.t_fs_write, ctx, path="a.txt", content="z", append="true")
    assert (root(ctx) / "a.txt").read_text() == "yz"


@pytest.mark.parametrize("rec", ["false", "no", "0", 0, None, "maybe", []])
def test_delete_recursive_falsy_keeps_nonempty_dir(ctx, rec):
    run(A.t_fs_write, ctx, path="d/x", content="1")
    out = run(A.t_fs_delete, ctx, path="d", recursive=rec)
    assert "non-empty directory" in out
    assert (root(ctx) / "d" / "x").exists()


@pytest.mark.parametrize("rec", [True, "true", "yes", 1])
def test_delete_recursive_truthy_deletes(ctx, rec):
    run(A.t_fs_write, ctx, path="d/x", content="1")
    out = run(A.t_fs_delete, ctx, path="d", recursive=rec)
    assert out.startswith("deleted d")
    assert not (root(ctx) / "d").exists()


def test_list_recursive_false_string(ctx):
    run(A.t_fs_write, ctx, path="d/inner.txt", content="1")
    out = run(A.t_fs_list, ctx, recursive="false")
    assert "d/" in out and "inner.txt" not in out
    out = run(A.t_fs_list, ctx, recursive="true")
    assert "d/inner.txt" in out


def test_fleet_tools_use_tool_bool():
    src = Path(__file__).resolve().parents[1].joinpath("avrone", "fleet.py").read_text()
    assert 'bool(args.get("live_only"))' not in src and 'bool(args.get("wait"))' not in src


def test_no_bare_bool_tool_args_in_agentfs_tools():
    src = Path(A.__file__).read_text()
    assert "bool(args.get(" not in src.replace("tool_bool(args.get(", "")


# ---- content ------------------------------------------------------------------------------------

def test_lone_surrogate_written_as_replacement(ctx):
    out = run(A.t_fs_write, ctx, path="s.txt", content="a\ud83db\udc00")
    assert out.startswith("wrote s.txt")
    assert (root(ctx) / "s.txt").read_bytes() == "a\ufffdb\ufffd".encode()


def test_valid_astral_kept(ctx):
    run(A.t_fs_write, ctx, path="e.txt", content="😀")
    assert (root(ctx) / "e.txt").read_text() == "😀"


def test_none_content_is_empty(ctx):
    out = run(A.t_fs_write, ctx, path="q", content=None)
    assert "(0 bytes" in out and (root(ctx) / "q").read_bytes() == b""


def test_missing_content_is_empty(ctx):
    run(A.t_fs_write, ctx, path="q")
    assert (root(ctx) / "q").read_bytes() == b""


def test_number_content_written_as_text(ctx):
    run(A.t_fs_write, ctx, path="n", content=12)
    assert (root(ctx) / "n").read_text() == "12"


# ---- path arguments -----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [["x"], {"a": 1}, 7, 1.5, b"x", True])
@pytest.mark.parametrize("fn", [A.t_fs_write, A.t_fs_read, A.t_fs_list, A.t_fs_delete])
def test_non_string_path_refused(ctx, bad, fn):
    with pytest.raises(AgentFSError, match="path must be a string"):
        run(fn, ctx, path=bad, content="z")
    assert not any(p.name.startswith("[") or p.name.startswith("{") for p in root(ctx).iterdir()) \
        if root(ctx).exists() else True
    assert any(k == "agentfs.denied" for k, _ in ctx.core.codex.events)


def test_none_path_lists_root(ctx):
    out = run(A.t_fs_list, ctx, path=None)
    assert out.startswith("[agent dir a1:")


# ---- OS errors never show the host layout --------------------------------------------------------

def test_parent_is_a_file(ctx):
    run(A.t_fs_write, ctx, path="a.txt", content="x")
    with pytest.raises(AgentFSError) as ei:
        run(A.t_fs_write, ctx, path="a.txt/b", content="z")
    msg = str(ei.value)
    assert str(root(ctx)) not in msg and "a.txt" in msg and "not a directory" in msg
    assert any(k == "agentfs.error" for k, _ in ctx.core.codex.events)


def test_name_too_long(ctx):
    with pytest.raises(AgentFSError) as ei:
        run(A.t_fs_write, ctx, path="x" * 300, content="z")
    msg = str(ei.value)
    assert str(root(ctx)) not in msg and "too long" in msg.lower() and len(msg) < 300


def test_deep_parent_is_a_file(ctx):
    run(A.t_fs_write, ctx, path="a/f", content="x")
    with pytest.raises(AgentFSError) as ei:
        run(A.t_fs_write, ctx, path="a/f/g/h", content="z")
    assert str(root(ctx)) not in str(ei.value)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_rmtree_permission_error_relative(ctx):
    run(A.t_fs_write, ctx, path="d/sub/x", content="1")
    sub = root(ctx) / "d" / "sub"
    os.chmod(sub, 0o500)
    try:
        with pytest.raises(AgentFSError) as ei:
            run(A.t_fs_delete, ctx, path="d", recursive=True)
        assert str(root(ctx)) not in str(ei.value)
    finally:
        os.chmod(sub, 0o700)


def test_os_error_without_filename(ctx):
    e = OSError(28, "No space left on device")
    err = A._os_error(ctx, "fs_write", e)
    assert str(err) == "No space left on device"


def test_os_error_outside_root_shows_name_only(ctx):
    e = FileNotFoundError(2, "No such file or directory", "/etc/secret/thing")
    assert str(A._os_error(ctx, "fs_read", e)) == "No such file or directory: thing"


def test_os_error_bytes_filename(ctx):
    root(ctx).mkdir(parents=True, exist_ok=True)
    e = FileNotFoundError(2, "No such file or directory", os.fsencode(str(root(ctx) / "b\x1bx")))
    msg = str(A._os_error(ctx, "fs_read", e))
    assert "\x1b" not in msg and "b\\x1bx" in msg


# ---- echoed names are escaped -------------------------------------------------------------------

EVIL = "e\x1b[31mvil\nFORGED\u202e.txt"


def test_write_message_escaped(ctx):
    out = run(A.t_fs_write, ctx, path=EVIL, content="z")
    assert "\x1b" not in out and "\n" not in out and "\u202e" not in out
    assert (root(ctx) / EVIL).read_text() == "z"


def test_list_lines_escaped(ctx):
    run(A.t_fs_write, ctx, path=EVIL, content="z")
    (root(ctx) / ("d\nx")).mkdir()
    os.symlink("nowhere", root(ctx) / "l\x07")
    out = run(A.t_fs_list, ctx)
    assert "\x1b" not in out and "\u202e" not in out and "\x07" not in out
    assert out.count("\n") == 3  # header + three rows, nothing forged


def test_list_single_file_escaped(ctx):
    run(A.t_fs_write, ctx, path=EVIL, content="z")
    out = run(A.t_fs_list, ctx, path=EVIL)
    assert "\n" not in out and "\x1b" not in out


def test_read_messages_escaped(ctx):
    out = run(A.t_fs_read, ctx, path="no\x1bpe")
    assert "\x1b" not in out
    (root(ctx) / "d\x1b").mkdir()
    out = run(A.t_fs_read, ctx, path="d\x1b")
    assert "\x1b" not in out and "directory" in out


def test_delete_message_escaped(ctx):
    run(A.t_fs_write, ctx, path=EVIL, content="z")
    out = run(A.t_fs_delete, ctx, path=EVIL)
    assert out.startswith("deleted ") and "\n" not in out and "\x1b" not in out
    assert not (root(ctx) / EVIL).exists()


def test_agent_id_escaped_in_list_header(ctx):
    ctx.agent_id = "a\nB"
    out = run(A.t_fs_list, ctx)
    assert out.split("\n")[0].startswith("[agent dir a\\x0aB:")


def test_audit_keeps_raw_path(ctx):
    run(A.t_fs_write, ctx, path="x\ty", content="z")
    w = [d for k, d in ctx.core.codex.events if k == "agentfs.write"]
    assert w and w[-1]["path"] == "x\ty"


# ---- special files ------------------------------------------------------------------------------

def test_delete_fifo_still_refused_but_escaped(ctx):
    root(ctx).mkdir(parents=True, exist_ok=True)
    os.mkfifo(root(ctx) / "f\x1bf")
    out = run(A.t_fs_delete, ctx, path="f\x1bf")
    assert out == "ERROR: f\\x1bf is not a file or directory"
    assert os.path.lexists(root(ctx) / "f\x1bf")


def test_write_onto_fifo_refused(ctx):
    root(ctx).mkdir(parents=True, exist_ok=True)
    os.mkfifo(root(ctx) / "ff")
    with pytest.raises(AgentFSError, match="not a regular file"):
        run(A.t_fs_write, ctx, path="ff", content="z")


# ---- earlier guarantees still hold --------------------------------------------------------------

def test_symlink_escape_still_refused(ctx):
    root(ctx).mkdir(parents=True, exist_ok=True)
    os.symlink("/etc/passwd", root(ctx) / "l")
    with pytest.raises(AgentFSError, match="escapes"):
        run(A.t_fs_read, ctx, path="l")
    with pytest.raises(AgentFSError, match="escapes"):
        run(A.t_fs_write, ctx, path="l", content="z")


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "~/x", "C:x", "a/../../b", "a\0b"])
def test_confinement(ctx, bad):
    with pytest.raises(AgentFSError):
        run(A.t_fs_write, ctx, path=bad, content="z")


def test_file_cap_and_quota(ctx, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "1")
    with pytest.raises(AgentFSError, match="per-file cap"):
        run(A.t_fs_write, ctx, path="big", content="x" * (A.MB + 1))
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    run(A.t_fs_write, ctx, path="a", content="x" * (A.MB - 10))
    with pytest.raises(AgentFSError, match="quota"):
        run(A.t_fs_write, ctx, path="b", content="x" * 20)


def test_read_max_chars_odd(ctx):
    run(A.t_fs_write, ctx, path="t", content="abcdef")
    for mc in ("nan", math.inf, -5, "2", None, [1]):
        out = run(A.t_fs_read, ctx, path="t", max_chars=mc)
        assert out.startswith("a")


def test_no_temp_files_left(ctx):
    for i in range(20):
        run(A.t_fs_write, ctx, path="f", content=str(i))
    assert sorted(p.name for p in root(ctx).iterdir()) == ["f"]


def test_wrappers_stay_coroutines():
    import inspect
    for fn in (A.t_fs_write, A.t_fs_read, A.t_fs_list, A.t_fs_delete):
        assert inspect.iscoroutinefunction(fn)
