"""Adversarial property and exact-boundary tests for the v2 trial contract."""

import pytest

from avrone.trial_contract import (
    MAX_CONFIG_BYTES,
    MAX_CONFIG_DEPTH,
    MAX_CONFIG_NODES,
    canonical_config_bytes,
    make_trial,
    validate_trial_envelope,
)


def test_config_exact_utf8_byte_limit_is_accepted_and_one_over_is_rejected():
    # The canonical encoding of {"x":"..."} adds exactly eight ASCII bytes.
    exact = {"x": "a" * (MAX_CONFIG_BYTES - 8)}
    assert len(canonical_config_bytes(exact)) == MAX_CONFIG_BYTES

    over = {"x": "a" * (MAX_CONFIG_BYTES - 7)}
    with pytest.raises(ValueError, match="bytes"):
        canonical_config_bytes(over)


def test_config_exact_node_budget_is_accepted_and_one_over_is_rejected():
    assert len(canonical_config_bytes([None] * (MAX_CONFIG_NODES - 1))) > 0

    with pytest.raises(ValueError, match="JSON values"):
        canonical_config_bytes([None] * MAX_CONFIG_NODES)


def test_config_exact_depth_boundary_is_accepted_and_one_over_is_rejected():
    at_limit = None
    for _ in range(MAX_CONFIG_DEPTH):
        at_limit = [at_limit]
    assert canonical_config_bytes(at_limit)

    over_limit = [at_limit]
    with pytest.raises(ValueError, match="maximum depth"):
        canonical_config_bytes(over_limit)


def test_nested_mapping_order_is_canonical_but_scalar_types_remain_distinct():
    left = {"outer": {"z": 1, "a": [True, 2.5]}, "other": "value"}
    right = {"other": "value", "outer": {"a": [True, 2.5], "z": 1}}
    assert canonical_config_bytes(left) == canonical_config_bytes(right)

    assert canonical_config_bytes({"value": 1}) != canonical_config_bytes({"value": 1.0})
    assert canonical_config_bytes({"value": 0.0}) != canonical_config_bytes({"value": -0.0})


def test_trial_identity_is_stable_for_nfc_equivalent_identity_and_config_strings():
    common = {
        "system": "cyclekernel",
        "node": "node-a",
        "run_id": "run-1",
        "check_id": "verify",
        "check_version": "1.0",
        "attempt_id": "attempt-1",
        "outcome": "pass",
    }
    composed = make_trial(**common, check_config={"label": "café"})
    decomposed = make_trial(**common, check_config={"label": "cafe\u0301"})

    assert composed == decomposed
    assert validate_trial_envelope(composed)
