from __future__ import annotations

import copy
import json

import pytest

from avrone.integration_contract import (
    EvidenceContractError,
    build_evidence_event,
    verify_evidence_event,
)


def make(failures=0, runs=100, **kwargs):
    return build_evidence_event(
        source="property-suite",
        run_id="property-run",
        failures=failures,
        runs=runs,
        **kwargs,
    )


@pytest.mark.parametrize("runs", [1, 2, 3, 7, 10, 99, 100, 997, 1000])
def test_every_integer_failure_count_round_trips_for_small_domains(runs):
    for failures in range(runs + 1):
        record = make(failures=failures, runs=runs)
        assert verify_evidence_event(record) == (True, "")
        assert record["evidence"]["failures"] == failures
        assert record["evidence"]["runs"] == runs


def test_json_round_trip_preserves_verifiable_canonical_event():
    record = make(failures=17, runs=113, refs=["a", "b"])
    decoded = json.loads(json.dumps(record, allow_nan=False))
    assert verify_evidence_event(decoded) == (True, "")


def test_duplicate_refs_are_deduplicated_deterministically():
    first = make(refs=["a", "a", "b"])
    second = make(refs=["a", "b"])
    assert first == second
    assert first["refs"] == ["a", "b"]


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(source="other"),
    lambda e: e.update(run_id="other"),
    lambda e: e.update(check_id="other"),
    lambda e: e.update(refs=["different"]),
    lambda e: e.update(capability_id="cap:other"),
    lambda e: e.update(human_root="operator:other"),
    lambda e: e.update(provenance_depth=1),
])
def test_every_identity_and_provenance_field_is_digest_bound(mutation):
    record = make()
    mutation(record)
    assert verify_evidence_event(record)[0] is False


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, -1])
def test_provenance_depth_rejects_coercion_and_invalid_values(value):
    with pytest.raises(EvidenceContractError):
        make(provenance_depth=value)


@pytest.mark.parametrize("value", [True, 1.0, "1", None, -1])
def test_failure_and_run_counts_reject_non_integer_types(value):
    with pytest.raises(EvidenceContractError):
        make(failures=value)
    with pytest.raises(EvidenceContractError):
        make(runs=value)


def test_verifier_rejects_rehashed_but_inconsistent_measurement_summary():
    record = make(failures=1, runs=10)
    # Rebuilding a valid digest around an inconsistent evidence object must not
    # bypass the independent recomputation check.
    from avrone.integration_contract import _canonical
    import hashlib

    forged = copy.deepcopy(record)
    forged["evidence"]["p"] = 0.9
    body = {key: forged[key] for key in forged if key != "digest"}
    forged["digest"] = hashlib.sha256(_canonical(body)).hexdigest()
    assert verify_evidence_event(forged)[0] is False


def test_verifier_rejects_nonfinite_numbers_even_with_valid_digest():
    record = make()
    from avrone.integration_contract import _canonical
    import hashlib

    forged = copy.deepcopy(record)
    forged["evidence"]["p"] = float("nan")
    with pytest.raises(ValueError):
        forged["digest"] = hashlib.sha256(_canonical({k: v for k, v in forged.items() if k != "digest"})).hexdigest()


def test_nested_unknown_fields_are_rejected_by_summary_recomputation():
    record = make()
    forged = copy.deepcopy(record)
    forged["evidence"]["unreviewed"] = "unexpected"
    assert verify_evidence_event(forged)[0] is False
