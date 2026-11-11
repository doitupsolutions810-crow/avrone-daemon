"""Great Scale: reject fractional evidence; retain rare-event sample size precision."""
import math
import pytest
from avrone import greatscale as gs

@pytest.mark.parametrize("failures,runs", [
    (1.5, 10), (0.0, 10), (1, 10.0), ("1", 10), (1, "10"),
    (float("nan"), 10), (1, float("inf")), (1, 1.99), (True, 10), (1, False),
])
def test_evidence_snapshot_rejects_noninteger_trial_counts(failures, runs):
    with pytest.raises(ValueError, match="integers"):
        gs.evidence_snapshot(failures, runs)

@pytest.mark.parametrize("p", [1e-9, 1e-12, 1e-16, 1e-17, 1e-20])
def test_very_rare_failure_detection_uses_stable_probability(p):
    n = gs.runs_to_detect(p)
    assert isinstance(n, int) and n > 0
    assert abs(n * p + math.log(0.05)) <= 3 * p + 1e-12

@pytest.mark.parametrize("failures,runs", [(0, 1), (1, 1), (1, 2), (0, 10**9)])
def test_valid_integer_evidence_contract_remains_supported(failures, runs):
    out = gs.evidence_snapshot(failures, runs)
    assert (out["failures"], out["runs"]) == (failures, runs)
