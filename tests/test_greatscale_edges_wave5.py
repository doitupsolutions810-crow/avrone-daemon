"""Wave 5: boundary, tool-error, and identity edge cases for greatscale.

Complements wave 4. Covers rejection surfaces, PHI identities, orientation
channel, and the evidence contract under hostile counts.
"""
from __future__ import annotations

import json
import math

import pytest

from avrone import greatscale as gs


def test_phi_identities():
    assert gs.PHI == pytest.approx((1 + math.sqrt(5)) / 2)
    assert gs.PHI_INV == pytest.approx(gs.PHI - 1)
    assert gs.CONE == pytest.approx(gs.PHI_INV ** 2)
    assert gs.CONE == pytest.approx(1 / gs.PHI ** 2)
    # phi^2 = phi + 1
    assert gs.PHI ** 2 == pytest.approx(gs.PHI + 1)


def test_cone_floor_constant_stable():
    assert gs.CONE == pytest.approx(0.3819660112501051, abs=1e-15)


# ---- rejection surface -------------------------------------------------------

@pytest.mark.parametrize("f,r", [
    (-1, 10), (11, 10), (0, 0), (1, 0), (True, 10), (10, True),
    (0, gs.MAX_RUNS + 1), (-5, -1),
])
def test_snapshot_rejects_bad_counts(f, r):
    with pytest.raises(ValueError):
        gs.evidence_snapshot(f, r)


@pytest.mark.parametrize("p,alpha", [
    (-0.01, 0.05), (1.01, 0.05), (0.5, 0.0), (0.5, 1.0), (0.5, -1), (0.5, 2),
])
def test_runs_to_detect_rejects(p, alpha):
    with pytest.raises(ValueError):
        gs.runs_to_detect(p, alpha)


def test_runs_to_detect_p_zero_is_none():
    assert gs.runs_to_detect(0.0) is None


def test_runs_to_detect_p_one_is_one():
    assert gs.runs_to_detect(1.0) == 1
    assert gs.runs_to_detect(1.0, 1e-12) == 1


# ---- orientation channel -----------------------------------------------------

@pytest.mark.parametrize("f,r,orient", [
    (0, 1, "pass"), (1, 1, "fail"), (0, 999, "pass"), (999, 999, "fail"),
    (1, 2, "mixed"), (2, 3, "mixed"), (gs.MAX_RUNS, gs.MAX_RUNS, "fail"),
    (0, gs.MAX_RUNS, "pass"),
])
def test_orientation_partition(f, r, orient):
    assert gs.evidence_snapshot(f, r)["orientation"] == orient


def test_heat_blind_to_orientation():
    a = gs.evidence_snapshot(0, 50)
    b = gs.evidence_snapshot(50, 50)
    assert a["S"] == b["S"] == 0.0
    assert a["psi"] == pytest.approx(b["psi"]) == pytest.approx(gs.CONE)
    assert a["orientation"] != b["orientation"]


# ---- tool error surface ------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {},
    {"runs": 10},
    {"failures": 1.5, "runs": 10},
    {"failures": True, "runs": 10},
    {"failures": 5, "runs": 3},
    {"failures": -1, "runs": 4},
    {"h": 2},
    {"h": -0.1},
    {"h": True},
    {"h": float("nan")},
    {"h": float("inf")},
    {"failures": 1, "runs": 10, "alpha": 0},
    {"failures": 1, "runs": 10, "alpha": 1},
    {"failures": 1, "runs": 10, "alpha": "x"},
    {"failures": 1, "runs": 10, "alpha": True},
])
async def test_tool_rejects(args):
    out = await gs.t_great_scale(None, args)
    assert isinstance(out, str) and out.startswith("ERROR")


@pytest.mark.asyncio
async def test_tool_h_mode_json_sorted():
    out = await gs.t_great_scale(None, {"h": 0.25})
    data = json.loads(out)
    assert list(data.keys()) == sorted(data.keys())
    assert data["S"] == pytest.approx(0.25 * 0.75)
    assert data["coneOk"] is True


@pytest.mark.asyncio
async def test_tool_text_mentions_orientation_words():
    fail = await gs.t_great_scale(None, {"failures": 4, "runs": 4})
    ok = await gs.t_great_scale(None, {"failures": 0, "runs": 4})
    mixed = await gs.t_great_scale(None, {"failures": 1, "runs": 4})
    assert "deterministic fail" in fail
    assert "deterministic pass" in ok
    assert "residual heat present" in mixed
    assert "orientation: mixed" in mixed


# ---- snapshot field contract -------------------------------------------------

def test_snapshot_keys_stable():
    snap = gs.evidence_snapshot(3, 11)
    expected = {
        "failures", "runs", "p", "S", "variance", "logCoord", "psi", "cone",
        "coneOk", "onConeFloor", "orientation", "n", "lambda", "singleRunDetect",
        "detectInRuns", "runsFor95",
    }
    assert set(snap) == expected
    assert snap["variance"] == snap["S"]
    assert snap["cone"] == gs.CONE
    assert snap["coneOk"] is True


def test_log_coord_is_four_s():
    snap = gs.evidence_snapshot(1, 4)
    assert snap["logCoord"] == pytest.approx(4 * snap["S"])
    assert 0 <= snap["logCoord"] <= 1


def test_single_run_detect_equals_p():
    snap = gs.evidence_snapshot(7, 20)
    assert snap["singleRunDetect"] == snap["p"]


# ---- atlas --------------------------------------------------------------------

def test_atlas_roles_exact():
    assert gs.atlas_role(4) == "Watch"
    assert gs.atlas_role(3) == "Observer"
    assert gs.atlas_role(1) == "Forward"
    assert gs.atlas_role(0) is None


def test_from_topological_saturation_keys():
    out = gs.from_topological_saturation(0.0)
    assert set(out) >= {"S", "n", "lambda", "logCoord", "psi", "coneOk", "tau"}
    assert out["S"] == pytest.approx(0.25)
    assert out["coneOk"] is True


# ---- flakeprobe allowlist mentions great_scale if present --------------------

def test_flakeprobe_allowlist_includes_great_scale():
    try:
        from avrone import tools_flakeprobe as fp
    except ImportError:
        pytest.skip("tools_flakeprobe not present")
    allowed = getattr(fp, "FLAKE_PROBE_ALLOWED", ())
    assert "great_scale" in allowed
