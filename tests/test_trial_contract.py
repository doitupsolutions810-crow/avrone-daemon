import copy

import pytest

from avrone.trial_contract import (
    TrialContractError,
    TrialNotPoolable,
    make_trial,
    project_pool_trial,
    validate_trial_envelope,
)


def sample(**overrides):
    values = dict(
        system="avrone", node="node-a", run_id="run-17", check_id="security-suite",
        check_version="3.2", attempt_id="attempt-1", outcome="pass",
        check_config={"python": "3.12", "profile": "strict"},
    )
    values.update(overrides)
    return make_trial(**values)


def test_identity_is_deterministic_and_attempt_scoped():
    a, b = sample(), sample()
    assert a == b
    assert validate_trial_envelope(a)
    assert a["trial_id"] != sample(attempt_id="attempt-2")["trial_id"]


def test_check_version_and_configuration_digest_are_identity_fields():
    base = sample()
    assert base["trial_id"] != sample(check_version="3.3")["trial_id"]
    assert base["trial_id"] != sample(check_config={"python": "3.13"})["trial_id"]


def test_outcome_is_excluded_from_identity_but_conflict_is_not_poolable():
    passed, failed = sample(outcome="pass"), sample(outcome="fail")
    assert passed["trial_id"] == failed["trial_id"]
    assert passed["failed"] is False and failed["failed"] is True
    assert project_pool_trial(passed)["failed"] is False
    assert project_pool_trial(failed)["failed"] is True


@pytest.mark.parametrize("outcome", ["skipped", "cancelled", "timed_out", "inconclusive"])
def test_non_pass_outcomes_are_explicit_and_never_projected(outcome):
    record = sample(outcome=outcome)
    assert record["failed"] is None
    assert validate_trial_envelope(record)
    with pytest.raises(TrialNotPoolable, match="not poolable"):
        project_pool_trial(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("system", ""), ("system", "s" * 65), ("node", "n" * 64), ("run_id", 7),
        ("check_id", ""), ("check_version", "v" * 257), ("attempt_id", "bad\nvalue"),
        ("system", "node\u202e"),
    ],
)
def test_identity_fields_are_typed_bounded_and_log_safe(field, value):
    with pytest.raises(TrialContractError):
        sample(**{field: value})


def test_unicode_identifiers_and_config_are_nfc_canonicalized():
    a = sample(node="caf\u00e9", check_config={"label": "caf\u00e9"})
    b = sample(node="cafe\u0301", check_config={"label": "cafe\u0301"})
    assert a["trial_id"] == b["trial_id"]


def test_config_rejects_nonfinite_non_json_deep_and_oversized_values():
    with pytest.raises(TrialContractError, match="NaN or Infinity"):
        sample(check_config={"bad": float("nan")})
    with pytest.raises(TrialContractError, match="non-JSON type"):
        sample(check_config={"bad": (1, 2)})
    deep = value = {}
    for _ in range(40):
        child = {}
        value["next"] = child
        value = child
    with pytest.raises(TrialContractError, match="depth"):
        sample(check_config=deep)
    with pytest.raises(TrialContractError, match="exceeds"):
        sample(check_config={"payload": "x" * 20_000})


def test_config_rejects_keys_that_collide_after_unicode_normalization():
    with pytest.raises(TrialContractError, match="duplicate keys"):
        sample(check_config={"caf\u00e9": 1, "cafe\u0301": 2})


def test_validator_rejects_extra_tampered_and_noncanonical_fields():
    record = sample()
    assert validate_trial_envelope(record)
    extra = copy.deepcopy(record)
    extra["untrusted"] = True
    assert not validate_trial_envelope(extra)
    bad = copy.deepcopy(record)
    bad["trial_id"] = "0" * 64
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["check_version"] = "4"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["source"] = "forged:node"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["node"] = "cafe\u0301"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["failed"] = None
    assert not validate_trial_envelope(bad)


def test_composite_source_respects_existing_evidence_pool_limit():
    record = sample(system="s" * 64, node="n" * 63)
    assert len(record["source"]) == 128
    assert validate_trial_envelope(record)


def test_rejects_invalid_outcome_and_surrogate_identifiers():
    with pytest.raises(TrialContractError):
        sample(outcome="unknown")
    with pytest.raises(TrialContractError):
        sample(node="bad\ud800")
