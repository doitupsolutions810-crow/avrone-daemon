"""Round 87: the last raw env readers take the node's one reading. trinity limits saw '"40000"' and
" 40000\\n" as junk; AVRONE_LOG_LEVEL='"debug"' crashed the MCP server at start; '"1"' for
AVRONE_AGENT_DOCKER_ISOLATE read as off; quoted recon/priv-shell allowlists lost their first and last
entries ('"*"' denied all); a quoted AVRONE_TOOLS_ONLY kept every tool; quoted URLs and MCP config
paths were used verbatim; evolve/agentfs/hostview numbers accepted fullwidth digits or inf."""
from __future__ import annotations

import logging

import pytest

from avrone import agentfs, agentfs_tools, bootstrap, codex, config as c, evolve, mcp_client, mcp_server
from avrone import recon, tools_extra as tx, tools_hostview as hv, trinity


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    c._NUM_WARNED.clear()
    for k in ("AVRONE_TOOL_OUTPUT_MAX", "AVRONE_CONTEXT_CHARS", "AVRONE_MAX_CALLS_PER_TURN", "AVRONE_DEEP_ROUNDS",
              "AVRONE_LOG_LEVEL", "AVRONE_AGENT_FS", "AVRONE_AGENT_DOCKER_ISOLATE", "AVRONE_RECON_ALLOWLIST",
              "AVRONE_PRIV_SHELL_ALLOW", "AVRONE_TOOLS_ONLY", "AVRONE_MCP_CONFIG", "AVRONE_CODEX_REDACT",
              "AVRONE_PROC_LONG_S", "AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_EVOLVE_MAX_ITERS", "AVRONE_EVOLVE_HISTORY",
              "AVRONE_EVOLVE_MAX_BG", "AVRONE_EVOLVE_SKILL_MAX_BYTES", "AVRONE_EVOLVE_TIME_BUDGET"):
        monkeypatch.delenv(k, raising=False)


# ------------------------------------------------------------------ trinity limits
@pytest.mark.parametrize("raw,want", [(None, 20000), ("", 20000), ("  ", 20000), ("40000", 40000), ('"40000"', 40000),
                                      ("'40000'", 40000), (" 40000\n", 40000), ("40_000", 40000), ("4e4", 40000),
                                      ("\uff14\uff10\uff10\uff10\uff10", 20000), ("nan", 20000), ("inf", 20000), ("1e999", 500000), ('"-1e999"', 1000),
                                      ("x", 20000), ("1", 1000), ("1e12", 500000), ("9" * 70, 20000)])
def test_tool_output_max(raw, want, monkeypatch):
    if raw is not None:
        monkeypatch.setenv("AVRONE_TOOL_OUTPUT_MAX", raw)
    assert trinity.tool_output_max() == want


@pytest.mark.parametrize("raw,want", [('"200000"', 200000), (" 9000 ", 9000), ("\uff19\uff10\uff10\uff10", 120000),
                                      ("1", 8000), ("1e99", 2000000), ("", 120000)])
