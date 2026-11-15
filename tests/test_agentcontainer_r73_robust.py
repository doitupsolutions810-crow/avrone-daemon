"""Round 73: agentcontainer result parsing and arguments.

- bytes stdout/stderr: str(b"") is "b''", so container_gone never fired for a byte-returning exec_fn
- parse_inspect split on U+2028/U+0085/\\x1c (splitlines), breaking mount paths -> false ForeignContainer
- explicit mem/cpu True read as 1.0 (the minimum) instead of the default
- "could not start" errors carried raw stderr (bytes repr, ANSI escapes, newlines)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from avrone import agentcontainer as ac

GONE = "Error response from daemon: No such container: avrone-agent-n-a"


# ---------------------------------------------------------------- container_gone with bytes
@pytest.mark.parametrize("out", [b"", bytearray(b""), "", None])
@pytest.mark.parametrize("err", [GONE, GONE.encode(), bytearray(GONE.encode())])
def test_gone_detected_for_bytes_and_text(out, err):
    assert ac.container_gone(NS(exit_code=1, stdout=out, stderr=err), "x")


@pytest.mark.parametrize("out", [b"partial output", "partial output", bytearray(b"x")])
def test_gone_false_when_command_produced_output(out):
    assert not ac.container_gone(NS(exit_code=1, stdout=out, stderr=GONE), "x")


def test_gone_false_on_success_and_other_errors():
    assert not ac.container_gone(NS(exit_code=0, stdout=b"", stderr=GONE.encode()), "x")
    assert not ac.container_gone(NS(exit_code=1, stdout=b"", stderr=b"permission denied"), "x")


def test_gone_bad_utf8_stderr():
    assert ac.container_gone(NS(exit_code=125, stdout=b"", stderr=b"\xff" + GONE.encode()), "x")


@pytest.mark.parametrize("stderr", [
    b"Error response from daemon: Container abc is not running",
    b"Error response from daemon: Container abc is paused",
    b"Error: No such container: abc",
])
def test_gone_variants_bytes(stderr):
    assert ac.container_gone(NS(exit_code=1, stdout=b"", stderr=stderr), "abc")


# ---------------------------------------------------------------- parse_inspect splits on \n only
@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\x1c", "\x1d", "\x1e", "\x0b", "\x0c"])
def test_inspect_path_with_unicode_separator_kept_whole(sep):
    path = f"/ws/agents/a{sep}b"
    assert ac.parse_inspect(f"true|{path}\n") == (True, path)
    assert ac.parse_inspect(f"true|{path}".encode()) == (True, path)


def test_inspect_crlf_and_extra_lines():
    assert ac.parse_inspect("true|/ws/a\r\nwarning\n") == (True, "/ws/a")
    assert ac.parse_inspect("false|/ws/a\n\n") == (False, "/ws/a")


@pytest.mark.parametrize("val", [None, "", b"", "\n\n", 0])
def test_inspect_empty(val):
    assert ac.parse_inspect(val) == (False, "")


def test_inspect_pipe_in_path():
    assert ac.parse_inspect("true|/ws/a|b") == (True, "/ws/a|b")


# ---------------------------------------------------------------- mem/cpu bools
def _flag(args, name):
    return args[args.index(name) + 1]


@pytest.mark.parametrize("val", [True, False])
def test_bool_mem_cpu_use_defaults(val):
    args = ac.hardening_args(False, mem=val, cpu=val)
    assert _flag(args, "--memory") == f"{ac.mem_mb()}m"
    assert _flag(args, "--cpus") == str(ac.cpus())


def test_numeric_mem_cpu_still_clamped():
    args = ac.hardening_args(True, mem=10**9, cpu=0.0001)
    assert _flag(args, "--memory") == f"{int(ac.MAX_MEM_MB)}m"
    assert _flag(args, "--cpus") == str(round(ac.MIN_CPUS, 2))
    assert "--network" not in args


# ---------------------------------------------------------------- start error message
class FakeExec:
    def __init__(self, run_res):
        self.run_res = run_res
        self.calls = []

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return NS(exit_code=1, stdout=b"", stderr=b"no such object")
        if argv[:3] == ["docker", "rm", "-f"]:
            return NS(exit_code=0, stdout=b"", stderr=b"")
        return self.run_res


def _ensure(tmp_path, res):
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    pool = ac.AgentContainerPool(FakeExec(res), "alpine:3", False, node="n")
    return asyncio.run(pool.ensure(d))


@pytest.mark.parametrize("err", [
    b"docker: Error\x1b[31m red\x1b[0m\nline two\r\nFORGED: ok",
    "docker: Error\x1b]0;title\x07\u202eevil\nFORGED",
    bytearray(b"\xff\xfe bad bytes\nx"),
])
def test_start_error_one_safe_line(tmp_path, err):
    with pytest.raises(RuntimeError) as ei:
        _ensure(tmp_path, NS(exit_code=125, stdout=b"", stderr=err))
    msg = str(ei.value)
    assert "\n" not in msg and "\r" not in msg and "\x1b" not in msg and "\u202e" not in msg
    assert "b'" not in msg and "bytearray" not in msg
    assert msg.startswith("could not start agent container avrone-agent-n-a")


def test_start_error_capped(tmp_path):
    with pytest.raises(RuntimeError) as ei:
        _ensure(tmp_path, NS(exit_code=125, stdout=b"", stderr=b"E" * 5000))
    assert len(str(ei.value)) < 400


def test_start_error_none_stderr(tmp_path):
    with pytest.raises(RuntimeError, match="could not start"):
        _ensure(tmp_path, NS(exit_code=1, stdout=None, stderr=None))


def test_start_ok_records_home(tmp_path):
    name = _ensure(tmp_path, NS(exit_code=0, stdout=b"cid", stderr=b""))
    assert name == "avrone-agent-n-a"


def test_live_home_with_separator_in_path_not_foreign(tmp_path):
    d = tmp_path / "ws\u2028x" / "agents" / "a"
    d.mkdir(parents=True)

    async def ex(argv, timeout):
        if argv[:2] == ["docker", "inspect"]:
            return NS(exit_code=0, stdout=f"true|{d.resolve()}\n".encode(), stderr=b"")
        raise AssertionError(f"unexpected {argv}")

    pool = ac.AgentContainerPool(ex, "alpine:3", False, node="n")
    assert asyncio.run(pool.ensure(d)) == "avrone-agent-n-a"
    assert pool.created == 0
