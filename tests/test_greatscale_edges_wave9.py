"""Wave 9: flake_probe evidence channel plus the h+runs tool split.

Checked against greatscale.py and tools_flakeprobe.py from main before push.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from avrone import greatscale as gs
from avrone import tools_flakeprobe as fp


class _Reg:
    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    async def call(self, ctx, name, args):
        self.calls.append((name, args))
        return await self.fn(args)

    def get(self, name):
        return type("T", (), {"fn": self.fn})()


def _ctx(reg):
    return type("Ctx", (), {"core": type("Core", (), {"tools": reg})()})()


def test_digest_is_12_hex_and_stable():
    d = fp._digest("same")
    assert d == fp._digest("same")
    assert len(d) == 12
    assert d != fp._digest("other")


def test_first_diff_line_positions():
    assert fp._first_diff_line("a\nb", "a\nb") == 3
    assert fp._first_diff_line("a\nb", "a\nc") == 2
    assert fp._first_diff_line("", "x") == 1
    assert fp._first_diff_line("a\nb\nc", "a") == 2


@pytest.mark.asyncio
async def test_probe_identical_outputs_sit_on_cone_floor():
    async def fn(args):
        return "ok"

    rep = await fp.flake_probe(None, _Reg(fn), "great_scale", 6, {"h": 0})
    assert rep["runs"] == 6
    assert rep["differing_runs"] == 0
    assert rep["distinct_outputs"] == 1
    g = rep["great_scale"]
    assert g["failures"] == 0 and g["runs"] == 6
    assert g["onConeFloor"] is True
    assert g["orientation"] == "pass"
    assert g["S"] == 0.0
    assert g["runsFor95"] is None


@pytest.mark.asyncio
async def test_probe_one_difference_is_mixed_heat():
    n = {"i": 0}

    async def fn(args):
        n["i"] += 1
        return "A" if n["i"] == 1 else "B"

    rep = await fp.flake_probe(None, _Reg(fn), "env_report", 4, {})
    assert rep["differing_runs"] == 3
    assert rep["distinct_outputs"] == 2
    g = rep["great_scale"]
    assert g["failures"] == 3 and g["runs"] == 4
    assert g["orientation"] == "mixed"
    assert g["onConeFloor"] is False
    assert g["S"] == pytest.approx((3 / 4) * (1 / 4))
    assert rep["diffs"][0]["run"] == 2
    assert rep["diffs"][0]["first_diff_line"] == 1


@pytest.mark.asyncio
async def test_probe_deepcopies_args_between_runs():
    seen = []

    async def fn(args):
        seen.append(dict(args))
        args["mutated"] = True
        return "x"

    await fp.flake_probe(None, _Reg(fn), "self_scan", 3, {"k": 1})
    assert seen == [{"k": 1}, {"k": 1}, {"k": 1}]


@pytest.mark.asyncio
async def test_probe_counts_errors_and_timeouts():
    n = {"i": 0}

    async def fn(args):
        n["i"] += 1
        if n["i"] == 1:
            return "ERROR: boom"
        if n["i"] == 2:
            await asyncio.sleep(1)
            return "late"
        return "ok"

    rep = await fp.flake_probe(None, _Reg(fn), "memory_peek", 3, {}, run_timeout=0.05, budget=5)
    assert rep["errors"] == 1
    assert rep["timeouts"] == 1
    assert rep["runs"] == 3
    timeout_diffs = [d for d in rep["diffs"] if d["first_diff_line"] is None]
    assert timeout_diffs and timeout_diffs[0]["run"] == 2


@pytest.mark.asyncio
async def test_probe_budget_stops_after_first_when_budget_already_spent():
    async def fn(args):
        return "ok"

    rep = await fp.flake_probe(None, _Reg(fn), "codex_stats", 8, {}, budget=0)
    assert rep["runs"] == 1
    assert rep["runs_requested"] == 8
    assert rep["budget_hit"] is True
    assert rep["great_scale"]["runs"] == 1


@pytest.mark.asyncio
async def test_probe_non_string_output_is_stringified():
    async def fn(args):
        return {"a": 1}

    rep = await fp.flake_probe(None, _Reg(fn), "inspect_tool", 2, {})
    assert rep["differing_runs"] == 0
    assert rep["outputs"][0]["chars"] == len(str({"a": 1}))


@pytest.mark.asyncio
async def test_probe_hides_diffs_past_the_list_cap():
    n = {"i": 0}

    async def fn(args):
        n["i"] += 1
        return f"v{n['i']}"

    rep = await fp.flake_probe(None, _Reg(fn), "capabilities_report", 25, {})
    assert rep["differing_runs"] == 24
    assert len(rep["diffs"]) == fp.FLAKE_PROBE_MAX_LISTED
    assert rep["diffs_hidden"] == 4


@pytest.mark.asyncio
async def test_tool_rejects_before_registry():
    assert (await fp.t_flake_probe(None, {})).startswith("ERROR")
    assert "recursion" in await fp.t_flake_probe(None, {"tool": "flake_probe"})
    bad = await fp.t_flake_probe(None, {"tool": "shell"})
    assert "allow-list" in bad
    assert (await fp.t_flake_probe(None, {"tool": "great_scale", "runs": True})).startswith("ERROR")
    assert (await fp.t_flake_probe(None, {"tool": "great_scale", "runs": 0})).startswith("ERROR")
    assert (await fp.t_flake_probe(None, {"tool": "great_scale", "runs": 51})).startswith("ERROR")
    assert (await fp.t_flake_probe(None, {"tool": "great_scale", "args": [1]})).startswith("ERROR")
    huge = await fp.t_flake_probe(None, {"tool": "great_scale", "args": {"x": "y" * 5000}})
    assert "longer than" in huge


@pytest.mark.asyncio
async def test_tool_blank_runs_defaults_and_json_carries_snapshot():
    async def fn(args):
        return "same"

    ctx = _ctx(_Reg(fn))
    raw = await fp.t_flake_probe(ctx, {"tool": "great_scale", "runs": "", "format": "json", "args": {"h": 0}})
    data = json.loads(raw)
    assert data["runs"] == 5
    assert data["great_scale"]["onConeFloor"] is True
    assert data["great_scale"]["orientation"] == "pass"
    assert "same" not in raw


@pytest.mark.asyncio
async def test_tool_text_names_nondeterministic():
    n = {"i": 0}

    async def fn(args):
        n["i"] += 1
        return "A" if n["i"] == 1 else "B"

    ctx = _ctx(_Reg(fn))
    text = await fp.t_flake_probe(ctx, {"tool": "env_report", "runs": 3})
    assert "NONDETERMINISTIC (2/3" in text
    assert "orientation: mixed" in text
    assert "content never shown" in text
    assert "B" not in text


@pytest.mark.asyncio
async def test_tool_missing_registry():
    ctx = type("Ctx", (), {"core": type("Core", (), {"tools": None})()})()
    out = await fp.t_flake_probe(ctx, {"tool": "great_scale"})
    assert "registry is not available" in out


@pytest.mark.asyncio
async def test_h_is_ignored_when_runs_is_also_set():
    out = await gs.t_great_scale(None, {"h": 0.5, "failures": 0, "runs": 4})
    assert out.startswith("great_scale evidence: 0/4")
    assert "deterministic pass" in out


@pytest.mark.asyncio
async def test_format_none_stays_text():
    out = await gs.t_great_scale(None, {"failures": 1, "runs": 2, "format": None})
    assert out.startswith("great_scale evidence:")


@pytest.mark.asyncio
async def test_alpha_one_is_error_string():
    out = await gs.t_great_scale(None, {"failures": 1, "runs": 2, "alpha": 1})
    assert out.startswith("ERROR")
    assert "Traceback" not in out
