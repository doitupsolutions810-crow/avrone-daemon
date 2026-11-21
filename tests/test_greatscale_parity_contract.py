"""Twin-side parity contract (W1 GAP-004): avrone.greatscale must stay a faithful peer of
doitupsolutions810-crow/great-scale (public/residual.cjs). These tests pin the shared constants
and the exact function table; a change that drifts from the twin must fail here first.

Contract source: crow-os docs/W1_TRAIN_TRIAGE.md section 2 (Great Scale parity).
"""
import math

from avrone import greatscale as gs


def test_cone_is_phi_inverse_squared():
    phi = (1 + math.sqrt(5)) / 2
    assert abs(gs.PHI - phi) < 1e-15
    assert abs(gs.PHI_INV - (phi - 1)) < 1e-15
    assert abs(gs.CONE - (1 / phi) ** 2) < 1e-15
    assert abs(gs.CONE - (3 - math.sqrt(5)) / 2) < 1e-15


def test_heat_is_bernoulli_variance_and_symmetric():
    for p in (0.0, 0.01, 1 / 200, 0.25, 0.5, 0.75, 0.99, 1.0):
        assert abs(gs.heat(p) - p * (1 - p)) < 1e-15
        assert abs(gs.heat(p) - gs.heat(1 - p)) < 1e-15  # orientation-blind
    assert gs.heat(0.5) == 0.25  # peak residual heat
    assert gs.heat(0) == 0.0 and gs.heat(1) == 0.0  # deterministic checks sit on the cone floor
    assert math.isnan(gs.heat(float("nan")))


def test_log_coordinate_and_residual_count():
    for s in (0.0, 0.01, 0.25):
        assert abs(gs.log_coordinate(s) - 4 * s) < 1e-15
        assert abs(gs.residual_count(s) - (8 - 4 * s)) < 1e-15


def test_cascade_lambda_is_phi_to_minus_n():
    for n in (8.0, 6.8, 5.6, 4.4):  # residual_count at s = 0, 0.3, 0.6, 0.9
        assert abs(gs.cascade_lambda(n) - gs.PHI ** (-n)) < 1e-15


def test_psi_and_cone_floor():
    assert abs(gs.psi(0.0) - gs.CONE) < 1e-15  # deterministic check: exactly the floor
    assert gs.cone_compliant(0.0)
    assert not gs.cone_compliant(-0.1)
    for s in (0.005, 0.25):
        assert gs.psi(s) >= gs.CONE


def test_psi_floor_is_cone():
    assert gs.psi(0) == gs.CONE


def test_runs_to_detect_matches_ln_alpha_over_ln_one_minus_p():
    assert gs.runs_to_detect(1 / 200) == 598   # rare defect: 598 runs for 95%
    assert gs.runs_to_detect(0.01) == 299
    assert gs.runs_to_detect(1.0) == 1
    assert gs.runs_to_detect(0) is None        # nothing to detect
    import pytest
    with pytest.raises(ValueError):
        gs.runs_to_detect(0.5, 1.5)            # alpha out of range
    with pytest.raises(ValueError):
        gs.runs_to_detect(-0.01)


def test_evidence_snapshot_shape_and_orientation_channel():
    snap = gs.evidence_snapshot(0, 200)
    assert snap["orientation"] == "pass" and snap["onConeFloor"] is True
    assert abs(snap["S"]) < 1e-15 and abs(snap["psi"] - gs.CONE) < 1e-15
    assert snap["runsFor95"] is None
    fail = gs.evidence_snapshot(200, 200)
    assert fail["orientation"] == "fail" and fail["onConeFloor"] is True  # heat is orientation-blind
    mixed = gs.evidence_snapshot(1, 200)
    assert mixed["orientation"] == "mixed"
    # cross-formula compare: evidence_snapshot computes p*(1-p) from exact integer counts
    # (199/40000), heat() multiplies floats (0.005*0.995); they differ at ~1e-18, so pin to 1e-12
    assert abs(mixed["S"] - gs.heat(1 / 200)) < 1e-12 and abs(mixed["S"] - gs.heat(199 / 200)) < 1e-12
    assert mixed["runsFor95"] == 598


def test_dual_atlas_partition():
    assert gs.RESIDUAL_PARTITION == (4, 3, 1)
    assert gs.DUAL_ATLAS == {4: "Watch", 3: "Observer", 1: "Forward"}
    assert gs.atlas_role(4) == "Watch" and gs.atlas_role(2) is None


def test_from_topological_saturation_matches_heat_identity():
    # tau = 1 -> s = 0 (cone floor); tau = 0 -> s = 1/4 (peak heat)
    t1 = gs.from_topological_saturation(1.0)
    assert abs(t1["S"]) < 1e-15 and t1["coneOk"]
    t0 = gs.from_topological_saturation(0.0)
    assert abs(t0["S"] - 0.25) < 1e-15
    assert abs(t0["logCoord"] - 1.0) < 1e-15
