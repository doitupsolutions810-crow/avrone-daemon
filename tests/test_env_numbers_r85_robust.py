"""Round 85: one reading of numeric env settings (config.env_num) for 23 module readers.
Fullwidth digits were accepted, '"100"' silently became the default, AVRONE_MCP_TIMEOUT=60s broke
`import avrone.mcp_client`, and AVRONE_SHELL_PIPE_GRACE=nan gave 10 s."""
from __future__ import annotations

import subprocess
import sys

import pytest

from avrone import (bridge, checkpoint, codex, config as c, ctxspawn, fleet, jobqueue, logsafe, mcp_client,
                    mcp_server, memstore, safeio, server, sovereign, telemetry, tools_extra)


@pytest.fixture(autouse=True)
def _reset():
    c._NUM_WARNED.clear()


@pytest.mark.parametrize("raw,want", [
    (None, None), (5, None), ("", None), ("   ", None), ("''", None), ("10", 10.0), (" 10 ", 10.0),
    ('"10"', 10.0), ("'10'", 10.0), ("1_000", 1000.0), ("1e3", 1000.0), ("-4", -4.0), ("2.5", 2.5),
    ("\uff11\uff10", None), ("\u0661\u0660", None), ("nan", None), ("inf", None), ("-inf", None),
    ("1e999", None), ("10s", None), ("0x10", None), ("1" * 65, None), ("10\n", 10.0), ("'10\"", None),
])
def test_parse_env_number(raw, want):
    assert c.parse_env_number(raw) == want


def test_env_num_warns_once_per_value(capsys, monkeypatch):
    monkeypatch.setenv("AVRONE_X_TEST", "10s")
    for _ in range(5):
        assert c.env_num("AVRONE_X_TEST") is None
    err = capsys.readouterr().err
    assert err.count("AVRONE_X_TEST") == 1


def test_env_num_no_warning_for_unset_or_blank(capsys, monkeypatch):
    monkeypatch.delenv("AVRONE_X_TEST", raising=False)
    c.env_num("AVRONE_X_TEST")
    monkeypatch.setenv("AVRONE_X_TEST", "  ")
    c.env_num("AVRONE_X_TEST")
    assert capsys.readouterr().err == ""


def test_env_num_warning_capped_and_escaped(capsys, monkeypatch):
    for i in range(100):
        monkeypatch.setenv("AVRONE_X_TEST", f"bad{i}\x1b[31m\n")
        c.env_num("AVRONE_X_TEST")
    err = capsys.readouterr().err
    assert len(c._NUM_WARNED) == c.NUM_WARN_CAP and "\x1b" not in err
    assert err.count("\n") == c.NUM_WARN_CAP


def test_env_num_explicit_env_mapping():
    assert c.env_num("A", {"A": '"7"'}) == 7.0 and c.env_num("A", {}) is None


# (reader, env name, default, quoted value, its clamped result)
INT_READERS = [
    (codex.max_subscribers, "AVRONE_MAX_WS_SUBSCRIBERS"),
    (codex.replay_limit, "AVRONE_REPLAY_MAX_EVENTS"),
    (fleet.max_nodes, "AVRONE_FLEET_MAX_NODES"),
    (mcp_server.max_inflight, "AVRONE_MCP_MAX_INFLIGHT"),
    (mcp_server.max_batch, "AVRONE_MCP_MAX_BATCH"),
    (telemetry.max_file_bytes, "AVRONE_TELEMETRY_MAX_FILE_BYTES"),
    (logsafe.arg_chars, "AVRONE_LOG_ARG_MAX_CHARS"),
    (mcp_client.max_response_bytes, "AVRONE_MCP_MAX_RESPONSE_BYTES"),
    (checkpoint.max_bytes, "AVRONE_CHECKPOINT_MAX_BYTES"),
    (server.max_background_runs, None),
    (jobqueue.max_pending, None),
    (ctxspawn.max_agents_per_run, None),
    (bridge.max_posts, None),
]


def _env_name(fn, name):
    if name:
        return name
    import inspect
    import re
    src = inspect.getsource(fn)
    return re.search(r'"(AVRONE_[A-Z_]+)"', src).group(1)


@pytest.mark.parametrize("fn,name", INT_READERS, ids=lambda x: getattr(x, "__qualname__", str(x)))
def test_int_readers_quoted_equals_bare(fn, name, monkeypatch):
    name = _env_name(fn, name)
    monkeypatch.delenv(name, raising=False)
    default = fn()
    probe = default + 1 if default > 1 else 2
    monkeypatch.setenv(name, str(probe))
    bare = fn()
    for wrapped in (f'"{probe}"', f"'{probe}'", f" {probe} ", f"{probe}\n"):
        monkeypatch.setenv(name, wrapped)
        assert fn() == bare, wrapped


