"""Wave 8: format whitespace, inf saturation, bool heat, alpha NaN, fail-N=1.

Checked against avrone/greatscale.py from main before push.
"""
from __future__ import annotations

import json
import math

import pytest

from avrone import greatscale as gs


def test_residual_count_endpoints():
    assert gs.residual_count(0.0) == 8.0
    assert gs.residual_count(0.25) == 7.0


def test_heat_of_bool_follows_float_cast():
    # bool is not rejected by heat(); True becomes 1.0 and sits on the floor.
    assert gs.heat(True) == 0.0
    assert gs.heat(False) == 0.0


def test_snapshot_rejects_bools_even_though_int_subclass():
    with pytest.raises(ValueError):
        gs.evidence_snapshot(True, 1)
    with pytest.raises(ValueError):
        gs.evidence_snapshot(0, True)


def test_fail_orientation_needs_one_run_for_95():
    snap = gs.evidence_snapshot(3, 3)
    assert snap["orientation"] == "fail"
    assert snap["onConeFloor"] is True
    assert snap["runsFor95"] == 1
    assert snap["detectInRuns"] == 1.0


def test_pass_orientation_has_no_detection_budget():
    snap = gs.evidence_snapshot(0, 3)
    assert snap["runsFor95"] is None
    assert snap["detectInRuns"] == 0.0


def test_saturation_inf_is_not_finite():
    out = gs.from_topological_saturation(float("inf"))
    assert math.isinf(out["S"]) or out["S"] != out["S"] or out["S"] < 0
    assert out["coneOk"] is False


def test_snapshot_nan_alpha_raises():
    with pytest.raises(ValueError):
        gs.evidence_snapshot(1, 4, alpha=float("nan"))


def test_snapshot_truncates_toward_zero():
    snap = gs.evidence_snapshot(2.9, 10.9)
    assert snap["failures"] == 2 and snap["runs"] == 10


def test_soft_contract_large_k_stays_finite():
    y = gs.soft_contract(1.25, k=1e6)
    assert math.isfinite(y)
    assert y != gs.FIXED


@pytest.mark.asyncio
async def test_format_json_with_trailing_space_stays_text():
    out = await gs.t_great_scale(None, {"failures": 1, "runs": 4, "format": "json "})
    assert out.startswith("great_scale evidence:")
    assert not out.startswith("{")


@pytest.mark.asyncio
async def test_empty_format_is_text():
    out = await gs.t_great_scale(None, {"failures": 1, "runs": 4, "format": ""})
    assert "residual heat present" in out


@pytest.mark.asyncio
async def test_json_on_cone_floor_flag():
    raw = await gs.t_great_scale(None, {"failures": 0, "runs": 9, "format": "json"})
    data = json.loads(raw)
    assert data["onConeFloor"] is True
    assert data["orientation"] == "pass"
    assert data["runsFor95"] is None


@pytest.mark.asyncio
async def test_h_integer_endpoints():
    for h in (0, 1):
        data = json.loads(await gs.t_great_scale(None, {"h": h}))
        assert data["S"] == 0.0
        assert data["coneOk"] is True


@pytest.mark.asyncio
async def test_alpha_negative_is_error_not_exception():
    out = await gs.t_great_scale(None, {"failures": 1, "runs": 4, "alpha": -0.1})
    assert out.startswith("ERROR")
    assert "Traceback" not in out


@pytest.mark.asyncio
async def test_h_string_is_error():
    out = await gs.t_great_scale(None, {"h": "0.5"})
    assert out.startswith("ERROR")
    assert "h must be a number" in out


@pytest.mark.asyncio
async def test_runs_only_missing_is_error_when_neither():
    out = await gs.t_great_scale(None, {"failures": 1})
    assert out.startswith("ERROR")


def test_runs_to_detect_p_above_one_raises():
    with pytest.raises(ValueError):
        gs.runs_to_detect(1.0000001, 0.05)


def test_one_run_mixed_is_impossible():
    # With one run the only legal counts are 0 or 1, both on the floor.
    assert gs.evidence_snapshot(0, 1)["orientation"] == "pass"
    assert gs.evidence_snapshot(1, 1)["orientation"] == "fail"
    with pytest.raises(ValueError):
        gs.evidence_snapshot(2, 1)
