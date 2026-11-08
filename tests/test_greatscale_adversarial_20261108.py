from __future__ import annotations

import math

import pytest

from avrone.greatscale import (
    MAX_RUNS,
    evidence_snapshot,
    heat,
    runs_to_detect,
)


@pytest.mark.parametrize("bad", [True, False, 1.0, 1.5, "1", None, [], {}])
def test_evidence_snapshot_rejects_non_integer_failure_counts(bad):
    with pytest.raises(ValueError, match="integers"):
        evidence_snapshot(bad, 10)


@pytest.mark.parametrize("bad", [True, False, 10.0, "10", None, [], {}])
def test_evidence_snapshot_rejects_non_integer_run_counts(bad):
    with pytest.raises(ValueError, match="integers"):
        evidence_snapshot(1, bad)


@pytest.mark.parametrize("p", [True, False, "0.1", None, [], {}, float("nan"), float("inf")])
def test_runs_to_detect_rejects_non_numeric_or_nonfinite_probabilities(p):
    with pytest.raises(ValueError):
        runs_to_detect(p, 0.05)


@pytest.mark.parametrize("alpha", [True, False, "0.05", None, [], {}, float("nan"), float("inf"), 0, 1])
def test_runs_to_detect_rejects_invalid_confidence_thresholds(alpha):
    with pytest.raises(ValueError):
        runs_to_detect(0.1, alpha)


@pytest.mark.parametrize("p", [0.0, 1.0, 1e-9, 0.001, 0.01, 0.05, 0.2, 0.5, 0.9, 0.999])
@pytest.mark.parametrize("alpha", [0.5, 0.1, 0.05, 0.01])
def test_detection_count_is_minimal_for_probability_and_confidence(p, alpha):
    count = runs_to_detect(p, alpha)
    if p == 0:
        assert count is None
        return
    assert count is not None and count >= 1
    probability = 1 - (1 - p) ** count
    assert probability >= 1 - alpha - 1e-12
    if count > 1:
        previous = 1 - (1 - p) ** (count - 1)
        assert previous < 1 - alpha + 1e-12


def test_detection_count_handles_deterministic_failure_and_pass_extremes():
    assert runs_to_detect(0.0) is None
    assert runs_to_detect(1.0) == 1
    assert evidence_snapshot(0, 1)["orientation"] == "pass"
    assert evidence_snapshot(1, 1)["orientation"] == "fail"


def test_heat_is_symmetric_but_snapshot_orientation_is_not():
    for numerator in range(0, 101):
        p = numerator / 100
        assert math.isclose(heat(p), heat(1 - p), rel_tol=0, abs_tol=1e-15)
    passing = evidence_snapshot(1, 10)
    failing = evidence_snapshot(9, 10)
    assert passing["S"] == failing["S"]
    assert passing["orientation"] == "mixed"
    assert failing["orientation"] == "mixed"


def test_maximum_supported_trial_count_and_one_beyond():
    assert evidence_snapshot(1, MAX_RUNS)["runs"] == MAX_RUNS
    with pytest.raises(ValueError):
        evidence_snapshot(1, MAX_RUNS + 1)


def test_failure_count_cannot_exceed_runs_even_at_maximum_boundary():
    with pytest.raises(ValueError):
        evidence_snapshot(MAX_RUNS + 1, MAX_RUNS)
