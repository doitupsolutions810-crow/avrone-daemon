"""Hardening round 47: flake_probe edge cases (run counts, timeout sentinel forgery, raising and
non-text runs, budget overrun, line-ending diffs, args JSON)."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone import tools_flakeprobe as fp
from avrone.tools import Tool, builtin_registry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)))
    ctx = SimpleNamespace(core=core, run_id="run1", name="W", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx)


def call(env, **args):
    return run(fp.t_flake_probe(env.ctx, args))


def fake(env, name, fn):
    env.reg.register(Tool(name, "fake", "{}", fn, source="builtin"))


def seq_tool(env, outs, name="self_scan"):
    it = iter(outs)

    async def f(ctx, args):
        return next(it)
    fake(env, name, f)


class DuckReg:
    """A registry that does not catch: exercises the probe's own guards."""

    def __init__(self, fn):
        self.fn = fn

    async def call(self, ctx, name, args):
        return await self.fn()


# ------------------------------------------------------------ runs
@pytest.mark.parametrize("runs", [float("inf"), float("-inf"), float("nan"), "1e999", 2.7, True, False,
                                  "1_0", "\u00b2", "\u0663", [3], {"n": 3}, "abc"])
def test_bad_runs_refused_never_raise(env, runs):
    out = call(env, tool="great_scale", runs=runs)
    assert out.startswith("ERROR: runs"), out


@pytest.mark.parametrize("runs,n", [(3, 3), ("3", 3), (" 4 ", 4), (2.0, 2), ("2.0", 2), (None, 5), ("", 5)])
def test_good_runs(env, runs, n):
    rep = json.loads(call(env, tool="great_scale", runs=runs, format="json"))
    assert rep["runs"] == n


@pytest.mark.parametrize("runs", [0, -1, 51, 10**30, "51"])
def test_out_of_range_runs(env, runs):
    assert "runs must be 1..50" in call(env, tool="great_scale", runs=runs)


# ------------------------------------------------------------ the timeout sentinel
def test_output_equal_to_old_timeout_mark_is_not_a_timeout(env):
    seq_tool(env, [fp._TIMEOUT_MARK, fp._TIMEOUT_MARK])
    rep = json.loads(call(env, tool="self_scan", runs=2, format="json"))
    assert rep["timeouts"] == 0 and rep["outputs"][0]["digest"] != "timeout"


def test_str_subclass_output_cannot_pose_as_timeout(env):
    seq_tool(env, [fp._Timeout(fp._TIMEOUT_MARK), "x"])
    rep = json.loads(call(env, tool="self_scan", runs=2, format="json"))
    assert rep["timeouts"] == 0


def test_real_timeouts_counted():
    async def slow():
        await asyncio.sleep(5)
    rep = run(fp.flake_probe(None, DuckReg(slow), "t", 2, {}, run_timeout=0.05, budget=10))
    assert rep["timeouts"] == 2 and rep["differing_runs"] == 0
    assert rep["outputs"][0]["digest"] == "timeout"


def test_timeout_vs_output_differ():
    calls = iter([None, "ok"])

    async def f():
        v = next(calls)
        if v is None:
            await asyncio.sleep(5)
        return v
    rep = run(fp.flake_probe(None, DuckReg(f), "t", 2, {}, run_timeout=0.05, budget=10))
    assert rep["differing_runs"] == 1 and rep["diffs"][0]["first_diff_line"] is None


# ------------------------------------------------------------ raising and odd outputs
def test_raising_run_is_an_error_not_a_crash():
    n = iter(range(10))

    async def f():
        if next(n) == 1:
            raise RuntimeError("boom")
        return "ok"
    rep = run(fp.flake_probe(None, DuckReg(f), "t", 3, {}))
    assert rep["runs"] == 3 and rep["errors"] == 1 and rep["differing_runs"] == 1


def test_raising_first_run():
    async def f():
        raise ValueError("x")
    rep = run(fp.flake_probe(None, DuckReg(f), "t", 2, {}))
    assert rep["errors"] == 2 and rep["differing_runs"] == 0


