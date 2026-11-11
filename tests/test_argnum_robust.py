"""Hardening round 45: integer tool arguments (inf/NaN from model JSON, Unicode digits, "1_000",
superscripts) and the Content-Length superscript crash."""
from __future__ import annotations

import asyncio
import math
import pathlib
import types

import pytest

from avrone.argnum import ascii_int, tool_int
from avrone.gateway import _parse_args

INF, NAN = float("inf"), float("nan")


# ------------------------------------------------------------ the helper
@pytest.mark.parametrize("v,want", [
    (7, 7), (" 7 ", 7), ("+7", 7), ("-3", 1), (7.9, 7), ("2.0", 2), ("1e1", 10),
    (None, 5), ("", 5), ("   ", 5), (10**30, 20), (-10**30, 1),
    (INF, 20), (-INF, 1), ("1e999", 20), ("-1e999", 1),
])
def test_accepted(v, want):
    assert tool_int(v, 5, 1, 20) == want


@pytest.mark.parametrize("v", [
    NAN, True, False, "\u00b2", "\u0663", "\uff17", "1_000", "12abc", "0x10", "nan", "inf",
    "Infinity", "--1", "1.2.3", [1], {"a": 1}, b"7", object(), "7\n8", "\u20077",
])
def test_refused(v):
    assert tool_int(v, 5, 1, 20) == 5
    assert tool_int(v, 5, 1, 20, bad=None) is None


def test_inf_without_bound_is_bad():
    assert tool_int(INF, 5, bad=None) is None
    assert tool_int(-INF, 5, lo=1, bad=None) == 1


def test_never_raises_on_huge_digit_strings():
    assert tool_int("9" * 5000, 5, 1, 20) == 5  # over 18 digits: not an int text, not a float text
    assert tool_int("9" * 18, 5, 1, 20) == 20


@pytest.mark.parametrize("t,want", [("12", 12), (" -4 ", -4), ("\u00b2", None), ("\u0663", None),
                                    ("1_0", None), ("", None), (5, None)])
def test_ascii_int(t, want):
    assert ascii_int(t) == want


def test_model_json_really_delivers_inf():
    args, ok = _parse_args('{"limit": 1e999}')
    assert ok and math.isinf(args["limit"])
    args, ok = _parse_args('{"limit": Infinity, "n": NaN}')
    assert math.isinf(args["limit"]) and math.isnan(args["n"])


# ------------------------------------------------------------ the tools' helpers
@pytest.mark.parametrize("mod", ["tools_extra", "tools_codexstats", "tools_fleetstatus", "tools_mempeek"])
@pytest.mark.parametrize("v", [INF, -INF, NAN, "1e999", "\u00b2", "\u0663", True, "1_000"])
def test_module_int_helpers_never_raise(mod, v):
    import importlib
    m = importlib.import_module(f"avrone.{mod}")
    out = m._int(v, 5, 1, 20)
    assert out is None or 1 <= out <= 20


def test_tools_extra_int_inf_clamps():
    from avrone.tools_extra import _int
    assert _int(INF, 5, 1, 20) == 20 and _int(NAN, 5, 1, 20) == 5


def test_codexstats_junk_reports_error_not_default():
    from avrone.tools_codexstats import _int
    assert _int("\u00b2", 10, 0, 100) is None and _int(None, 10, 0, 100) == 10


# ------------------------------------------------------------ tools end to end
def _ctx(tmp_path):
    settings = types.SimpleNamespace(data_dir=tmp_path)
    core = types.SimpleNamespace(settings=settings, codex=None)
    return types.SimpleNamespace(core=core, run_id="r1", name="a", settings=settings)


@pytest.mark.parametrize("v", [INF, NAN, "\u00b2", "1e999", True])
def test_lesson_recall_limit(tmp_path, v):
    from avrone.tools_lessonrecall import t_lesson_recall
    out = asyncio.run(t_lesson_recall(_ctx(tmp_path), {"topic": "x", "limit": v}))
    assert isinstance(out, str)


@pytest.mark.parametrize("v", ["\u00b2", NAN, True, [1]])
def test_lesson_recall_bad_limit_message(tmp_path, v):
    from avrone.tools_lessonrecall import t_lesson_recall
    out = asyncio.run(t_lesson_recall(_ctx(tmp_path), {"topic": "x", "limit": v}))
    assert out.startswith("ERROR: limit")


@pytest.mark.parametrize("port", ["abc", NAN, INF, "1e999", True, 443.5, "\u00b2"])
def test_tcp_ping_bad_port_is_an_error_not_an_exception(port):
    from avrone.netdiag import t_tcp_ping
    out = asyncio.run(t_tcp_ping(types.SimpleNamespace(), {"host": "example.com", "port": port}))
    assert out.startswith("ERROR")


def test_tcp_ping_out_of_range_port():
    from avrone.netdiag import t_tcp_ping
    out = asyncio.run(t_tcp_ping(types.SimpleNamespace(), {"host": "example.com", "port": 70000}))
    assert "out of range" in out


def test_netdiag_source_uses_helper():
    src = (pathlib.Path(__file__).resolve().parents[1] / "avrone" / "netdiag.py").read_text()
    assert ' int(args.get("count"' not in src and "min(int(args.get(" not in src


@pytest.mark.parametrize("v", [INF, NAN, "\u00b2", "1e999"])
def test_log_health_runs_arg(tmp_path, v):
    from avrone.tools_limits import t_log_health
    ctx = _ctx(tmp_path)
    out = asyncio.run(t_log_health(ctx, {"runs": v}))
    assert isinstance(out, str)


# ------------------------------------------------------------ server Content-Length
@pytest.mark.parametrize("value,over", [
    (b"\xb2", False), (b"\xb3", False), (b"\xb9", False), (b"99999999999", True), (b" 10 ", False),
    (b"+10", False), (b"-1", False), (b"zz", False), (b"\xb2\xb2\xb2\xb2\xb2\xb2\xb2\xb2\xb2\xb2", False),
])
def test_content_length_superscript_never_raises(value, over):
    from avrone.server import _content_length_over
    assert _content_length_over([(b"content-length", value)], 1000) is over


def test_content_length_mixed_headers():
    from avrone.server import _content_length_over
    assert _content_length_over([(b"content-length", b"\xb2"), (b"Content-Length", b"5000")], 1000)


def test_no_isdigit_then_int_on_untrusted_text():
    """`str.isdigit()` is True for superscripts that int() refuses. Validators use argnum or an
    ASCII check."""
    root = pathlib.Path(__file__).resolve().parents[1] / "avrone"
    for name in ("server.py", "tools_lessonrecall.py"):
        src = (root / name).read_text()
        for line in src.splitlines():
            code = line.split("#", 1)[0]
            if ".isdigit()" in code and "int(" in code:
                assert "isascii()" in line, (name, line)
