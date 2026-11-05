from __future__ import annotations

import copy
import random

import pytest

from avrone.integration_contract import (
    EvidenceContractError,
    MAX_PROVENANCE_DEPTH,
    build_evidence_event,
    verify_evidence_event,
)


def make(**overrides):
    args = dict(source="security-suite", run_id="security-run", failures=1, runs=37)
    args.update(overrides)
    return build_evidence_event(**args)


@pytest.mark.parametrize("digest", [
    "💥", "é" * 64, "a" * 63, "a" * 65, "A" * 64, "g" * 64, "", None, 123, b"0" * 64,
])
def test_malformed_digest_is_rejected_without_raising(digest):
    record = make()
    record["digest"] = digest
    ok, reason = verify_evidence_event(record)
    assert ok is False
    assert reason


def test_depth_upper_boundary_is_accepted_and_next_value_rejected():
    record = make(provenance_depth=MAX_PROVENANCE_DEPTH, human_root="operator:ref")
    assert verify_evidence_event(record) == (True, "")
    with pytest.raises(EvidenceContractError):
        make(provenance_depth=MAX_PROVENANCE_DEPTH + 1, human_root="operator:ref")


@pytest.mark.parametrize("depth", [True, False, -1, 1.0, "2", None, 10**100])
def test_invalid_provenance_depths_are_rejected(depth):
    with pytest.raises(EvidenceContractError):
        make(provenance_depth=depth)


def test_unicode_identifiers_round_trip_but_unicode_digest_does_not():
    record = make(source="sourcé", run_id="rún-⚛")
    assert verify_evidence_event(record) == (True, "")
    record["digest"] = "é" * 64
    assert verify_evidence_event(record)[0] is False


def test_deeply_nested_untrusted_summary_is_rejected_cleanly():
    record = make()
    nested = []
    for _ in range(2000):
        nested = [nested]
    record["evidence"]["untrusted"] = nested
    ok, reason = verify_evidence_event(record)
    assert ok is False
    assert reason


def test_large_extra_event_field_is_rejected_before_digest_work():
    record = make()
    record["unexpected"] = "x" * 2_000_000
    assert verify_evidence_event(record) == (False, "event fields do not match schema v1")


def test_seeded_measurement_fuzz_always_round_trips():
    rng = random.Random(20261105)
    for _ in range(300):
        runs = rng.randint(1, 100_000)
        failures = rng.randint(0, runs)
        record = make(failures=failures, runs=runs)
        assert verify_evidence_event(record) == (True, "")
        assert record["evidence"]["orientation"] == (
            "pass" if failures == 0 else "fail" if failures == runs else "mixed"
        )


@pytest.mark.parametrize("field,value", [
    ("source", "forged-source"),
    ("run_id", "forged-run"),
    ("check_id", "forged-check"),
    ("refs", ["forged-ref"]),
    ("capability_id", "forged-capability"),
    ("human_root", "forged-root"),
    ("provenance_depth", 1),
])
def test_rehashed_identity_mutation_still_cannot_forge_measurements(field, value):
    record = make()
    original_digest = record["digest"]
    changed = copy.deepcopy(record)
    changed[field] = value
    assert changed["digest"] == original_digest
    assert verify_evidence_event(changed)[0] is False
