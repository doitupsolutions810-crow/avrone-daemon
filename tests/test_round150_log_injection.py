"""Round 150: text from outside in log arguments can't forge server log lines.

On v0.142, the log lines of avrone.* modules put outside text into messages raw. That text
included an MCP server's name, protocol version, tool names and stderr, a fleet peer's error, a
plugin's exception, and file names. A "\\n" wrote a fake log line, and ESC, "\\r" or U+202E reached
the operator's terminal. String and exception arguments of avrone.* records are now escaped.
Message templates, tracebacks and other libraries' loggers are left alone.
"""
from __future__ import annotations

import io
import logging

import pytest

import avrone  # noqa: F401  installs the record factory


@pytest.fixture()
def cap():
    s = io.StringIO()
    h = logging.StreamHandler(s)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    lg = logging.getLogger("avrone.r150")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    yield lg, s
    lg.removeHandler(h)


def test_args_escaped(cap):
    lg, s = cap
    lg.warning("MCP server %s negotiated unknown protocol %s", "srv\nWARNING avrone.fleet trusted", "1\u202e\x1b[2J")
    out = s.getvalue()
    assert out.count("\n") == 1, out
    assert "\u202e" not in out and "\x1b" not in out and "srv\\x0aWARNING" in out


def test_exception_arg_escaped_and_ints_kept(cap):
    lg, s = cap
    lg.warning("heartbeat to %s failed (%d): %s", "p", 3, ValueError("bad\r\nFAKE"))
    out = s.getvalue()
    assert out.count("\n") == 1 and "(3)" in out and "bad\\x0d\\x0aFAKE" in out


def test_fstring_message_escaped_template_kept(cap):
    lg, s = cap
    lg.warning(f"file {'a' + chr(10) + 'FAKE'} gone")
    lg.warning("two-line template\nsecond %s", "x")
    out = s.getvalue()
    assert "a\\x0aFAKE" in out and "two-line template\nsecond x" in out


def test_traceback_untouched(cap):
    lg, s = cap
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        lg.exception("failed %s", "t")
    out = s.getvalue()
    assert "Traceback (most recent call last):\n" in out and "RuntimeError: boom" in out


def test_other_loggers_untouched():
    rec = logging.getLogger("thirdparty.r150").makeRecord("thirdparty.r150", logging.INFO, "f", 1, "%s", ("a\nb",), None)
    assert rec.args == ("a\nb",)


def test_logsafe_installed_once():
    from avrone import logsafe
    logsafe.install()
    logsafe.install()
    rec = logging.getLogRecordFactory()("avrone.x", logging.INFO, "f", 1, "%s", ("a\nb",), None)
    assert rec.getMessage() == "a\\x0ab"


def test_plugin_failure_logs_traceback(caplog):
    from avrone.plugins import _run_register
    from avrone.tools import ToolRegistry

    def register(registry, settings):
        raise ValueError("bad\nWARNING avrone fake")

    with caplog.at_level(logging.WARNING, logger="avrone"):
        _run_register("evil\nplug", "file", register, ToolRegistry(), None)
    recs = [r for r in caplog.records if "register() failed" in r.getMessage()]
    assert recs and "\n" not in recs[0].getMessage() and recs[0].exc_info
