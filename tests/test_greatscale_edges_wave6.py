"""Wave 6 edge cases against the live greatscale.py contract.

Locked to functions as they exist on main (heat clamp, evidence_snapshot
int coercion, soft_contract modulo-9, tool text vs JSON split).
"""
from __future__ import annotations

import json
import math

import pytest

from avrone import greatscale as gs


# ---- soft_contract -----------------------------------------------------------

@pytest.mark.parametrize("k", [0.0, 0.25, 0.5, 1.0, 3.0])
def test_soft_contract_fixed_point(k):
    assert gs.soft_contract(gs.FIXED, k=k) == pytest.approx(gs.FIXED)


def test_soft_contract_k_zero_collapses():
    for x in (-100.0, 0.0, 1.0, 4.5, 8.999, 9.0, 18.5, 1e6):
        assert gs.soft_contract(x, k=0.0) == pytest.approx(gs.FIXED)


def test_soft_contract_period_nine():
    for x in (0.3, 1.7, 4.5, 8.2):
        assert gs.soft_contract(x) == pytest.approx(gs.soft_contract(x + 9))
        assert gs.soft_contract(x) == pytest.approx(gs.soft_contract(x - 18))


def test_soft_contract_range():
    # k=0.5 maps residue in [0,9) into [2.25, 6.75)
    for i in range(90):
        y = gs.soft_contract(i / 10)
        assert 2.25 - 1e-9 <= y < 6.75 + 1e-9


# ---- detection formula -------------------------------------------------------

def test_detect_in_runs_matches_closed_form():
    for f, r in ((0, 1), (1, 1), (1, 2), (3, 10), (9, 10), (1, 200), (50, 100)):
        snap = gs.evidence_snapshot(f, r)
        p = f / r
        assert snap["detectInRuns"] == pytest.approx(1 - (1 - p) ** r)
        assert snap["singleRunDetect"] == p


def test_runs_for_95_meets_confidence():
    for p in (0.5, 0.2, 0.1, 0.01):
        n = gs.runs_to_detect(p, 0.05)
        assert n is not None
        assert 1 - (1 - p) ** n >= 0.95 - 1e-9
        if n > 1:
            assert 1 - (1 - p) ** (n - 1) < 0.95 + 1e-9


def test_rare_defect_needs_many_runs():
    # p = 1/200 is the v0.21 corollary: ~598 runs for 95%.
    n = gs.runs_to_detect(1 / 200, 0.05)
    assert n is not None and 500 <= n <= 700


def test_alpha_near_edges():
    assert gs.runs_to_detect(0.5, 1e-9) > gs.runs_to_detect(0.5, 0.5)
    with pytest.raises(ValueError):
        gs.evidence_snapshot(1, 10, alpha=0)
    with pytest.raises(ValueError):
        gs.evidence_snapshot(1, 10, alpha=1)


# ---- saturation extremes -----------------------------------------------------

def test_saturation_tau_pm_one_is_floor():
    for tau in (-1.0, 1.0):
        out = gs.from_topological_saturation(tau)
        assert out["S"] == pytest.approx(0.0)
        assert out["psi"] == pytest.approx(gs.CONE)
        assert out["coneOk"] is True
        assert out["logCoord"] == pytest.approx(0.0)


def test_saturation_tau_zero_is_peak():
    out = gs.from_topological_saturation(0.0)
    assert out["S"] == pytest.approx(0.25)
    assert out["logCoord"] == pytest.approx(1.0)


def test_saturation_outside_unit_interval_still_finite():
    # tau is not clamped; S = (1 - tau^2)/4 can be negative.
    out = gs.from_topological_saturation(2.0)
    assert out["S"] == pytest.approx((1 - 4) / 4)
    assert math.isfinite(out["psi"])
    assert out["coneOk"] is False


# ---- heat clamp and snapshot coercion ----------------------------------------

def test_heat_clamps_outside_unit():
    assert gs.heat(-5) == 0.0
    assert gs.heat(5) == 0.0
    assert gs.heat(0) == 0.0
    assert gs.heat(1) == 0.0
    assert gs.heat(0.5) == 0.25


def test_snapshot_coerces_whole_floats():
    # bools are rejected; plain floats are truncated via int().
    snap = gs.evidence_snapshot(1.9, 10.2)
    assert snap["failures"] == 1 and snap["runs"] == 10


def test_max_runs_boundary_accepted():
    snap = gs.evidence_snapshot(0, gs.MAX_RUNS)
    assert snap["runs"] == gs.MAX_RUNS
    assert snap["onConeFloor"] is True
    assert snap["runsFor95"] is None


# ---- tool format split -------------------------------------------------------

@pytest.mark.asyncio
async def test_h_mode_is_always_json():
    for h in (0, 0.5, 1):
        raw = await gs.t_great_scale(None, {"h": h})
        data = json.loads(raw)
        assert data["h"] == h
        assert data["S"] == pytest.approx(gs.heat(h))
        assert data["coneOk"] is True


@pytest.mark.asyncio
async def test_evidence_text_vs_json():
    text = await gs.t_great_scale(None, {"failures": 2, "runs": 8})
    assert text.startswith("great_scale evidence: 2/8")
    assert "orientation: mixed" in text
    js = await gs.t_great_scale(None, {"failures": 2, "runs": 8, "format": "JSON"})
    data = json.loads(js)
    assert data["failures"] == 2 and data["orientation"] == "mixed"
    assert list(data) == sorted(data)


@pytest.mark.asyncio
async def test_tool_default_failures_zero_needs_runs():
    out = await gs.t_great_scale(None, {"runs": 7})
    assert "0/7 failed" in out
    assert "deterministic pass" in out


@pytest.mark.asyncio
async def test_tool_rejects_float_counts():
    out = await gs.t_great_scale(None, {"failures": 1.0, "runs": 4})
    assert out.startswith("ERROR")


# ---- cascade on partition ----------------------------------------------------

def test_partition_lambdas_multiply_to_phi_inverse_eight():
    # blocks 4+3+1 = 8, product of lambdas is phi^-8
    prod = 1.0
    for block in gs.RESIDUAL_PARTITION:
        prod *= gs.cascade_lambda(block)
    assert prod == pytest.approx(gs.PHI ** -8)
    assert sum(gs.RESIDUAL_PARTITION) == 8
