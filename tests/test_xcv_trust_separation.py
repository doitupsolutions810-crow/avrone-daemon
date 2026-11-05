"""Descent XCV: trust gates are recorded, never decided. Separation invariant tests."""
from __future__ import annotations

import pytest

from avrone.greatscale import evidence_snapshot, CONE


TRUST_KEYS = ("source_commit", "artifact_digest", "signature_verified",
              "capability_valid", "execution_receipt", "numerical_gates_passed",
              "domain_assumptions_valid")


def test_default_off_no_trust_fields():
    snap = evidence_snapshot(1, 10)
    assert "trust_context" not in snap
    assert "trust_gates_present" not in snap
    assert "all_trust_gates_pass" not in snap


def test_trust_context_records_but_does_not_alter_geometry():
    base = evidence_snapshot(3, 30)
    with_tc = evidence_snapshot(3, 30, trust_context={"capability_valid": False})
    for k in ("S", "psi", "logCoord", "p", "orientation", "onConeFloor"):
        assert base[k] == with_tc[k]
    assert "trust_context" not in base
    assert with_tc["trust_context"] == {"capability_valid": False}
    assert with_tc["trust_gates_present"] is True
    assert with_tc["all_trust_gates_pass"] is False


def test_perfect_snapshot_missing_capability_is_not_authorized():
    """The core XCV assertion: geometry can be perfect and still unauthorized."""
    snap = evidence_snapshot(
        0, 1000,
        trust_context={k: True for k in TRUST_KEYS if k != "capability_valid"},
    )
    assert snap["onConeFloor"] is True
    assert snap["orientation"] == "pass"
    assert snap["trust_gates_present"] is True
    assert snap["all_trust_gates_pass"] is False  # missing capability_valid


def test_all_gates_pass_is_explicit():
    snap = evidence_snapshot(0, 100, trust_context={k: True for k in TRUST_KEYS})
    assert snap["all_trust_gates_pass"] is True


def test_signature_false_blocks_all_pass():
    snap = evidence_snapshot(
        0, 50,
        trust_context={k: True for k in TRUST_KEYS if k != "signature_verified"}
        | {"signature_verified": False},
    )
    assert snap["all_trust_gates_pass"] is False


def test_unknown_trust_key_rejected():
    with pytest.raises(ValueError, match="unknown trust_context key"):
        evidence_snapshot(1, 10, trust_context={"nope": True})


def test_non_dict_trust_context_rejected():
    with pytest.raises(ValueError, match="trust_context must be a dict"):
        evidence_snapshot(1, 10, trust_context="x")  # type: ignore


def test_empty_trust_context_is_not_present():
    snap = evidence_snapshot(1, 10, trust_context={})
    assert snap["trust_gates_present"] is False
    assert snap["all_trust_gates_pass"] is False


def test_heat_still_orientation_blind_with_trust_context():
    a = evidence_snapshot(0, 100, trust_context={"capability_valid": True})
    b = evidence_snapshot(100, 100, trust_context={"capability_valid": True})
    assert a["S"] == b["S"] and a["psi"] == b["psi"]
    assert a["orientation"] != b["orientation"]
