from __future__ import annotations

import copy

import pytest

from avrone.integration_contract import (
    EvidenceContractError,
    build_evidence_event,
    verify_evidence_event,
)


def event(**overrides):
    args = dict(source="avrone-daemon", run_id="run-1", failures=1, runs=100,
                refs=["test:smoke"], provenance_depth=0)
    args.update(overrides)
    return build_evidence_event(**args)


def test_event_round_trip_and_digest_is_deterministic():
    first = event()
    second = event()
    assert first == second
    assert verify_evidence_event(first) == (True, "")


def test_outcome_orientation_is_not_erased_by_symmetric_heat():
    passing = event(failures=0, runs=10)
    failing = event(failures=10, runs=10)
    assert passing["evidence"]["S"] == failing["evidence"]["S"] == 0
    assert passing["evidence"]["orientation"] == "pass"
    assert failing["evidence"]["orientation"] == "fail"
    assert verify_evidence_event(failing)[0]


@pytest.mark.parametrize("failures,runs", [
    (True, 2), (-1, 2), (3, 2), (0, 0), (1, 10**10), (1.5, 2), ("1", 2),
])
def test_invalid_measurements_are_rejected(failures, runs):
    with pytest.raises((ValueError, TypeError)):
        event(failures=failures, runs=runs)


def test_model_only_evidence_cannot_claim_human_provenance():
    with pytest.raises(EvidenceContractError):
        event(provenance_depth=1)


def test_explicit_human_root_allows_declared_ancestry():
    record = event(provenance_depth=1, human_root="operator:approved")
    assert verify_evidence_event(record)[0]


def test_tampering_and_extra_fields_are_rejected():
    record = event()
    changed = copy.deepcopy(record)
    changed["evidence"]["failures"] = 0
    assert verify_evidence_event(changed)[0] is False
    extended = copy.deepcopy(record)
    extended["unreviewed"] = True
    assert verify_evidence_event(extended)[0] is False


@pytest.mark.parametrize("refs", ["not-a-list", [""], ["x" * 161], ["x"] * 33])
def test_bad_refs_are_rejected(refs):
    with pytest.raises(EvidenceContractError):
        event(refs=refs)


def test_nonfinite_and_control_text_are_rejected():
    with pytest.raises(EvidenceContractError):
        event(source="avrone\nforged")
    with pytest.raises(EvidenceContractError):
        event(run_id="\x00")