@pytest.mark.parametrize("fn,name", INT_READERS, ids=lambda x: getattr(x, "__qualname__", str(x)))
def test_int_readers_reject_fullwidth_and_junk(fn, name, monkeypatch):
    name = _env_name(fn, name)
    monkeypatch.delenv(name, raising=False)
    default = fn()
    for junk in ("\uff13", "\u0663", "3s", "nan", "inf", "", "''"):
        monkeypatch.setenv(name, junk)
        assert fn() == default, junk
    monkeypatch.setenv(name, "1e999")
    assert fn() == default


def test_memstore_caps(monkeypatch):
    monkeypatch.delenv("AVRONE_MEM_MAX_ENTRIES", raising=False)
    import inspect
    import re
    name = re.search(r'"(AVRONE_[A-Z_]+)"', inspect.getsource(memstore.max_entries)).group(1)
    monkeypatch.delenv(name, raising=False)
    d = memstore.max_entries()
    monkeypatch.setenv(name, "\uff15")
    assert memstore.max_entries() == d
    monkeypatch.setenv(name, '"5"')
    assert memstore.max_entries() == 5


@pytest.mark.parametrize("raw,want", [('"2"', 2.0), ("nan", 30.0), ("inf", 30.0), ("\uff12", 30.0), ("-1", 30.0),
                                      ("", 30.0), ("99999", 3600.0)])
def test_lock_wait(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_LOCK_WAIT_S", raw)
    assert safeio.lock_wait_s() == want


@pytest.mark.parametrize("raw,want", [("'3'", 3.0), ("nan", 5.0), ("-2", 5.0), ("\uff13", 5.0), ("999", 60.0)])
def test_nonce_lock(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_NONCE_LOCK_S", raw)
    assert fleet.nonce_lock_wait_s() == want


@pytest.mark.parametrize("raw,want", [('"5"', 5.0), ("nan", 30.0), ("0", 0.0), ("9999", 3600.0), ("\uff15", 30.0)])
def test_mcp_shutdown_grace(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_SHUTDOWN_GRACE", raw)
    assert mcp_server.shutdown_grace() == want


@pytest.mark.parametrize("raw,want", [('"12"', 12.0), ("nan", 30.0), ("0", 1.0), ("9999", 600.0)])
def test_ws_send_timeout(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_WS_SEND_TIMEOUT", raw)
    assert server.ws_send_timeout() == want


@pytest.mark.parametrize("raw,want", [("nan", 0.5), ("inf", 0.5), ('"2"', 2.0), ("-1", 0.0), ("50", 10.0),
                                      ("", 0.5), ("\uff12", 0.5)])
def test_pipe_grace(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_SHELL_PIPE_GRACE", raw)
    assert sovereign._pipe_grace() == want


@pytest.mark.parametrize("raw,want", [('"0"', 0.0), ("'2'", 2.0), ("", 5.0), ("x", 5.0), ("-1", 0.0), ("nan", 5.0)])
def test_runs_cache(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_RUNS_CACHE_S", raw)
    assert codex.runs_cache_s() == want


def test_priv_shell_timeout(monkeypatch):
    monkeypatch.delenv("AVRONE_PRIV_SHELL_TIMEOUT", raising=False)
    d = tools_extra.priv_shell_timeout()
    monkeypatch.setenv("AVRONE_PRIV_SHELL_TIMEOUT", '"7"')
    assert tools_extra.priv_shell_timeout() == 7
    monkeypatch.setenv("AVRONE_PRIV_SHELL_TIMEOUT", " 7 ")
    assert tools_extra.priv_shell_timeout() == 7
    monkeypatch.setenv("AVRONE_PRIV_SHELL_TIMEOUT", "\uff17")
    assert tools_extra.priv_shell_timeout() == d
    monkeypatch.setenv("AVRONE_PRIV_SHELL_TIMEOUT", "7")
    assert tools_extra.priv_shell_timeout(3) == 3 and tools_extra.priv_shell_timeout(99) == 7


@pytest.mark.parametrize("raw,want", [("60s", 60.0), ("", 60.0), ("nan", 60.0), ("inf", 60.0), ("-1", 60.0),
                                      ("0", 60.0), ("0.2", 1.0), ("'90'", 90.0), ("99999", 3600.0)])
def test_mcp_timeout_import_never_fails(raw, want):
    env = {"PATH": "/usr/bin:/bin", "AVRONE_MCP_TIMEOUT": raw, "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1])}
    r = subprocess.run([sys.executable, "-c", "import avrone.mcp_client as m; print(m.DEFAULT_TIMEOUT)"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    assert float(r.stdout.strip()) == want


def test_default_timeout_function(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_TIMEOUT", '"45"')
    assert mcp_client._default_timeout() == 45.0
