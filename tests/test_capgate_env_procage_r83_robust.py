"""Round 83: capgate env read like the rest of config (quoted AVRONE_CAPGATE="enforce" was OFF,
a quoted policy granted nothing, quoted attest switch/log path), and proc_status ages from
/proc/uptime so a wall clock stepped back never shows negative ages or hides long children."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from avrone import capgate, capgate_head_attest as cha
from avrone import tools_hostview as hv


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AVRONE_CAPGATE", "AVRONE_CAPGATE_POLICY", "AVRONE_CAPGATE_ATTEST", "AVRONE_ATTESTED_HISTORY_LOG"):
        monkeypatch.delenv(k, raising=False)
    capgate._WARNED.clear()


@pytest.mark.parametrize("raw,want", [
    ('"enforce"', "enforce"), ("'audit'", "audit"), (" ENFORCE\n", "enforce"), ('" enforce "', "enforce"),
    ("", "off"), ("''", "off"), ("   ", "off"), ("off", "off"), ('"off"', "off"),
    ("enfroce", "off"), ("ＥＮＦＯＲＣＥ", "off"), ("'enforce\"", "off"), ("enforce;", "off"),
])
def test_mode(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE", raw)
    assert capgate.mode() == want


def test_unknown_mode_warns_once(monkeypatch, capsys):
    monkeypatch.setenv("AVRONE_CAPGATE", "enfroce")
    capgate.mode(); capgate.mode()
    err = capsys.readouterr().err
    assert err.count("unknown AVRONE_CAPGATE") == 1 and "OFF" in err


def test_unknown_mode_warning_is_one_line_and_bounded(monkeypatch, capsys):
    monkeypatch.setenv("AVRONE_CAPGATE", "x\n\x1b[31mred" + "y" * 500)
    capgate.mode()
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "\x1b" not in err and len(err) < 200


def test_warned_set_is_capped(monkeypatch, capsys):
    for i in range(100):
        monkeypatch.setenv("AVRONE_CAPGATE", f"bad{i}")
        capgate.mode()
    assert len(capgate._WARNED) <= 34


def test_known_modes_do_not_warn(monkeypatch, capsys):
    for v in ("off", "audit", "enforce", '"enforce"', ""):
        monkeypatch.setenv("AVRONE_CAPGATE", v)
        capgate.mode()
    assert capsys.readouterr().err == ""


POL = {"*": ["echo"], "lead": ["read_*"]}


@pytest.mark.parametrize("wrap", ["{}", "'{}'", '"{}"', " '{}' ", "\n{}\n"])
def test_quoted_policy(wrap, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_POLICY", wrap.replace("{}", json.dumps(POL)))
    assert capgate.policy() == POL
    assert capgate.rights_for("lead") == ["read_*"]


@pytest.mark.parametrize("raw", ['"*"', "'x'", "'{'", "[", "'[1]'", '"not json"', "{bad}"])
def test_bad_policy_fails_closed(raw, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_POLICY", raw)
    assert capgate.policy() == {} and capgate.rights_for("anyone") == []


def test_blank_policy_still_default(monkeypatch):
    for raw in ("", "   "):
        monkeypatch.setenv("AVRONE_CAPGATE_POLICY", raw)
        assert capgate.policy() == {"*": ["*"]}


@pytest.mark.parametrize("raw,on", [('"on"', True), ("'1'", True), (" yes ", True), ('"off"', False),
                                     ("''", False), ("enable", False), ('"enable"', False), ("ｏｎ", False)])
def test_attest_enabled(raw, on, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_ATTEST", raw)
    assert cha.enabled() is on


@pytest.mark.parametrize("raw", ["''", '"  "', "   "])
def test_attest_log_blank_is_default(raw, monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_ATTESTED_HISTORY_LOG", raw)
    assert cha.history_path(tmp_path) == tmp_path / "attested" / "history.jsonl"


def test_attest_log_quoted_and_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_ATTESTED_HISTORY_LOG", f'"{tmp_path}/h.jsonl"')
    assert cha.history_path("/nope") == tmp_path / "h.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AVRONE_ATTESTED_HISTORY_LOG", "~/x.jsonl")
    assert cha.history_path("/nope") == tmp_path / "x.jsonl"


# ------------------------------------------------------------------ proc ages
def _fake(tmp_path, btime="1000", uptime="500.25 900.0", kids=((11, 10, 100), (12, 10, 900000))):
    def mk(pid, ppid, ticks, state="S"):
        d = tmp_path / str(pid)
        d.mkdir()
        rest = [state, str(ppid)] + ["0"] * 17 + [str(ticks), "0", "10"] + ["0"] * 10
        (d / "stat").write_text(f"{pid} (n) " + " ".join(rest))
        (d / "cmdline").write_bytes(b"sleep\x00" + str(pid).encode())
    (tmp_path / "stat").write_text(f"cpu 1\nbtime {btime}\n")
    if uptime is not None:
        (tmp_path / "uptime").write_text(uptime)
    mk(10, 1, 0)
    for k in kids:
        mk(*k)


TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


@pytest.mark.parametrize("raw,want", [("500.25 900.0", 500.25), ("12\n", 12.0), ("", None), ("x y", None),
                                      ("nan 1", None), ("-5 1", None), ("inf 1", None), ("1e20 1", None)])
def test_uptime(raw, want, monkeypatch, tmp_path):
    (tmp_path / "uptime").write_text(raw)
    monkeypatch.setattr(hv, "PROC", tmp_path)
    assert hv._uptime() == want


def test_uptime_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(hv, "PROC", tmp_path)
    assert hv._uptime() is None


def test_age_from_uptime(monkeypatch, tmp_path):
    _fake(tmp_path)
    monkeypatch.setattr(hv, "PROC", tmp_path)
    kids = hv._children(10)
    assert kids[11]["age"] == pytest.approx(500.25 - 100 / TCK)
    assert kids[12]["age"] == 0.0  # started "after" uptime (bad tick): clamped, never negative


def test_clock_stepped_back_still_shows_long_child(monkeypatch, tmp_path):
    # wall clock set far into the past: time.time() - started is hugely negative
    _fake(tmp_path, btime="4000000000", kids=((11, 10, 100),))
    monkeypatch.setattr(hv, "PROC", tmp_path)
    monkeypatch.setattr(hv.os, "getpid", lambda: 10)
    monkeypatch.setenv("AVRONE_PROC_LONG_S", "60")
    out = run(hv.t_proc_status(None, {}))
    assert "1 running >= 60s" in out and "age -" not in out
    assert f"age {500.25 - 100 / TCK:.0f}s" in out


def test_no_uptime_falls_back_to_wall_clock_clamped(monkeypatch, tmp_path):
    _fake(tmp_path, btime="4000000000", uptime=None, kids=((11, 10, 100),))
    monkeypatch.setattr(hv, "PROC", tmp_path)
    monkeypatch.setattr(hv.os, "getpid", lambda: 10)
    out = run(hv.t_proc_status(None, {"all": True}))
    assert "age 0s" in out and "age -" not in out


def test_no_btime_no_uptime_shows_unknown(monkeypatch, tmp_path):
    _fake(tmp_path, btime="x", uptime=None, kids=((11, 10, 100),))
    monkeypatch.setattr(hv, "PROC", tmp_path)
    monkeypatch.setattr(hv.os, "getpid", lambda: 10)
    out = run(hv.t_proc_status(None, {"all": True}))
    assert "age ?" in out and "0 running" in out


def test_oldest_first(monkeypatch, tmp_path):
    _fake(tmp_path, kids=((11, 10, 40000), (12, 10, 100), (13, 10, 2000)))
    monkeypatch.setattr(hv, "PROC", tmp_path)
    monkeypatch.setattr(hv.os, "getpid", lambda: 10)
    out = run(hv.t_proc_status(None, {"all": True}))
    order = [ln.split()[1] for ln in out.splitlines()[1:]]
    assert order == ["12", "13", "11"]


@pytest.mark.parametrize("r,want", [({"age": 5}, 5.0), ({"age": -3}, 0.0), ({"age": float("nan"), "started": 90.0}, 10.0),
                                    ({"age": True, "started": None}, None), ({"started": 200.0}, 0.0), ({}, None),
                                    ({"age": None, "started": "x"}, None)])
def test_age_helper(r, want):
    assert hv._age(r, 100.0) == want
