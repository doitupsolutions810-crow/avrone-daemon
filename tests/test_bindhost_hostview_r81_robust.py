"""Round 81: one reading of AVRONE_HOST/AVRONE_PORT for server.main, doctor and exposure_check
(a blank host bound every interface while the checks said loopback), and proc_status edges."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace as NS

import pytest

from avrone import config as c
from avrone import doctor
from avrone import tools_hostview as hv


@pytest.mark.parametrize("raw,want", [
    (None, "127.0.0.1"), ("", "127.0.0.1"), ("   ", "127.0.0.1"), ("''", "127.0.0.1"), ('""', "127.0.0.1"),
    ("0.0.0.0", "0.0.0.0"), (" 0.0.0.0 ", "0.0.0.0"), ("'0.0.0.0'", "0.0.0.0"), ("::", "::"),
    ("localhost", "localhost"), ("10.0.0.5", "10.0.0.5"),
    ("0.0.0.0 evil", "127.0.0.1"), ("\uff10.\uff10.\uff10.\uff10", "127.0.0.1"), ("a\x00b", "127.0.0.1"),
    ("a\nb", "127.0.0.1"),
])
def test_bind_host(raw, want):
    env = {} if raw is None else {"AVRONE_HOST": raw}
    assert c.bind_host(env) == want


@pytest.mark.parametrize("raw,want", [
    (None, 8710), ("", 8710), (" ", 8710), ("8787", 8787), (" 8787 ", 8787), ("'8787'", 8787),
    ("0", 8710), ("65535", 65535), ("65536", 8710), ("-1", 8710), ("80.5", 8710), ("abc", 8710),
    ("\uff18\uff17\uff18\uff17", 8710), ("\u0668\u0667", 8710), ("1e3", 8710), ("1", 1),
])
def test_bind_port(raw, want):
    env = {} if raw is None else {"AVRONE_PORT": raw}
    assert c.bind_port(env) == want


def test_bind_reads_process_env(monkeypatch):
    monkeypatch.setenv("AVRONE_HOST", "  ")
    monkeypatch.setenv("AVRONE_PORT", "")
    assert (c.bind_host(), c.bind_port()) == ("127.0.0.1", 8710)


def test_bad_values_warn_capped(capsys):
    c.bind_port({"AVRONE_PORT": "x" * 500})
    c.bind_host({"AVRONE_HOST": "a b" * 200})
    err = capsys.readouterr().err
    assert "AVRONE_PORT" in err and "AVRONE_HOST" in err and len(err) < 400


@pytest.mark.parametrize("raw,want", [("", ("127.0.0.1", 8710)), ("0.0.0.0", ("0.0.0.0", 8710))])
def test_server_main_binds_what_checks_report(raw, want, monkeypatch):
    import uvicorn
    from avrone import server
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(host=host, port=port))
    monkeypatch.setenv("AVRONE_HOST", raw)
    monkeypatch.delenv("AVRONE_PORT", raising=False)
    server.main()
    assert (seen["host"], seen["port"]) == want


def test_server_main_blank_port_no_crash(monkeypatch):
    import uvicorn
    from avrone import server
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(host=host, port=port))
    monkeypatch.setenv("AVRONE_PORT", "")
    monkeypatch.setenv("AVRONE_HOST", "127.0.0.1")
    server.main()
    assert seen["port"] == 8710


@pytest.mark.parametrize("host,exposed", [("", False), ("  ", False), ("0.0.0.0", True), ("'0.0.0.0'", True),
                                          ("127.0.0.1", False)])
def test_exposure_host_matches_bind(host, exposed, monkeypatch):
    monkeypatch.setenv("AVRONE_HOST", host)
    e = hv.exposure()
    assert e["exposed"] is exposed
    assert e["host"] == c.bind_host()


@pytest.mark.parametrize("host,warns", [("'0.0.0.0'", True), (" 0.0.0.0 ", True), ("", False), ("  ", False)])
def test_doctor_host_matches_bind(host, warns, tmp_path):
    rep = doctor.run_doctor(env={"AVRONE_HOST": host}, which=lambda n: None, sockets=())
    names = [ch["name"] if isinstance(ch, dict) else ch.name for ch in rep["checks"]]
    assert ("security.admin" in names) is warns


@pytest.mark.parametrize("raw,want", [("\uff16\uff10", 600.0), ("'60'", 60.0), ('"120"', 120.0), (" 30 ", 30.0),
                                      ("", 600.0), ("-5", 600.0), ("inf", 600.0), ("nan", 600.0), ("x", 600.0)])
def test_proc_long_s(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_PROC_LONG_S", raw)
    assert hv._long_s() == want


@pytest.mark.skipif(not os.path.isdir("/proc") or sys.platform != "linux", reason="needs /proc")
def test_proc_status_huge_argv_bounded_and_masked(tmp_path):
    import subprocess
    big = ["A" * 100_000] * 4  # one argv string may not pass 128 KB (MAX_ARG_STRLEN)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--token=ghp_" + "x1Y2" * 10, *big])
    try:
        out = asyncio.run(hv.t_proc_status(NS(), {"all": True}))
        line = next(l for l in out.splitlines() if str(p.pid) in l)
        assert "ghp_x1Y2" not in line and ("***" in line or "REDACTED" in line)
        assert len(line) < 400
    finally:
        p.kill()
        p.wait()


def test_cmdline_read_cap_constant():
    assert hv.CMD_MAX < hv.CMDLINE_READ <= 65536
