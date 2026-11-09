import copy

import pytest

from avrone.trial_contract import (
    MAX_CONFIG_BYTES,
    MAX_CONFIG_DEPTH,
    make_trial,
    pool_record,
    validate_trial_envelope,
)


def sample(**overrides):
    args = {
        "system": "grok-bot",
        "node": "node-a",
        "run_id": "run-17",
        "check_id": "bridge-contract",
        "check_version": "v3",
        "attempt_id": "attempt-1",
        "outcome": "passed",
        "check_config": {"timeout": 30, "label": "café"},
    }
    args.update(overrides)
    return make_trial(**args)


def test_trial_id_is_stable_across_outcomes_but_outcome_is_preserved():
    passed = sample(outcome="passed")
    failed = sample(outcome="failed")
    assert passed["trial_id"] == failed["trial_id"]
    assert passed["outcome"] == "passed"
    assert failed["outcome"] == "failed"
    assert validate_trial_envelope(passed)
    assert validate_trial_envelope(failed)


@pytest.mark.parametrize("outcome", ["skipped", "cancelled", "timed_out", "inconclusive"])
def test_non_pass_outcomes_are_explicit_and_never_projected_as_pass(outcome):
    record = sample(outcome=outcome)
    assert validate_trial_envelope(record)
    assert pool_record(record) is None


def test_only_pass_and_fail_project_into_great_scale_pool():
    passed = sample(outcome="passed")
    failed = sample(outcome="failed")
    assert pool_record(passed) == {
        "source": "grok-bot:node-a", "trial_id": passed["trial_id"], "failed": False}
    assert pool_record(failed)["failed"] is True


def test_unicode_identity_and_config_are_nfc_canonicalized():
    decomposed = sample(system="cafe\u0301", check_config={"label": "cafe\u0301"})
    composed = sample(system="café", check_config={"label": "café"})
    assert decomposed["trial_id"] == composed["trial_id"]
    assert decomposed["check_config_sha256"] == composed["check_config_sha256"]
    assert decomposed["source"] == "café:node-a"


def test_validator_rejects_noncanonical_identity_even_if_other_fields_remain():
    record = sample()
    tampered = copy.deepcopy(record)
    tampered["node"] = "node-a\u0301"
    assert not validate_trial_envelope(tampered)


def test_validator_rejects_extra_fields_and_outcome_mutation_with_wrong_id():
    record = sample()
    extra = dict(record, independence="independent")
    assert not validate_trial_envelope(extra)
    tampered = dict(record, outcome="failed", trial_id="0" * 64)
    assert not validate_trial_envelope(tampered)


def test_config_rejects_non_string_keys_and_normalization_collisions():
    with pytest.raises(ValueError, match="keys must be strings"):
        sample(check_config={1: "one"})
    with pytest.raises(ValueError, match="collide"):
        sample(check_config={"café": 1, "cafe\u0301": 2})


def test_config_depth_and_node_limits_are_enforced():
    value = "leaf"
    for _ in range(MAX_CONFIG_DEPTH + 1):
        value = [value]
    with pytest.raises(ValueError, match="depth"):
        sample(check_config=value)


def test_config_byte_limit_is_enforced():
    with pytest.raises(ValueError, match="bytes"):
        sample(check_config={"blob": "x" * (MAX_CONFIG_BYTES + 1)})


@pytest.mark.parametrize("bad", ["", "invalid", None, 1, True])
def test_invalid_outcome_is_rejected(bad):
    with pytest.raises(ValueError, match="outcome"):
        sample(outcome=bad)


def test_identity_components_are_bounded_and_unambiguous():
    with pytest.raises(ValueError, match="system"):
        sample(system="s" * 64)
    with pytest.raises(ValueError, match="must not contain"):
        sample(node="node:other")
