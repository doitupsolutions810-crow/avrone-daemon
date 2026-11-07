"""Edge-case battery for evidence_pool dual-channel contract.

Success criteria (Great Scale certificates):
  dual channels, threshold distances, wall/ambiguity set, reject pure-sigma.
"""
from __future__ import annotations

import math
import pytest

from avrone.evidence_pool import (
    CONFLICT_HEAT_ALARM,
    DEMOTE_PSI_FLOOR,
    PROMOTE_PSI_CEILING,
    pool_trials,
)
from avrone.greatscale import CONE, heat, psi


def test_empty_pool():
    r = pool_trials([])
    assert r["runs"] == 0
    assert r["failures"] == 0
    assert r["evidence"] is None
    assert r["evidence_jeffreys"]["p_J"] is None
    assert r["orientation_conflict"] is False
    assert r["ambiguity"]["independence"] == "not_assessed"
    assert "wall" in r["ambiguity"]
    assert r["distances"]["conflict_alarm"] is False


def test_single_pass_on_floor():
    r = pool_trials([{"source": "local", "trial_id": "t1", "failed": False}])
    assert r["runs"] == 1 and r["failures"] == 0
    e = r["evidence"]
    assert e["onConeFloor"] is True
    assert e["orientation"] == "pass"
    assert abs(e["psi"] - CONE) < 1e-12
    j = r["evidence_jeffreys"]
    assert j["p_J"] == pytest.approx(0.25)
    assert j["orientation"] == "pass"
    assert r["orientation_conflict"] is False


def test_single_fail_on_floor():
    r = pool_trials([{"source": "ci", "trial_id": "c1", "failed": True}])
    e = r["evidence"]
    assert e["onConeFloor"] is True
    assert e["orientation"] == "fail"
    assert abs(e["psi"] - CONE) < 1e-12


def test_mixed_same_source_max_heat():
    trials = [
        {"source": "a", "trial_id": "1", "failed": True},
        {"source": "a", "trial_id": "2", "failed": False},
    ]
    r = pool_trials(trials)
    e = r["evidence"]
    assert e["p"] == 0.5
    assert e["S"] == pytest.approx(0.25)
    assert e["psi"] == pytest.approx(CONE + 0.25)
    assert e["orientation"] == "mixed"
    assert r["orientation_conflict"] is False


def test_orientation_conflict_alarm():
    trials = [
        {"source": "ci", "trial_id": "c1", "failed": True},
        {"source": "ci", "trial_id": "c2", "failed": True},
        {"source": "local", "trial_id": "l1", "failed": False},
        {"source": "local", "trial_id": "l2", "failed": False},
    ]
    r = pool_trials(trials)
    assert r["orientation_conflict"] is True
    assert r["ambiguity"]["independence"] == "contradicted_by_orientation_conflict"
    assert len(r["ambiguity"]["shared_cause_candidates"]) >= 1
    e = r["evidence"]
    assert e["p"] == 0.5
    assert e["S"] == pytest.approx(0.25)
    assert r["distances"]["conflict_alarm"] is True


def test_duplicate_trial_id_rejected():
    with pytest.raises(ValueError, match="duplicate trial_id"):
        pool_trials([
            {"source": "a", "trial_id": "same", "failed": False},
            {"source": "b", "trial_id": "same", "failed": True},
        ])


def test_invalid_types_rejected():
    with pytest.raises(ValueError):
        pool_trials([{"source": "", "trial_id": "x", "failed": False}])
    with pytest.raises(ValueError):
        pool_trials([{"source": "a", "trial_id": "x", "failed": "yes"}])  # type: ignore
    with pytest.raises(ValueError):
        pool_trials([{"source": 1, "trial_id": "x", "failed": False}])  # type: ignore


def test_jeffreys_descends_with_confirmations():
    trials = [{"source": "s", "trial_id": f"t{i}", "failed": False} for i in range(40)]
    r = pool_trials(trials)
    j = r["evidence_jeffreys"]
    assert j["p_J"] == pytest.approx((0 + 0.5) / 41)
    assert j["psi_J"] < CONE + 0.02
    assert r["distances"]["near_floor"] is True


def test_dual_channel_divergence_named():
    trials = [
        {"source": "ci", "trial_id": "1", "failed": True},
        {"source": "ci", "trial_id": "2", "failed": True},
    ]
    r = pool_trials(trials)
    e, j = r["evidence"], r["evidence_jeffreys"]
    assert e["onConeFloor"] is True
    assert j["onConeFloor"] is False
    assert abs(j["psi_J"] - psi(heat(2.5 / 3))) < 1e-9
    assert "evidence_jeffreys" in r and "ambiguity" in r and "distances" in r


def test_threshold_constants_consistent():
    assert PROMOTE_PSI_CEILING > CONE
    assert DEMOTE_PSI_FLOOR > PROMOTE_PSI_CEILING
    assert 0 < CONFLICT_HEAT_ALARM <= 0.25


def test_many_sources_mixed_edge():
    trials = []
    for i in range(5):
        trials.append({"source": f"node{i}", "trial_id": f"p{i}", "failed": False})
    for i in range(3):
        trials.append({"source": f"ci{i}", "trial_id": f"f{i}", "failed": True})
    r = pool_trials(trials)
    assert r["runs"] == 8
    assert r["failures"] == 3
    assert r["orientation_conflict"] is True
    assert r["evidence"]["p"] == pytest.approx(3 / 8)
