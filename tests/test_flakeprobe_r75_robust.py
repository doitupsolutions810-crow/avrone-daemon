"""Round 75: flake_probe argument and schema-reason hardening."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from avrone import tools_flakeprobe as fp


class Reg:
    def __init__(self, check=None, out="same"):
        self._check, self.out, self.calls = check, out, 0

    def get(self, n):
        return NS(fn=None)

    async def call(self, ctx, n, a):
        self.calls += 1
        return self.out

    def check(self, n, a):
        return self._check


def run(args, reg=None, ctx=None):
    reg = reg or Reg()
    ctx = NS(core=NS(tools=reg)) if ctx is None else ctx
    return asyncio.run(fp.t_flake_probe(ctx, args))


@pytest.mark.parametrize("args", [None, [], ["tool"], "env_report", 5, True])
def test_non_dict_args_ask_for_tool(args):
    assert run(args).startswith("ERROR: give tool")


@pytest.mark.parametrize("tool", [["env_report"], {"env_report": 1}, 5, 1.5, True, ("env_report",)])
def test_non_text_tool_refused_without_calls(tool):
    reg = Reg()
    assert run({"tool": tool}, reg).startswith("ERROR: tool must be text")
    assert reg.calls == 0


@pytest.mark.parametrize("tool", [None, "", "   "])
def test_missing_tool(tool):
    assert run({"tool": tool}).startswith("ERROR: give tool")


@pytest.mark.parametrize("fmt", [["json"], {"json": 1}, 1, True, "xml", "jsonl", "JSON5"])
def test_bad_format_refused_before_running(fmt):
    reg = Reg()
    assert run({"tool": "env_report", "runs": 2, "format": fmt}, reg) == 'ERROR: format must be "text" or "json"'
    assert reg.calls == 0


@pytest.mark.parametrize("fmt", ["json", " JSON ", "Json"])
def test_json_format_variants(fmt):
    out = run({"tool": "env_report", "runs": 2, "format": fmt})
    assert json.loads(out)["runs"] == 2


@pytest.mark.parametrize("fmt", [None, "", "text", " TEXT "])
def test_text_format_variants(fmt):
    assert run({"tool": "env_report", "runs": 2, "format": fmt}).startswith("flake_probe env_report x2")


@pytest.mark.parametrize("ctx", [NS(), NS(core=None), NS(core=NS()), object()])
def test_ctx_without_registry(ctx):
    assert run({"tool": "env_report", "runs": 1}, ctx=ctx) == "ERROR: the tool registry is not available"


@pytest.mark.parametrize("bad,why", [
    (("x", "bad\nFORGED: line"), "bad FORGED: line"),
    (("x", "a\r\nb\u2028c"), "a b c"),
    ("oops", "oops"),
    (("x",), "invalid args"),
    (True, "invalid args"),
    (("x", 5), "invalid args"),
    (["x", "listed reason"], "listed reason"),
])
def test_schema_reason_one_line(bad, why):
    reg = Reg(check=bad)
    out = run({"tool": "env_report", "runs": 1}, reg)
    assert out == f"ERROR: args rejected by env_report's schema: {why}"
    assert "\n" not in out and reg.calls == 0


def test_schema_reason_capped():
    out = run({"tool": "env_report", "runs": 1}, Reg(check=("x", "R" * 5000)))
    assert len(out) < 400


@pytest.mark.parametrize("falsy", [None, (), "", False, []])
def test_falsy_check_passes(falsy):
    assert run({"tool": "env_report", "runs": 1}, Reg(check=falsy)).startswith("flake_probe env_report x1")


def test_runs_string_number_still_ok():
    assert run({"tool": "env_report", "runs": "3"}).startswith("flake_probe env_report x3")
