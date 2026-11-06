"""Great Scale bridge: parity with great-scale/public/residual.cjs and the evidence channel."""
import asyncio
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from avrone import greatscale as gs
from avrone.tools import ToolRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "great_scale_residual.cjs"


def run(args):
    return asyncio.run(gs.t_great_scale(None, args))


def test_constants_match_upstream_laws():
    assert gs.CONE == pytest.approx(0.381966, abs=1e-6)
    assert gs.CONE == pytest.approx(1 / gs.PHI ** 2)
    assert gs.RESIDUAL_PARTITION == (4, 3, 1) and sum(gs.RESIDUAL_PARTITION) == 8
    assert [gs.atlas_role(b) for b in (4, 3, 1, 2)] == ["Watch", "Observer", "Forward", None]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_parity_with_javascript_residual_module():
    js = (f"const r=require({json.dumps(str(FIXTURE))});"
          "const hs=[0,0.06,0.25,0.5,0.75,1,-1,2];const taus=[0,0.3,1];const xs=[0,4.5,8.9,13,-3];"
          "console.log(JSON.stringify({cone:r.CONE,heat:hs.map(r.heat),psi:hs.map(h=>r.psi(r.heat(h))),"
          "lam:[0,1,2.5,8].map(r.cascadeLambda),topo:taus.map(t=>r.fromTopologicalSaturation(t)),"
          "soft:xs.map(x=>r.softContract(x)),rc:[0,0.25].map(r.residualCount)}))")
    ref = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert gs.CONE == pytest.approx(ref["cone"], abs=1e-15)
    hs = [0, 0.06, 0.25, 0.5, 0.75, 1, -1, 2]
    assert [gs.heat(h) for h in hs] == pytest.approx(ref["heat"], abs=1e-15)
    assert [gs.psi(gs.heat(h)) for h in hs] == pytest.approx(ref["psi"], abs=1e-15)
    assert [gs.cascade_lambda(n) for n in (0, 1, 2.5, 8)] == pytest.approx(ref["lam"], rel=1e-12)
    for t, r in zip((0, 0.3, 1), ref["topo"]):
        py = gs.from_topological_saturation(t)
        for k in ("S", "n", "lambda", "logCoord", "psi"):
            assert py[k] == pytest.approx(r[k], rel=1e-12, abs=1e-15)
        assert py["coneOk"] is r["coneOk"]
    assert [gs.soft_contract(x) for x in (0, 4.5, 8.9, 13, -3)] == pytest.approx(ref["soft"], abs=1e-12)
    assert [gs.residual_count(s) for s in (0, 0.25)] == ref["rc"]


@pytest.mark.parametrize("p", [0, 0.01, 0.06, 0.3, 0.5, 0.9, 1])
def test_heat_is_bernoulli_variance(p):
    # E[X^2] - E[X]^2 for X ~ Bernoulli(p)
    assert gs.heat(p) == pytest.approx(p - p * p)
    assert 0 <= gs.log_coordinate(gs.heat(p)) <= 1
    assert gs.cone_compliant(gs.heat(p))


def test_cone_floor_only_for_deterministic_checks_and_peak_at_half():
    assert gs.evidence_snapshot(0, 200)["onConeFloor"] and gs.evidence_snapshot(200, 200)["onConeFloor"]
    assert not gs.evidence_snapshot(1, 200)["onConeFloor"]
    peak = gs.evidence_snapshot(50, 100)
    assert peak["S"] == 0.25 and peak["logCoord"] == 1 and peak["psi"] == pytest.approx(gs.CONE + 0.25)
    assert max(gs.heat(i / 1000) for i in range(1001)) == 0.25


def test_v020_import_race_evidence_maps_and_needs_many_runs():
    # Lead measurement in v0.20: 9 failures in 150 concurrent-import trials before the fix, 0/150 after.
    before, after = gs.evidence_snapshot(9, 150), gs.evidence_snapshot(0, 150)
    assert before["p"] == 0.06 and before["S"] == pytest.approx(0.0564)
    assert before["psi"] == pytest.approx(gs.CONE + 0.0564)
    assert before["runsFor95"] == 49  # ln(0.05)/ln(0.94) = 48.4
    assert before["singleRunDetect"] == 0.06  # one CI run: 6% chance of seeing it
    assert after["onConeFloor"] and after["runsFor95"] is None


def test_runs_to_detect_is_minimal():
    for p in (0.001, 0.02, 0.06, 0.2, 0.5, 0.99):
        n = gs.runs_to_detect(p, 0.05)
        assert 1 - (1 - p) ** n >= 0.95 - 1e-12
        assert n == 1 or 1 - (1 - p) ** (n - 1) < 0.95
    assert gs.runs_to_detect(1) == 1 and gs.runs_to_detect(0) is None
    for bad in ((-0.1, 0.05), (1.1, 0.05), (0.5, 0), (0.5, 1)):
        with pytest.raises(ValueError):
            gs.runs_to_detect(*bad)


@pytest.mark.parametrize("f,r", [
    (-1, 10), (11, 10), (0, 0), (True, 10), (0, 10**10),
    (1.9, 10), (1, 10.9), ("1", 10), (1, "10"), (None, 10), (1, None),
])
def test_evidence_snapshot_rejects_bad_counts(f, r):
    with pytest.raises(ValueError):
        gs.evidence_snapshot(f, r)


def test_tool_text_json_and_h_modes():
    out = run({"failures": 9, "runs": 150})
    assert "p=0.06" in out and "runs for 95%: 49" in out and "residual heat present" in out
    j = json.loads(run({"failures": 0, "runs": 150, "format": "json"}))
    assert j["onConeFloor"] and j["psi"] == pytest.approx(gs.CONE)
    h = json.loads(run({"h": 0.5}))
    assert h["S"] == 0.25 and h["coneOk"] is True


@pytest.mark.parametrize("args", [{}, {"runs": "10"}, {"failures": 1.5, "runs": 10}, {"failures": 1, "runs": True},
                                  {"h": 2}, {"h": float("nan")}, {"h": True}, {"failures": 1, "runs": 10, "alpha": 1},
                                  {"failures": 1, "runs": 10, "alpha": "x"}, {"failures": 5, "runs": 3}])
def test_tool_rejects_bad_args(args):
    assert run(args).startswith("ERROR")


def test_registered_with_extra_tools():
    from avrone.tools_extra import register_extra_tools
    reg = ToolRegistry()
    register_extra_tools(reg)
    names = set(reg.names())
    assert "great_scale" in names
    assert math.isclose(gs.psi(0), gs.CONE)


def test_orientation_channel_separates_always_fail_from_always_pass():
    ok, bad = gs.evidence_snapshot(0, 200), gs.evidence_snapshot(200, 200)
    assert ok["S"] == bad["S"] == 0 and ok["psi"] == bad["psi"] == gs.CONE  # heat is orientation-blind
    assert (ok["orientation"], bad["orientation"], gs.evidence_snapshot(1, 200)["orientation"]) == ("pass", "fail", "mixed")
    assert "deterministic fail" in run({"failures": 200, "runs": 200})


def test_rare_defects_carry_least_heat_and_cost_most_runs():
    rare, common = gs.evidence_snapshot(1, 200), gs.evidence_snapshot(125, 200)
    assert rare["S"] < common["S"] and rare["runsFor95"] == 598 and common["runsFor95"] == 4
