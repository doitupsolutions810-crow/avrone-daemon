from __future__ import annotations

import copy
import json
import math
import random

import pytest

from avrone.integration_contract import (
    EvidenceContractError,
    MAX_PROVENANCE_DEPTH,
    MAX_REF,
    MAX_REFS,
    MAX_RUN_ID,
    MAX_SOURCE,
    build_evidence_event,
    verify_evidence_event,
)


def event(**overrides):
    values = {
        "source": "adversarial-suite",
        "run_id": "adversarial-run",
        "failures": 2,
        "runs": 17,
    }
    values.update(overrides)
    return build_evidence_event(**values)


@pytest.mark.parametrize(
    "field,limit",
    [("source", MAX_SOURCE), ("run_id", MAX_RUN_ID), ("check_id", MAX_REF)],
)
def test_identifier_exact_length_boundary_is_inclusive(field, limit):
    value = "x" * limit
    record = event(**{field: value})
    assert verify_evidence_event(record) == (True, "")
    with pytest.raises(EvidenceContractError):
        event(**{field: value + "x"})


def test_reference_count_and_length_boundaries_are_inclusive():
    refs = [f"ref-{index}-" + "x" * (MAX_REF - len(f"ref-{index}-")) for index in range(MAX_REFS)]
    record = event(refs=refs)
    assert len(record["refs"]) == MAX_REFS
    assert verify_evidence_event(record) == (True, "")
    with pytest.raises(EvidenceContractError):
        event(refs=refs + ["overflow"])
    with pytest.raises(EvidenceContractError):
        event(refs=["x" * (MAX_REF + 1)])



def test_capability_and_human_root_reference_boundaries_are_inclusive():
    maximum = "x" * MAX_REF
    record = event(capability_id=maximum, human_root="operator:root", provenance_depth=1)
    assert verify_evidence_event(record) == (True, "")
    with pytest.raises(EvidenceContractError):
        event(capability_id=maximum + "x", human_root="operator:root", provenance_depth=1)
    with pytest.raises(EvidenceContractError):
        event(capability_id="cap:1", human_root=maximum + "x", provenance_depth=1)

def test_reference_order_is_digest_significant_but_duplicates_are_canonicalized():
    left = event(refs=["alpha", "beta", "alpha"])
    same = event(refs=["alpha", "beta"])
    reversed_refs = event(refs=["beta", "alpha"])
    assert left == same
    assert left["digest"] != reversed_refs["digest"]
    assert verify_evidence_event(reversed_refs) == (True, "")


@pytest.mark.parametrize("raw", [
    True, False, 1.0, "1", None, [], {}, -1, 10**100,
])
def test_verifier_rejects_malformed_outcome_measurement_types(raw):
    record = event()
    candidate = copy.deepcopy(record)
    candidate["evidence"]["failures"] = raw
    assert verify_evidence_event(candidate)[0] is False
    candidate = copy.deepcopy(record)
    candidate["evidence"]["runs"] = raw
    assert verify_evidence_event(candidate)[0] is False


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_verifier_rejects_nonfinite_nested_measurements_without_raising(raw):
    record = event()
    candidate = copy.deepcopy(record)
    candidate["evidence"]["p"] = raw
    valid, reason = verify_evidence_event(candidate)
    assert valid is False
    assert isinstance(reason, str) and reason


def test_json_serialization_round_trip_and_key_order_do_not_change_digest():
    record = event(refs=["z", "a"], provenance_depth=MAX_PROVENANCE_DEPTH, human_root="root:1")
    decoded = json.loads(json.dumps(record, allow_nan=False))
    reordered = {key: decoded[key] for key in reversed(list(decoded))}
    assert verify_evidence_event(decoded) == (True, "")
    assert verify_evidence_event(reordered) == (True, "")
    assert decoded["digest"] == reordered["digest"]



def test_oversized_nested_summary_is_rejected_without_digest_work():
    record = event()
    candidate = copy.deepcopy(record)
    candidate["evidence"]["untrusted"] = "x" * 2_000_000
    valid, reason = verify_evidence_event(candidate)
    assert valid is False
    assert reason

def test_validly_rehashed_unknown_nested_summary_field_is_still_rejected():
    from avrone.integration_contract import _canonical
    import hashlib

    record = event()
    candidate = copy.deepcopy(record)
    candidate["evidence"]["unexpected"] = {"nested": [1, 2, 3]}
    candidate["digest"] = hashlib.sha256(
        _canonical({key: value for key, value in candidate.items() if key != "digest"})
    ).hexdigest()
    assert verify_evidence_event(candidate)[0] is False


def test_seeded_cross_field_mutation_sweep_never_accepts_stale_digest():
    rng = random.Random(20261108)
    fields = ("source", "run_id", "check_id", "refs", "capability_id", "human_root")
    original = event(capability_id="cap:read", human_root="operator:1", provenance_depth=1)
    for index in range(2_000):
        candidate = copy.deepcopy(original)
        field = rng.choice(fields)
        if field == "refs":
            candidate[field] = [f"forged:{index}"]
        elif field == "capability_id":
            candidate[field] = f"cap:forged:{index}"
        elif field == "human_root":
            candidate[field] = f"operator:forged:{index}"
        else:
            candidate[field] = f"forged-{field}-{index}"
        assert verify_evidence_event(candidate)[0] is False


def test_outcome_orientation_remains_distinct_at_both_deterministic_extremes():
    all_pass = event(failures=0, runs=1)
    all_fail = event(failures=1, runs=1)
    assert all_pass["evidence"]["S"] == all_fail["evidence"]["S"] == 0
    assert all_pass["evidence"]["orientation"] == "pass"
    assert all_fail["evidence"]["orientation"] == "fail"
    assert verify_evidence_event(all_pass) == (True, "")
    assert verify_evidence_event(all_fail) == (True, "")
