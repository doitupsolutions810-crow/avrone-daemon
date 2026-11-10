"""Wave 7: coercion, overflow path, saturation NaN, and tool key edges.

Checked against avrone/greatscale.py on main before push.
"""
from __future__ import annotations

import json
import math

import pytest

from avrone import greatscale as gs


def test_cascade_lambda_zero_is_one():
    assert gs.cascade_lambda(0) == pytest.approx(1.0)


def test_cascade_lambda_negative_grows():
    assert gs.cascade_lambda(-2) == pytest.approx(gs.PHI ** 2)
    assert gs.cascade_lambda(-2) > 1


def test_log_coordinate_negative_residual():
    assert gs.log_coordinate(-0.25) == pytest.approx(-1.0)


def test_psi_tracks_s_one_to_one():
    for s in (-0.5, 0.0, 0.1, 0.25, 1.0):
        assert gs.psi(s) - gs.CONE == pytest.approx(s)


def test_cone_compliant_false_only_below_floor():
    assert gs.cone_compliant(0.0) is True
    assert gs.cone_compliant(-1e-9) is False
    assert gs.cone_compliant(-1e-15) is True  # tolerance is 1e-12


def test_atlas_unknown_block_is_none():
    assert gs.atlas_role(2) is None
    assert gs.atlas_role(0) is None
    assert gs.atlas_role(99) is None


def test_saturation_nan_is_not_compliant():
    out = gs.from_topological_saturation(float("nan"))
    assert out["S"] != out["S"]
    assert out["coneOk"] is False


def test_half_split_is_max_heat():
    snap = gs.evidence_snapshot(1, 2)
    assert snap["p"] == 0.5
    assert snap["S"] == 0.25
    assert snap["logCoord"] == 1.0
    assert snap["orientation"] == "mixed"
    assert snap["onConeFloor"] is False
    assert snap["runsFor95"] == 5  # 1-(1/2)^4 = 0.9375 < 0.95; ^5 = 0.96875


def test_snapshot_coerces_digit_strings_and_rejects_words():
    # int() accepts digit strings; words still raise.
    snap = gs.evidence_snapshot("2", "8")
    assert snap["failures"] == 2 and snap["runs"] == 8
    with pytest.raises(ValueError):
        gs.evidence_snapshot("two", 8)
    with pytest.raises(ValueError):
        gs.evidence_snapshot(1, "eight")


def test_soft_contract_nine_equals_zero():
    assert gs.soft_contract(9) == pytest.approx(gs.soft_contract(0))
    assert gs.soft_contract(-9) == pytest.approx(gs.soft_contract(0))


def test_soft_contract_negative_k_inverts_direction():
    # residue of 0 is 0, which is below FIXED, so negative k lifts it
    y = gs.soft_contract(0.0, k=-1.0)
    assert y > gs.FIXED


def test_runs_to_detect_tiny_p_is_large_but_finite():
    n = gs.runs_to_detect(1e-6, 0.05)
    assert n is not None and n > 1_000_000
    assert isinstance(n, int)


def test_runs_to_detect_high_alpha_is_one_for_coin():
    assert gs.runs_to_detect(0.5, 0.6) == 1


@pytest.mark.asyncio
async def test_tool_none_failures_is_error():
    out = await gs.t_great_scale(None, {"failures": None, "runs": 4})
    assert out.startswith("ERROR")


@pytest.mark.asyncio
async def test_tool_string_counts_are_error():
    out = await gs.t_great_scale(None, {"failures": "1", "runs": "4"})
    assert out.startswith("ERROR")


@pytest.mark.asyncio
async def test_tool_explicit_text_format():
    out = await gs.t_great_scale(None, {"failures": 0, "runs": 3, "format": "text"})
    assert out.startswith("great_scale evidence:")
    assert "deterministic pass" in out


@pytest.mark.asyncio
async def test_tool_unknown_keys_ignored():
    out = await gs.t_great_scale(None, {"failures": 4, "runs": 4, "extra": "nope"})
    assert "deterministic fail" in out
    assert "nope" not in out


@pytest.mark.asyncio
async def test_tool_overflow_alpha():
    out = await gs.t_great_scale(None, {"failures": 1, "runs": 4, "alpha": 10 ** 400})
    assert out.startswith("ERROR")
    assert "out of range" in out


@pytest.mark.asyncio
async def test_tool_overflow_h():
    out = await gs.t_great_scale(None, {"h": 10 ** 400})
    assert out.startswith("ERROR")


@pytest.mark.asyncio
async def test_h_mode_ignores_format_text():
    raw = await gs.t_great_scale(None, {"h": 0.25, "format": "text"})
    data = json.loads(raw)
    assert data["S"] == pytest.approx(0.25 * 0.75)


def test_register_adds_great_scale():
    class Reg:
        def __init__(self):
            self.tools = []

        def register(self, tool):
            self.tools.append(tool)

    reg = Reg()
    gs.register_greatscale_tools(reg)
    assert len(reg.tools) == 1
    assert reg.tools[0].name == "great_scale"
    assert reg.tools[0].source == "builtin"
    assert callable(reg.tools[0].fn)
