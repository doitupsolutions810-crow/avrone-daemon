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