def test_context_chars(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_CONTEXT_CHARS", raw)
    assert trinity.context_chars() == want


@pytest.mark.parametrize("raw,want", [('"2"', 2), ("'6'\n", 6), ("\uff12", 4), ("0", 1), ("99", 8), ("2.9", 2),
                                      ("true", 4)])
def test_max_calls_per_turn(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_MAX_CALLS_PER_TURN", raw)
    assert trinity.max_calls_per_turn() == want


@pytest.mark.parametrize("raw,want", [('"3"', 3), (" 2 ", 2), ("\uff13", 1), ("inf", 1), ('"1e999"', 99), ("x", 1)])
def test_deep_rounds_env(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", raw)
    assert trinity.resolve_deep_rounds(None, None) == min(want, trinity.MAX_DEEP_ROUNDS)


def test_deep_rounds_request_and_settings_win(monkeypatch):
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", '"3"')
    assert trinity.resolve_deep_rounds(2, None) == 2
    s = type("S", (), {"deep_rounds": 2})()
    assert trinity.resolve_deep_rounds(None, s) == 2


# ------------------------------------------------------------------ MCP server log level
@pytest.mark.parametrize("raw,want", [(None, "WARNING"), ("", "WARNING"), ("debug", "DEBUG"), ('"debug"', "DEBUG"),
                                      (" Info\n", "INFO"), ("warn", "WARNING"), ("fatal", "CRITICAL"),
                                      ("verbose", "WARNING"), ("warn ing", "WARNING"), ("10", "WARNING"),
                                      ("d\u0131ebug", "WARNING")])
def test_log_level_never_crashes(raw, want, monkeypatch):
    if raw is not None:
        monkeypatch.setenv("AVRONE_LOG_LEVEL", raw)
    lvl = mcp_server.log_level()
    assert lvl == want
    logging.getLogger("r87").setLevel(lvl)  # a name logging accepts


# ------------------------------------------------------------------ agentfs switches
@pytest.mark.parametrize("raw,want", [(None, False), ("1", True), ('"1"', True), ("'on'", True), (" YES ", True),
                                      ("0", False), ('"0"', False), ("maybe", False), ("  ", False)])
def test_docker_isolate(raw, want, monkeypatch):
    if raw is not None:
        monkeypatch.setenv("AVRONE_AGENT_DOCKER_ISOLATE", raw)
    assert agentfs.docker_isolate() is want


@pytest.mark.parametrize("raw,want", [(None, True), ("", True), ("  ", True), ('"0"', False), ("off ", False),
                                      ('"on"', True)])
def test_agent_fs(raw, want, monkeypatch):
    if raw is not None:
        monkeypatch.setenv("AVRONE_AGENT_FS", raw)
    assert agentfs.agent_fs_enabled() is want


@pytest.mark.parametrize("raw,want", [("", 1), ('"64"', 64), ("\uff16\uff14", 1), ("nan", 1), ("inf", 4096), ('"inf"', 4096), ("-inf", 1), ("-3", 1),
                                      ("1_0", 10)])
def test_fs_quota_mb(raw, want, monkeypatch):
    """inf keeps meaning the hard cap (test_cov7), quoted or not; -inf and nan are the default."""
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", raw)
    assert agentfs_tools._mb_env("AVRONE_AGENT_FS_QUOTA_MB", 1, 4096) == want * agentfs_tools.MB


# ------------------------------------------------------------------ allowlists
@pytest.mark.parametrize("raw,want", [("", []), ("*", None), ('"*"', None), ("'*'", None), (" * ", None),
                                      ('"a.com,b.com"', ["a.com", "b.com"]), ("'a.com', \"b.com\"", ["a.com", "b.com"]),
                                      ('"https://A.com/x"', ["a.com"]), ('"a.com', ["a.com"]) if False else ("a.com,*", ["a.com"])])
def test_recon_allowlist(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", raw)
    assert recon._allowlist() == want


def test_recon_quoted_list_allows_its_hosts(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", '"mysite.com,other.org"')
    assert recon._allowed_host("www.mysite.com") and recon._allowed_host("other.org")
    assert not recon._allowed_host("evilmysite.com")


def test_quoted_star_was_never_tighter_than_star(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", '"*"')
    assert recon._allowed_host("example.net")


@pytest.mark.parametrize("raw,want", [("", set()), ("ls,cat", {"ls", "cat"}), ('"ls,cat"', {"ls", "cat"}),
                                      ("'ls', \"cat\"", {"ls", "cat"}), ("/bin/ls,cat", {"cat"}), ("l*s, df", {"df"}),
                                      ('"', set())])
def test_priv_shell_allowlist(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", raw)
    assert tx.priv_shell_allowlist() == want


# ------------------------------------------------------------------ tool focus
class _Reg:
    def __init__(self, names):
        self._n = list(names)

    def names(self):
        return list(self._n)

    def unregister(self, n):
        self._n.remove(n)


@pytest.mark.parametrize("raw", ['"shell,fs_read"', "'shell', 'fs_read'", " shell , fs_read \n"])
def test_tools_only_quoted(raw, monkeypatch):
    monkeypatch.setenv("AVRONE_TOOLS_ONLY", raw)
    r = _Reg(["shell", "fs_read", "web_search"])
    assert bootstrap.apply_tool_focus(r) == ["web_search"]
    assert sorted(r.names()) == ["fs_read", "shell"]


def test_tools_only_blank_or_quotes_only_keeps_all(monkeypatch):
    for raw in ("", '""', "  ", ",,"):
        monkeypatch.setenv("AVRONE_TOOLS_ONLY", raw)
        r = _Reg(["shell", "fs_read"])
        assert bootstrap.apply_tool_focus(r) == [] and len(r.names()) == 2


# ------------------------------------------------------------------ MCP config path
def test_mcp_config_quoted_path(tmp_path, monkeypatch):
    p = tmp_path / "mcp.json"
    p.write_text('{"mcpServers": {"one": {"command": "true"}}}')
    monkeypatch.setenv("AVRONE_MCP_CONFIG", f' "{p}" ')
    assert "one" in mcp_client.load_config()


def test_mcp_config_blank_means_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AVRONE_MCP_CONFIG", "   ")
    assert mcp_client.load_config() == {}
    (tmp_path / "mcp.json").write_text('{"mcpServers": {"d": {"command": "true"}}}')
    assert "d" in mcp_client.load_config()


# ------------------------------------------------------------------ codex redaction switch
@pytest.mark.parametrize("raw,want", [(None, True), ("", True), ('"off"', False), (" OFF\n", False), ("'0'", False),
                                      ("on", True), ("nope", True), ("of f", True)])
def test_codex_redact(raw, want, monkeypatch):
    if raw is not None:
        monkeypatch.setenv("AVRONE_CODEX_REDACT", raw)
    assert codex._redact_on() is want


# ------------------------------------------------------------------ hostview and evolve numbers
@pytest.mark.parametrize("raw,want", [("", 600.0), ('"60"', 60.0), ("'60\"", 600.0), ("1_0", 10.0), ("\uff16\uff10", 600.0),
                                      ("inf", 600.0), ("-1", 600.0), ("0", 600.0)])
def test_proc_long_s(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_PROC_LONG_S", raw)
    assert hv._long_s() == want


@pytest.mark.parametrize("raw,want", [("", None), ('"5"', 5.0), ("\uff15", None), ("inf", None), ("nan", None),
                                      ("-2", None), ("0", None), ("2_0", 20.0)])
def test_evolve_env_num(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", raw)
    assert evolve._env_num("AVRONE_EVOLVE_MAX_BG") == want


def test_evolve_readers_never_crash_on_inf(monkeypatch):
    for k in ("AVRONE_EVOLVE_SKILL_MAX_BYTES", "AVRONE_EVOLVE_HISTORY", "AVRONE_EVOLVE_MAX_BG",
              "AVRONE_EVOLVE_MAX_ITERS", "AVRONE_EVOLVE_TIME_BUDGET"):
        monkeypatch.setenv(k, "inf")
    assert evolve.skill_max_bytes() == evolve.SKILL_MAX_BYTES_CAP
    assert evolve.history_cap() >= 1 and evolve.max_bg_loops() >= 1
    n, b = evolve.loop_limits()
    assert n >= 1 and b > 0


def test_evolve_quoted_limits_apply(monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", '"2"')
    assert evolve.loop_limits()[0] == 2


@pytest.mark.parametrize("raw,want", [(None, []), ("", []), (",,", []), ("a,b", ["a", "b"]), ('"a,b"', ["a", "b"]),
                                      ("'a', 'b'", ["a", "b"]), (" a , \"b\" ,", ["a", "b"]), ('"', []), (5, [])])
def test_env_list(raw, want):
    assert c.env_list(raw) == want


@pytest.mark.parametrize("raw", ["*,", "*, *.", '"*,"', "*,*"])
def test_star_with_extras_stays_closed(raw, monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", raw)
    assert recon._allowlist() == []
