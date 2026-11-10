"""Wave 10: evidence_pool.pool_trials — dedupe, conflict, caps, snapshot attach.

Checked against avrone/evidence_pool.py and greatscale.py from main before push.
"""
from __future__ import annotations

import pytest

from avrone import evidence_pool as ep
from avrone.greatscale import CONE, evidence_snapshot


def trial(source, tid, failed):
    return {"source": source, "trial_id": tid, "failed": failed}


def test_empty_pool_has_no_snapshot():
    out = ep.pool_trials([])
    assert out["runs"] == 0
    assert out["failures"] == 0
    assert out["sources"] == []
    assert out["evidence"] is None
    assert out["orientation_conflict"] is False
    assert out["independence"] == "not_assessed"


def test_generator_input_is_consumed_once():
    def gen():
        yield trial("a", "1", False)
        yield trial("a", "2", True)

    out = ep.pool_trials(gen())
    assert out["runs"] == 2 and out["failures"] == 1
    assert out["evidence"] == evidence_snapshot(1, 2)


def test_single_pass_and_single_fail():
    ok = ep.pool_trials([trial("gate", "t1", False)])
    bad = ep.pool_trials([trial("gate", "t1", True)])
    assert ok["evidence"]["orientation"] == "pass"
    assert ok["evidence"]["onConeFloor"] is True
    assert ok["evidence"]["psi"] == pytest.approx(CONE)
    assert bad["evidence"]["orientation"] == "fail"
    assert bad["evidence"]["runsFor95"] == 1
    assert ok["orientation_conflict"] is False
    assert bad["orientation_conflict"] is False


def test_duplicate_id_is_rejected_not_counted():
    rows = [trial("a", "same", False), trial("b", "same", True)]
    with pytest.raises(ValueError, match="duplicate trial_id"):
        ep.pool_trials(rows)


def test_strip_makes_ids_and_sources_collide():
    with pytest.raises(ValueError, match="duplicate"):
        ep.pool_trials([trial("a", " id ", False), trial("a", "id", False)])
    out = ep.pool_trials([trial("  src  ", "1", False), trial("src", "2", True)])
    assert len(out["sources"]) == 1
    assert out["sources"][0]["source"] == "src"
    assert out["sources"][0]["orientation"] == "mixed"


def test_blank_and_non_string_rejected():
    with pytest.raises(ValueError):
        ep.pool_trials([trial("   ", "1", False)])
    with pytest.raises(ValueError):
        ep.pool_trials([trial("a", "   ", False)])
    with pytest.raises(ValueError):
        ep.pool_trials([trial("", "1", False)])
    with pytest.raises(ValueError):
        ep.pool_trials([{"source": 1, "trial_id": "1", "failed": False}])
    with pytest.raises(ValueError):
        ep.pool_trials(["not-a-mapping"])


def test_failed_must_be_real_bool():
    for bad in (1, 0, "true", None, 1.0):
        with pytest.raises(ValueError, match="boolean"):
            ep.pool_trials([trial("a", "1", bad)])


def test_length_caps():
    with pytest.raises(ValueError):
        ep.pool_trials([trial("s" * (ep.MAX_SOURCE_CHARS + 1), "1", False)])
    with pytest.raises(ValueError):
        ep.pool_trials([trial("s", "i" * (ep.MAX_TRIAL_ID_CHARS + 1), False)])
    # exact caps are accepted
    out = ep.pool_trials([trial("s" * ep.MAX_SOURCE_CHARS, "i" * ep.MAX_TRIAL_ID_CHARS, False)])
    assert out["runs"] == 1


def test_pass_plus_fail_is_conflict_and_max_heat():
    # Descent III: two cone-floor silos of opposite sign pool to S = 0.25.
    out = ep.pool_trials([
        trial("alpha", "a1", False),
        trial("alpha", "a2", False),
        trial("beta", "b1", True),
        trial("beta", "b2", True),
    ])
    assert out["orientation_conflict"] is True
    assert out["failures"] == 2 and out["runs"] == 4
    assert out["evidence"]["S"] == pytest.approx(0.25)
    assert out["evidence"]["orientation"] == "mixed"
    assert [r["source"] for r in out["sources"]] == ["alpha", "beta"]
    assert out["sources"][0]["orientation"] == "pass"
    assert out["sources"][1]["orientation"] == "fail"
    assert out["independence"] == "not_assessed"


def test_mixed_with_pass_is_not_a_sign_conflict():
    out = ep.pool_trials([
        trial("alpha", "a1", False),
        trial("beta", "b1", False),
        trial("beta", "b2", True),
    ])
    assert out["sources"][0]["orientation"] == "pass"
    assert out["sources"][1]["orientation"] == "mixed"
    assert out["orientation_conflict"] is False


def test_mixed_with_fail_is_not_a_sign_conflict():
    out = ep.pool_trials([
        trial("alpha", "a1", True),
        trial("beta", "b1", False),
        trial("beta", "b2", True),
    ])
    assert "fail" in {r["orientation"] for r in out["sources"]}
    assert "mixed" in {r["orientation"] for r in out["sources"]}
    assert out["orientation_conflict"] is False


def test_three_way_conflict_still_fires():
    out = ep.pool_trials([
        trial("a", "1", False),
        trial("b", "2", True),
        trial("c", "3", False),
        trial("c", "4", True),
    ])
    orients = {r["orientation"] for r in out["sources"]}
    assert orients == {"pass", "fail", "mixed"}
    assert out["orientation_conflict"] is True


def test_source_order_is_sorted_not_insertion():
    out = ep.pool_trials([
        trial("zeta", "1", False),
        trial("alpha", "2", True),
        trial("mu", "3", False),
    ])
    assert [r["source"] for r in out["sources"]] == ["alpha", "mu", "zeta"]


def test_extra_fields_are_ignored():
    row = trial("a", "1", False)
    row["note"] = "ignore"
    row["runs"] = 99
    out = ep.pool_trials([row])
    assert out["runs"] == 1
    assert out["evidence"]["runs"] == 1


def test_pool_trial_cap():
    def gen():
        for i in range(ep.MAX_POOL_TRIALS + 1):
            yield trial("s", str(i), False)

    with pytest.raises(ValueError, match="hard cap"):
        ep.pool_trials(gen())


def test_source_cap():
    def gen():
        for i in range(ep.MAX_SOURCES + 1):
            yield trial(f"s{i}", str(i), i % 2 == 0)

    with pytest.raises(ValueError, match="sources"):
        ep.pool_trials(gen())


def test_evidence_matches_direct_snapshot():
    rows = [trial("a", str(i), i < 3) for i in range(10)]
    out = ep.pool_trials(rows)
    assert out["evidence"] == evidence_snapshot(3, 10)
    assert out["evidence"]["coneOk"] is True
