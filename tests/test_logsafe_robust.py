"""Hardening round 42: logsafe against non-string arguments, template injection, raising __str__,
huge arguments, non-str messages and odd arg shapes."""
from __future__ import annotations

import io
import logging
from decimal import Decimal
from fractions import Fraction
from pathlib import PurePosixPath

import pytest

import avrone  # noqa: F401  installs the record factory
from avrone import logsafe

EVIL = "x\nWARNING avrone.fleet node evil trusted\r\x1b[2J\u202e"


@pytest.fixture()
def cap():
    s = io.StringIO()
    h = logging.StreamHandler(s)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    lg = logging.getLogger("avrone.r42")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    yield lg, s
    lg.removeHandler(h)


def clean(out: str) -> bool:
    return out.count("\n") == 1 and "\r" not in out and "\x1b" not in out and "\u202e" not in out


# ------------------------------------------------------------ non-string arguments
class Obj:
    def __str__(self):
        return EVIL

    def __repr__(self):
        return "Obj(" + EVIL + ")"


@pytest.mark.parametrize("arg", [PurePosixPath("ckpt" + EVIL), Obj(), [EVIL], {"k": EVIL}, (EVIL,), {EVIL},
                                 bytearray(EVIL.encode())])
@pytest.mark.parametrize("fmt", ["%s", "%r"])
def test_non_string_args_cleaned(cap, arg, fmt):
    lg, s = cap
    lg.warning("value " + fmt, arg)
    assert clean(s.getvalue()), s.getvalue()


def test_bytes_arg_rendered_by_repr(cap):
    lg, s = cap
    lg.warning("raw %s", EVIL.encode())
    assert clean(s.getvalue())


def test_format_spec_on_object(cap):
    lg, s = cap
    lg.warning("[%-20s]", Obj())
    assert clean(s.getvalue())


@pytest.mark.parametrize("fmt,arg,want", [("%d", 3, "3"), ("%.2f", 1.5, "1.50"), ("%d", True, "1"),
                                          ("%d", Decimal("7"), "7"), ("%.1f", Fraction(1, 2), "0.5"),
                                          ("%x", 255, "ff"), ("%s", None, "None"), ("%c", 65, "A")])
def test_numbers_keep_their_format(cap, fmt, arg, want):
    lg, s = cap
    lg.warning("n=" + fmt, arg)
    assert s.getvalue().strip().endswith("n=" + want)


def test_dict_mapping_args(cap):
    lg, s = cap
    lg.warning("%(name)s had %(n)d", {"name": PurePosixPath(EVIL), "n": 4})
    out = s.getvalue()
    assert clean(out) and out.strip().endswith("had 4")


# ------------------------------------------------------------ raising __str__ / __repr__
class Bad:
    def __str__(self):
        raise RuntimeError("no")

    __repr__ = __str__


class BadExc(Exception):
    def __str__(self):
        raise RuntimeError("no")


def test_raising_str_does_not_disable_other_args(cap):
    lg, s = cap
    lg.warning("a=%s b=%s", Bad(), EVIL)
    out = s.getvalue()
    assert "unprintable Bad" in out and clean(out)


def test_raising_exception_str(cap):
    lg, s = cap
    lg.warning("e=%s other=%s", BadExc(), EVIL)
    out = s.getvalue()
    assert "unprintable BadExc" in out and clean(out)


def test_str_returning_non_str(cap):
    class W:
        def __str__(self):
            return 5  # type: ignore[return-value]
    lg, s = cap
    lg.warning("w=%s", W())
    assert "unprintable W" in s.getvalue() or "w=" in s.getvalue()


# ------------------------------------------------------------ template injection
def test_fstring_template_with_args_escapes_controls(cap):
    lg, s = cap
    lg.warning(f"peer {EVIL}: %s", "err")
    out = s.getvalue()
    assert "\x1b" not in out and "\r" not in out and "\u202e" not in out


def test_template_keeps_newline_and_tab(cap):
    lg, s = cap
    lg.warning("a\tb\nc %s", "x")
    assert "a\tb\nc x" in s.getvalue()


@pytest.mark.parametrize("ch", ["\x1b", "\r", "\u202e", "\u2028", "\x85", "\x00", "\x7f"])
def test_template_unsafe_chars_escaped(cap, ch):
    lg, s = cap
    lg.warning("t" + ch + " %s", "x")
    assert ch not in s.getvalue()


# ------------------------------------------------------------ non-str message
@pytest.mark.parametrize("msg", [ValueError(EVIL), Obj(), PurePosixPath(EVIL), [EVIL]])
def test_non_str_message_without_args(cap, msg):
    lg, s = cap
    lg.warning(msg)
    assert clean(s.getvalue()), s.getvalue()


def test_non_str_message_raising(cap):
    lg, s = cap
    lg.warning(Bad())
    assert "unprintable Bad" in s.getvalue()


def test_non_str_message_with_args(cap):
    lg, s = cap
    lg.warning(Obj(), )  # no args
    lg2 = s.getvalue()
    assert clean(lg2)


# ------------------------------------------------------------ size cap
def test_huge_arg_capped(cap, monkeypatch):
    monkeypatch.setenv("AVRONE_LOG_ARG_MAX_CHARS", "1000")
    lg, s = cap
    lg.warning("stderr: %s", "A" * 50_000)
    out = s.getvalue()
    assert len(out) < 1200 and "log arg truncated: 50000 chars" in out


def test_huge_object_capped(cap, monkeypatch):
    monkeypatch.setenv("AVRONE_LOG_ARG_MAX_CHARS", "500")
    lg, s = cap
    lg.warning("x %s", ["B" * 10_000])
    assert len(s.getvalue()) < 800


@pytest.mark.parametrize("raw,want", [("", 65536), ("junk", 65536), ("1", 256), ("99999999999", 4 * 1024 * 1024),
                                      ("-5", 256), (" 2048 ", 2048)])
def test_arg_chars_clamped(monkeypatch, raw, want):
    monkeypatch.setenv("AVRONE_LOG_ARG_MAX_CHARS", raw)
    assert logsafe.arg_chars() == want


def test_secret_split_by_cap_still_redacted(cap, monkeypatch):
    monkeypatch.setenv("AVRONE_LOG_ARG_MAX_CHARS", "300")
    lg, s = cap
    tok = "ghp_" + "a" * 36
    lg.warning("x %s", "p" * 250 + " token " + tok + " tail" * 10)
    assert tok not in s.getvalue()


def test_secret_in_object_arg_redacted(cap):
    tok = "ghp_" + "b" * 36

    class Holder:
        def __str__(self):
            return f"Authorization: Bearer {tok}"
    lg, s = cap
    lg.warning("h=%s", Holder())
    assert tok not in s.getvalue()


# ------------------------------------------------------------ untouched paths
def test_other_loggers_untouched():
    rec = logging.getLogRecordFactory()("uvicorn", logging.INFO, "f", 1, "x %s", (EVIL,), None)
    assert rec.args == (EVIL,)


def test_clean_args_passthrough_shapes():
    assert logsafe.clean_args(None) is None
    assert logsafe.clean_args([EVIL]) == [EVIL]  # not a tuple/dict: left for logging to handle


def test_install_idempotent():
    f = logging.getLogRecordFactory()
    logsafe.install()
    assert logging.getLogRecordFactory() is f


def test_shown_repr_and_str_differ():
    sh = logsafe._Shown(PurePosixPath("a\nb"))
    assert "\n" not in str(sh) and "\n" not in repr(sh) and "PurePosixPath" in repr(sh)
