"""
Descent XCVII — production-module import of is_authorized.

The separation suite (test_xcv_trust_gates.py) uses a stand-in.
This module imports is_authorized from avrone.greatscale so CI
proves the production function enforces the same fail-closed rules.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from avrone.greatscale import (
    CONE,
    REQUIRED_TRUST_GATES,
    TRUST_CONTEXT_KEYS,
    evidence_snapshot,
    is_authorized,
)


def _envelope_hash(snap: dict[str, Any]) -> str:
    canon = json.dumps(snap, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def _geo_snap(**overrides: Any) -> dict[str, Any]:
    """Minimal geometry + provenance fields expected by production is_authorized."""
    base = {
        "psi": CONE + 0.0,
        "gates_passed": True,
        "A_Q": 2.0,
        "signed_C": 1,
        "E_Q": 1.0,
        "source_commit": "c725fd1" * 5,
        "artifact_digest": "deadbeef" * 8,
        "trust_context": None,
    }
    base.update(overrides)
    return base


def test_edge_23_production_is_authorized_denies_missing_capability():
    snap = _geo_snap()
    ok, reasons = is_authorized(snap, cap=None, receipt=None)
    assert not ok
    assert "capability_missing" in reasons
    assert "receipt_missing" in reasons


def test_edge_24_production_rejects_string_boolean_trust_context():
    snap = _geo_snap(
        trust_context={
            "numerical_gates": "true",
            "provenance_bound": "true",
            "capability_scoped": "true",
            "receipt_chained": "true",
        }
    )
    cap = {
        "audience": "avrone-daemon",
        "operation": "accept",
        "signature_valid": True,
        "expired": False,
        "revoked": False,
    }
    receipt = {"envelope_hash": _envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_25_dual_schema_keys_are_disjoint():
    """REQUIRED_TRUST_GATES and TRUST_CONTEXT_KEYS must not be silently conflated."""
    assert set(REQUIRED_TRUST_GATES) != set(TRUST_CONTEXT_KEYS)
    # A context valid for evidence_snapshot keys is incomplete for is_authorized REQUIRED_TRUST_GATES
    obs_only = {k: True for k in TRUST_CONTEXT_KEYS}
    snap = _geo_snap(trust_context=obs_only)
    cap = {
        "audience": "avrone-daemon",
        "operation": "accept",
        "signature_valid": True,
        "expired": False,
        "revoked": False,
    }
    receipt = {"envelope_hash": _envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    # Production is_authorized requires REQUIRED_TRUST_GATES subset; obs-only keys fail closed
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_26_observational_snapshot_does_not_authorize():
    """evidence_snapshot with perfect geometry and no cap still cannot authorize."""
    geo = evidence_snapshot(0, 200)
    # geometry only — no provenance / capability / receipt
    ok, reasons = is_authorized(geo, None, None)
    assert not ok
    assert "capability_missing" in reasons or "source_commit_missing" in reasons


def test_control_production_all_gates_pass():
    tc = {g: True for g in REQUIRED_TRUST_GATES}
    snap = _geo_snap(trust_context=tc)
    cap = {
        "audience": "avrone-daemon",
        "operation": "accept",
        "signature_valid": True,
        "expired": False,
        "revoked": False,
    }
    receipt = {"envelope_hash": _envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(
        snap, cap, receipt, known_commits={"c725fd1" * 5}
    )
    assert ok, reasons
    assert reasons == []
