import pytest

from avrone.evidence_pool import MAX_POOL_TRIALS, pool_trials


def trial(source, trial_id, failed):
    return {"source": source, "trial_id": trial_id, "failed": failed}


def test_pools_disjoint_cross_source_trials_and_preserves_silo_orientation():
    result = pool_trials([
        trial("node-a", "a-1", True), trial("node-a", "a-2", True),
        trial("node-b", "b-1", False), trial("node-b", "b-2", False),
    ])

    assert (result["failures"], result["runs"]) == (2, 4)
    assert result["orientation_conflict"] is True
    assert result["independence"] == "not_assessed"
    assert [(s["source"], s["orientation"]) for s in result["sources"]] == [
        ("node-a", "fail"), ("node-b", "pass")]
    assert result["evidence"]["orientation"] == "mixed"
    assert result["evidence"]["S"] == pytest.approx(0.25)


def test_pooled_same_orientation_stays_deterministic():
    result = pool_trials([trial("a", "1", False), trial("b", "2", False)])

    assert result["orientation_conflict"] is False
    assert result["evidence"]["onConeFloor"] is True
    assert result["evidence"]["orientation"] == "pass"


def test_empty_input_is_explicitly_unmeasured_not_zero_failure_evidence():
    result = pool_trials([])

    assert result["runs"] == result["failures"] == 0
    assert result["evidence"] is None
    assert result["sources"] == []
    assert result["orientation_conflict"] is False


@pytest.mark.parametrize("records", [
    [trial("node", "same", False), trial("other", "same", True)],
    [trial("node", "", False)],
    [trial(" ", "id", False)],
    [trial("node", "id", 1)],
    [None],
    [{"source": "node", "trial_id": "id"}],
    [trial("x" * 129, "id", False)],
    [trial("node", "x" * 257, False)],
])
def test_rejects_malformed_or_replayed_trial_records(records):
    with pytest.raises(ValueError):
        pool_trials(records)


def test_rejects_oversized_pool_before_unbounded_materialization():
    def too_many():
        for n in range(MAX_POOL_TRIALS + 1):
            yield trial("node", str(n), False)

    with pytest.raises(ValueError, match="hard cap"):
        pool_trials(too_many())


def test_normalizes_whitespace_in_identity_fields():
    result = pool_trials([trial(" node ", " id ", False)])

    assert result["sources"][0]["source"] == "node"
    assert result["runs"] == 1


# ---------------------------------------------------------------------------
# Descent XIII/XIV expansion: dual-channel, walls, more edges
# ---------------------------------------------------------------------------

from avrone.evidence_pool import (
    CONFLICT_HEAT_ALARM,
    DEMOTE_PSI_FLOOR,
    PROMOTE_PSI_CEILING,
    companion_ok,
)
from avrone.greatscale import CONE, heat, psi


def test_companion_fields_present_on_nonempty():
    r = pool_trials([trial("a", "1", False)])
    assert companion_ok(r)
    assert "evidence_jeffreys" in r and "ambiguity" in r and "distances" in r
    assert "wall" in r["ambiguity"]
    assert r["psi_form"].startswith("Psi")


def test_empty_still_has_companion_shell():
    r = pool_trials([])
    assert companion_ok(r)
    assert r["evidence"] is None
    assert r["evidence_jeffreys"]["orientation"] == "empty"
    assert r["distances"]["conflict_alarm"] is False


def test_jeffreys_diverges_from_raw_on_small_n():
    r = pool_trials([trial("ci", "1", True), trial("ci", "2", True)])
    e, j = r["evidence"], r["evidence_jeffreys"]
    assert e["onConeFloor"] is True
    assert j["onConeFloor"] is False
    assert abs(j["psi_J"] - psi(heat(2.5 / 3))) < 1e-9


def test_conflict_alarm_fires_on_cross_source_max_heat():
    r = pool_trials([
        trial("ci", "c1", True), trial("ci", "c2", True),
        trial("local", "l1", False), trial("local", "l2", False),
    ])
    assert r["orientation_conflict"] is True
    assert r["ambiguity"]["independence"] == "contradicted_by_orientation_conflict"
    assert r["independence"] == "not_assessed"  # public key stays stable
    assert r["distances"]["conflict_alarm"] is True
    assert len(r["ambiguity"]["shared_cause_candidates"]) >= 1


def test_no_false_conflict_same_source_mixed():
    r = pool_trials([trial("a", "1", True), trial("a", "2", False)])
    assert r["orientation_conflict"] is False
    assert r["ambiguity"]["independence"] == "not_assessed"
    assert r["distances"]["conflict_alarm"] is False


def test_near_floor_after_many_confirmations():
    trials = [trial("s", f"t{i}", False) for i in range(40)]
    r = pool_trials(trials)
    assert r["distances"]["near_floor"] is True
    assert r["evidence_jeffreys"]["psi_J"] < CONE + 0.02


def test_demote_zone_on_max_heat():
    r = pool_trials([trial("a", "1", True), trial("a", "2", False)])
    assert r["evidence"]["S"] == pytest.approx(0.25)
    assert r["distances"]["demote_zone"] is True


def test_threshold_constants_ordered():
    assert PROMOTE_PSI_CEILING > CONE
    assert DEMOTE_PSI_FLOOR > PROMOTE_PSI_CEILING
    assert 0 < CONFLICT_HEAT_ALARM <= 0.25


def test_many_sources_partial_conflict():
    trials = [trial(f"pass{i}", f"p{i}", False) for i in range(5)]
    trials += [trial(f"fail{i}", f"f{i}", True) for i in range(3)]
    r = pool_trials(trials)
    assert r["runs"] == 8 and r["failures"] == 3
    assert r["orientation_conflict"] is True
    assert r["evidence"]["p"] == pytest.approx(3 / 8)


def test_unicode_and_surrogate_safe_ids():
    r = pool_trials([trial("node-α", "id-β-1", False)])
    assert r["runs"] == 1


def test_boolean_not_coerced_from_int():
    with pytest.raises(ValueError):
        pool_trials([{"source": "a", "trial_id": "1", "failed": 0}])


def test_independence_wall_text_present():
    r = pool_trials([trial("a", "1", False)])
    wall = r["ambiguity"]["wall"]
    assert "independence" in wall.lower() or "trial_ids" in wall.lower()
    assert "CONE" in wall or "cone" in wall.lower() or "heat" in wall.lower()


def test_companion_ok_rejects_incomplete():
    assert companion_ok({}) is False
    assert companion_ok({"evidence": None}) is False
    assert companion_ok(pool_trials([])) is True


def test_zero_runs_jeffreys_empty():
    r = pool_trials([])
    assert r["evidence_jeffreys"]["p_J"] is None


def test_single_fail_jeffreys_not_floor():
    r = pool_trials([trial("x", "1", True)])
    assert r["evidence"]["onConeFloor"] is True
    assert r["evidence_jeffreys"]["onConeFloor"] is False
