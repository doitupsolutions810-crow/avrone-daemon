from __future__ import annotations

import copy

import pytest

from avrone.evidence_pool import pool_trials
from avrone.trial_contract import (
    NON_PASS_OUTCOMES,
    make_trial,
    make_trial_outcome,
    validate_trial_envelope,
    validate_trial_outcome,
)


def sample(**overrides):
    values = dict(
        system="avrone", node="node-a", run_id="run-17",
        check_id="security-suite", check_version="3", attempt_id="attempt-1",
        failed=False, check_config={"python": "3.12", "profile": "strict"},
    )
    values.update(overrides)
    return make_trial(**values)


def test_identity_is_deterministic_and_outcome_is_excluded():
    passed = sample(failed=False)
    failed = sample(failed=True)
    assert passed["trial_id"] == failed["trial_id"]
    assert validate_trial_envelope(passed)
    assert validate_trial_envelope(failed)


def test_check_version_config_and_attempt_are_identity_fields():
    base = sample()
    assert base["trial_id"] != sample(check_version="4")["trial_id"]
    assert base["trial_id"] != sample(attempt_id="attempt-2")["trial_id"]
    assert base["trial_id"] != sample(check_config={"python": "3.13"})["trial_id"]


def test_unicode_identity_and_config_are_recursively_nfc_normalized():
    assert sample(node="caf\u00e9")["trial_id"] == sample(node="cafe\u0301")["trial_id"]
    assert sample(check_config={"label": "caf\u00e9"})["check_config_sha256"] == sample(
        check_config={"label": "cafe\u0301"}
    )["check_config_sha256"]


def test_validator_rejects_noncanonical_or_tampered_identity():
    record = sample()
    bad = copy.deepcopy(record)
    bad["node"] = "cafe\u0301"
    bad["source"] = "avrone:cafe\u0301"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["check_version"] = "4"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["source"] = "forged:node"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["unexpected"] = True
    assert not validate_trial_envelope(bad)


@pytest.mark.parametrize("value", ["bad\nnode", "bad\u0085node", "bad\u202enode", "bad\u200bnode", "bad\ud800node"])
def test_identity_rejects_controls_formatting_and_surrogates(value):
    with pytest.raises(ValueError):
        sample(node=value)


def test_source_composite_fits_existing_evidence_pool_without_truncation():
    record = sample(system="s" * 63, node="n" * 63)
    assert len(record["source"]) == 127
    pooled = pool_trials([{
        "source": record["source"], "trial_id": record["trial_id"], "failed": record["failed"],
    }])
    assert pooled["runs"] == 1
    assert pooled["independence"] == "not_assessed"


def test_config_rejects_normalized_key_collisions_and_unsafe_values():
    with pytest.raises(ValueError, match="collide"):
        sample(check_config={"caf\u00e9": 1, "cafe\u0301": 2})
    with pytest.raises(ValueError):
        sample(check_config={"bad": float("nan")})
    deep = {}
    cursor = deep
    for _ in range(40):
        cursor["x"] = {}
        cursor = cursor["x"]
    with pytest.raises(ValueError, match="depth"):
        sample(check_config=deep)


def test_config_rejects_oversize_and_non_json_values():
    with pytest.raises(ValueError, match="exceeds"):
        sample(check_config={"payload": "x" * 20_000})
    with pytest.raises(ValueError):
        sample(check_config={"value": object()})


@pytest.mark.parametrize("outcome", sorted(NON_PASS_OUTCOMES))
def test_operational_outcomes_are_explicit_and_not_pass_fail(outcome):
    record = make_trial_outcome(
        system="avrone", node="node-a", run_id="run-17", check_id="suite",
        check_version="3", attempt_id=f"attempt-{outcome}", outcome=outcome,
    )
    assert validate_trial_outcome(record)
    assert "failed" not in record


def test_nonpass_and_completed_result_share_identity_for_conflict_detection():
    common = dict(
        system="avrone", node="node-a", run_id="run-17", check_id="suite",
        check_version="3", attempt_id="attempt-1",
    )
    assert make_trial_outcome(**common, outcome="timed_out")["trial_id"] == make_trial(
        **common, failed=False
    )["trial_id"]
