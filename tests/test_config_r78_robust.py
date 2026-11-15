"""Round 78: env parsing (on/off values, numbers, .env lines, AVRONE_ENV_FILE)."""
from __future__ import annotations

from pathlib import Path

import pytest

from avrone import config as c


@pytest.mark.parametrize("raw", ["true", "TRUE ", " 1", '"true"', "'on'", "Yes", "\ton\n", "y", "Enabled", "enable"])
def test_true_values(raw):
    assert c.parse_bool(raw, False) is True


@pytest.mark.parametrize("raw", ["false", " 0", "'off'", '"no"', "N", "disabled", "None", "FALSE\r"])
def test_false_values_quiet(raw, capsys):
    assert c.parse_bool(raw, True) is False
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("raw", ["nope", "maybe", "2", "tru", "on!"])
def test_unknown_is_off_and_reported(raw, capsys):
    assert c.parse_bool(raw, True) is False
    assert "unknown on/off value" in capsys.readouterr().err


def test_unknown_report_is_capped(capsys):
    c.parse_bool("x" * 500, True)
    assert len(capsys.readouterr().err) < 120


@pytest.mark.parametrize("raw", ["", " ", '""', "''", "\t"])
@pytest.mark.parametrize("default", [True, False])
def test_blank_is_default(raw, default):
    assert c.parse_bool(raw, default) is default


@pytest.mark.parametrize("default", [True, False])
def test_unset_is_default(default):
    assert c.parse_bool(None, default) is default


@pytest.mark.parametrize("raw", ["", "  ", '""'])
def test_blank_override(raw):
    assert c.parse_bool(raw, True, blank=False) is False


@pytest.mark.parametrize("var,attr", [("AVRONE_ALLOW_NETWORK", "allow_network"),
                                      ("AVRONE_AGENTS_CAN_SHELL", "agents_can_shell")])
@pytest.mark.parametrize("raw", ["", " ", '""'])
def test_permission_switch_blank_stays_off(var, attr, raw, monkeypatch):
    monkeypatch.setenv(var, raw)
    assert getattr(c.Settings(), attr) is False


@pytest.mark.parametrize("var,attr", [("AVRONE_ALLOW_NETWORK", "allow_network"),
                                      ("AVRONE_AGENTS_CAN_SHELL", "agents_can_shell")])
def test_permission_switch_unset_default_on(var, attr, monkeypatch):
    monkeypatch.delenv(var, raising=False)
    assert getattr(c.Settings(), attr) is True


@pytest.mark.parametrize("raw,want", [("", True), (" ", True), ("off", False), ("TRUE ", True)])
def test_governor_blank_default(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_GOVERNOR", raw)
    assert c.Settings().governor is want


@pytest.mark.parametrize("raw", ["\uff12", "\u0663", "\u00b2", "\u0968", "1\u200b"])
def test_non_ascii_numbers_refused(raw, capsys):
    assert c._num(raw, 7, int) == 7
    assert "ignoring bad number" in capsys.readouterr().err


@pytest.mark.parametrize("raw,want", [("5", 5), (" 6 ", 6), ('"8"', 8), ("1_0", 10), ("2.9", 2), ("", 7), (None, 7)])
def test_numbers_still_parse(raw, want):
    assert c._num(raw, 7, int) == want


def test_bad_number_report_capped(capsys):
    c._num("z" * 1000, 3, int)
    assert len(capsys.readouterr().err) < 120


@pytest.mark.parametrize("line", ["A=x\x00y", "A\x00=v", "B=\x00", "export C=\x00x"])
def test_nul_lines_skipped(line):
    assert c.parse_dotenv_line(line) is None


def test_load_dotenv_with_nul_does_not_raise(tmp_path, monkeypatch):
    for k in ("R78_A", "R78_B", "R78_C", "R78_D"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_bytes(b"R78_A=1\nR78_B=x\x00y\nR78_C=3\n")
    c._load_dotenv(f)
    import os
    assert os.environ.get("R78_A") == "1" and os.environ.get("R78_C") == "3"
    assert "R78_B" not in os.environ


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\x0b"])
def test_load_dotenv_splits_on_newline_only(sep, tmp_path, monkeypatch):
    import os
    for k in ("R78_V", "R78_W", "R78_INJ"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text(f"R78_V=a{sep}R78_INJ=evil\nR78_W=ok\n", encoding="utf-8")
    c._load_dotenv(f)
    assert "R78_INJ" not in os.environ
    assert os.environ.get("R78_V") == f"a{sep}R78_INJ=evil"
    assert os.environ.get("R78_W") == "ok"


def test_load_dotenv_crlf(tmp_path, monkeypatch):
    import os
    monkeypatch.delenv("R78_CR", raising=False)
    f = tmp_path / ".env"
    f.write_bytes(b"R78_CR=v\r\n")
    c._load_dotenv(f)
    assert os.environ.get("R78_CR") == "v"


@pytest.mark.parametrize("raw", ["", " ", "\t"])
def test_env_file_blank_is_default(raw, monkeypatch):
    monkeypatch.setenv("AVRONE_ENV_FILE", raw)
    assert c.env_file() == Path(".env")


def test_env_file_unset(monkeypatch):
    monkeypatch.delenv("AVRONE_ENV_FILE", raising=False)
    assert c.env_file() == Path(".env")


def test_env_file_quoted_and_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AVRONE_ENV_FILE", '"~/my.env"')
    assert c.env_file() == tmp_path / "my.env"


def test_env_file_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_ENV_FILE", str(tmp_path / "x.env"))
    assert c.env_file() == tmp_path / "x.env"
