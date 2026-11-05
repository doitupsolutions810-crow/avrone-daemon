from __future__ import annotations

import copy
import hashlib
import json

import pytest

from avrone.integration_contract import (
    EvidenceContractError,
    build_evidence_event,
    verify_evidence_event,
    _canonical,
)


def make(failures=0, runs=100, **kwargs):
    args = {
        "source": "property-suite",
        "run_id": "property-run",
        "failures": failures,
        "runs": runs,
    }
    args.update(kwargs)
    return build_evidence_event(**args)


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




@pytest.mark.parametrize("field", ["source", "run_id", "check_id", "capability_id", "human_root"])
@pytest.mark.parametrize("value", [" padded ", "\tleading", "trailing\n"])
def test_builder_rejects_noncanonical_identifier_whitespace(field, value):
    kwargs = {field: value}
    if field == "human_root":
        kwargs["provenance_depth"] = 1
    with pytest.raises(EvidenceContractError):
        make(**kwargs)


def test_verifier_rejects_rehashed_noncanonical_identifier():
    record = make()
    record["source"] = " property-suite "
    record["digest"] = hashlib.sha256(
        _canonical({key: value for key, value in record.items() if key != "digest"})
    ).hexdigest()
    assert verify_evidence_event(record)[0] is False


@pytest.mark.parametrize("value", ["\x7f", "\x85", "\u202e", "\u2066", "\u200b", "\ud800"])
def test_builder_rejects_control_bidi_zero_width_and_surrogate_characters(value):
    with pytest.raises(EvidenceContractError):
        make(source="agent" + value)


def test_verifier_contains_deeply_nested_untrusted_evidence():
    record = make()
    nested = "leaf"
    for _ in range(1500):
        nested = [nested]
    record["evidence"]["untrusted"] = nested
    valid, reason = verify_evidence_event(record)
    assert valid is False
    assert reason


@pytest.mark.parametrize("field,value", [
    ("source", []),
    ("run_id", {}),
    ("check_id", None),
    ("refs", {"not": "a list"}),
    ("capability_id", {"id": "spoof"}),
    ("human_root", 7),
    ("provenance_depth", []),
    ("evidence", []),
    ("digest", 123),
])
def test_verifier_never_throws_for_malformed_field_shapes(field, value):
    record = make()
    record[field] = value
    valid, reason = verify_evidence_event(record)
    assert valid is False
    assert isinstance(reason, str)


def test_maximum_run_count_boundary_and_rare_failure_snapshot():
    record = make(failures=1, runs=10**9)
    snap = record["evidence"]
    assert snap["p"] == 1e-9
    assert 0 <= snap["S"] <= 0.25
    assert snap["orientation"] == "mixed"
    assert verify_evidence_event(record)[0]


def test_maximum_provenance_depth_is_inclusive_and_next_value_rejected():
    record = make(provenance_depth=4096, human_root="operator:root")
    assert verify_evidence_event(record)[0]
    with pytest.raises(EvidenceContractError):
        make(provenance_depth=4097, human_root="operator:root")