@pytest.mark.parametrize("out", [b"bytes \xff", bytearray(b"ba"), 42, None, {"a": 1}, 1.5])
def test_non_text_outputs(out):
    async def f():
        return out
    rep = run(fp.flake_probe(None, DuckReg(f), "t", 2, {}))
    assert rep["runs"] == 2 and rep["differing_runs"] == 0


def test_hostile_str_output():
    class Bad:
        def __str__(self):
            raise RuntimeError("no")

    async def f():
        return Bad()
    rep = run(fp.flake_probe(None, DuckReg(f), "t", 2, {}))
    assert rep["errors"] == 2


def test_arg_error_counted_as_error(env):
    seq_tool(env, ["ARG ERROR (x): bad", "ARG ERROR (x): bad"])
    rep = json.loads(call(env, tool="self_scan", runs=2, format="json"))
    assert rep["errors"] == 2


def test_lone_surrogate_output_digested():
    async def f():
        return "a\ud800b"
    rep = run(fp.flake_probe(None, DuckReg(f), "t", 2, {}))
    assert rep["differing_runs"] == 0 and len(rep["outputs"][0]["digest"]) == 12


# ------------------------------------------------------------ budget
def test_budget_not_overrun_by_a_full_run_timeout():
    async def slow():
        await asyncio.sleep(10)
    t = time.monotonic()
    rep = run(fp.flake_probe(None, DuckReg(slow), "t", 5, {}, run_timeout=1.0, budget=1.2))
    took = time.monotonic() - t
    assert took < 1.9, took  # used to be ~2.0 s: the second run got a full 1 s
    assert rep["runs"] == 2 and rep["timeouts"] == 2


def test_budget_stops_runs():
    async def quick():
        await asyncio.sleep(0.03)
        return "x"
    rep = run(fp.flake_probe(None, DuckReg(quick), "t", 50, {}, run_timeout=1, budget=0.1))
    assert rep["budget_hit"] and rep["runs"] < 50


# ------------------------------------------------------------ first diff line
@pytest.mark.parametrize("a,b,line", [
    ("x\r\ny", "x\ny", 1), ("x", "x\n", 1), ("a\nb", "a\nc", 2), ("a", "a\nb", 2), ("", "x", 1),
    ("a\nb\n", "a\nb\r\n", 2), ("a\nb", "a\nb\nc\nd", 3),
])
def test_first_diff_line(a, b, line):
    assert fp._first_diff_line(a, b) == line
    assert fp._first_diff_line(b, a) == line


@pytest.mark.parametrize("a,b", [("x\r\ny", "x\ny"), ("x", "x\n"), ("a\u2028b", "a\nb"), ("", "\n")])
def test_first_diff_line_never_past_both_ends(a, b):
    n = fp._first_diff_line(a, b)
    assert 1 <= n <= max(len(a.splitlines()), len(b.splitlines()), 1)


# ------------------------------------------------------------ args
@pytest.mark.parametrize("args", [{"x": float("nan")}, {"x": float("inf")}, {1: "a"}, {None: 1},
                                  {"x": {2: "b"}}])
def test_non_json_args_refused(env, args):
    out = call(env, tool="great_scale", runs=2, args=args)
    assert out.startswith("ERROR: args"), out


def test_nested_non_str_key_refused_by_dumps(env):
    out = call(env, tool="great_scale", runs=2, args={"x": {(1, 2): "b"}})
    assert out.startswith("ERROR: args")


def test_deep_args_refused(env):
    d = {}
    cur = d
    for _ in range(5000):
        cur["a"] = {}
        cur = cur["a"]
    assert call(env, tool="great_scale", runs=2, args=d).startswith("ERROR: args")


@pytest.mark.parametrize("fmt", ["json", "JSON", " json ", "Json\n"])
def test_format_json_spellings(env, fmt):
    json.loads(call(env, tool="great_scale", runs=2, format=fmt))


@pytest.mark.parametrize("tool", ["\uff49nspect_tool", "inspect_tool\u200b", "INSPECT_TOOL", {"x": 1}, 5])
def test_lookalike_tool_names_refused(env, tool):
    assert call(env, tool=tool).startswith("ERROR")


def test_output_content_never_echoed(env):
    seq_tool(env, ["secret-a", "secret-b"])
    out = call(env, tool="self_scan", runs=2)
    assert "secret" not in out
