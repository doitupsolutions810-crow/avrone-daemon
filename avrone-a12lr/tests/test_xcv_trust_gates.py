"""
Descent XCV / XCVI separation test — canonical module.
Asserts that a perfect Great Scale snapshot never implies authorization
when any independent trust gate is absent, expired, mismatched, or false.
All denial cases: geometry pristine, authorization denied.
Control case: every gate clean -> authorized.
Invariants: psi >= phi^-2 (cone floor), E_Q >= 0, residual partition sums to 8.

XCVI: trust_context values must be strict booleans (type(v) is bool).
Strings, ints, and any non-bool are rejected with trust_context_invalid.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

# ── Minimal stand-ins mirroring avrone/greatscale.py contracts ──────────────

GOLDEN_RATIO = (5 ** 0.5 + 1) / 2
PHI_INV_SQ = GOLDEN_RATIO ** -2  # phi^-2 ≈ 0.381966
REQUIRED_TRUST_GATES = (
    "numerical_gates",
    "provenance_bound",
    "capability_scoped",
    "receipt_chained",
)


def heat_S(h: float) -> float:
    return h * (1.0 - h)


def psi(h: float) -> float:
    return PHI_INV_SQ + heat_S(h)


def make_snapshot(
    h: float = 0.5,
    signed_C: int = 1,
    A_Q: float = 2.0,
    source_commit: str = "c725fd1" * 5,
    artifact_digest: str = "deadbeef" * 8,
    gates_passed: bool = True,
    residual_partition: tuple = (4, 3, 1),
) -> dict[str, Any]:
    E_Q = A_Q - abs(signed_C)
    return {
        "S_T": heat_S(h),
        "Psi_T": psi(h),
        "signed_C": signed_C,
        "A_Q": A_Q,
        "E_Q": E_Q,
        "gates_passed": gates_passed,
        "residual_partition": list(residual_partition),
        "source_commit": source_commit,
        "artifact_digest": artifact_digest,
        "trust_context": None,
    }


def envelope_hash(snap: dict[str, Any]) -> str:
    canon = json.dumps(snap, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


# ── Authorization decision: ALL independent gates must pass ─────────────────

def is_authorized(
    snap: dict[str, Any],
    cap: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    verifier_key: str = "trusted-verifier",
    known_commits: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """Return (authorized, reasons). ALL independent gates must pass.

    trust_context values must be strict booleans — type(v) is bool.
    Strings, ints, and any non-bool are rejected with trust_context_invalid
    before any other trust_context gate logic runs.
    """
    reasons: list[str] = []

    # Gate 1: mathematical / numerical
    if snap["Psi_T"] < PHI_INV_SQ:
        reasons.append("psi_below_cone_floor")
    if not snap["gates_passed"]:
        reasons.append("numerical_gates_failed")
    if snap["E_Q"] != snap["A_Q"] - abs(snap["signed_C"]):
        reasons.append("excess_invariant_broken")

    # Gate 2: provenance
    if not snap["source_commit"] or len(snap["source_commit"]) < 7:
        reasons.append("source_commit_missing")
    elif known_commits is not None and snap["source_commit"] not in known_commits:
        reasons.append("source_commit_unknown")
    if not snap["artifact_digest"] or len(snap["artifact_digest"]) < 8:
        reasons.append("artifact_digest_missing")

    # Gate 3: capability
    if cap is None:
        reasons.append("capability_missing")
    else:
        if "audience" not in cap:
            reasons.append("capability_audience_missing")
        if "operation" not in cap:
            reasons.append("capability_operation_missing")
        if "signature_valid" not in cap:
            reasons.append("capability_signature_valid_missing")
        if cap.get("expired"):
            reasons.append("capability_expired")
        if cap.get("revoked"):
            reasons.append("capability_revoked")
        if cap.get("audience") is not None and cap.get("audience") != "avrone-daemon":
            reasons.append("capability_wrong_audience")
        if cap.get("operation") is not None and cap.get("operation") not in ("accept", "execute", "attest"):
            reasons.append("capability_wrong_operation")
        if cap.get("signature_valid") is False:
            reasons.append("capability_signature_invalid")
        if cap.get("attenuated") and cap.get("scope_exceeded"):
            reasons.append("capability_scope_exceeded")
        # Temporal: not_before in the future is a clock-skew deny
        if cap.get("not_before") is not None:
            try:
                # treat as epoch seconds; if > now+60s, deny
                import time
                if float(cap["not_before"]) > time.time() + 60:
                    reasons.append("capability_not_before_future")
            except (TypeError, ValueError):
                reasons.append("capability_not_before_invalid")

    # Gate 4: execution receipt
    if receipt is None:
        reasons.append("receipt_missing")
    else:
        if receipt.get("envelope_hash") != envelope_hash(snap):
            reasons.append("receipt_envelope_mismatch")
        if receipt.get("verifier") != verifier_key:
            reasons.append("receipt_untrusted_verifier")

    # Gate 5: trust_context — strict boolean enforcement (XCVI)
    tc = snap.get("trust_context")
    if tc is not None:
        if not isinstance(tc, dict):
            reasons.append("trust_context_invalid")
        else:
            for k, v in tc.items():
                if type(v) is not bool:
                    reasons.append("trust_context_invalid")
                    break
            else:
                for g in REQUIRED_TRUST_GATES:
                    if g not in tc:
                        reasons.append("trust_context_invalid")
                        break
                else:
                    for k, v in tc.items():
                        if v is False:
                            reasons.append(f"trust_context_{k}_false")

    return (len(reasons) == 0, reasons)


# ── Original case ──────────────────────────────────────────────────────────

def test_original_perfect_snapshot_missing_capability_denies():
    snap = make_snapshot()
    ok, reasons = is_authorized(snap, cap=None, receipt=None)
    assert not ok
    assert "capability_missing" in reasons
    assert "receipt_missing" in reasons


# ── Six expanded edge cases ────────────────────────────────────────────────

def test_edge_01_expired_capability_perfect_geometry_denies():
    snap = make_snapshot()
    cap = {"expired": True, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "capability_expired" in reasons


def test_edge_02_wrong_audience_denies():
    snap = make_snapshot()
    cap = {"expired": False, "revoked": False, "audience": "other-service",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "capability_wrong_audience" in reasons


def test_edge_03_revoked_mid_flight_denies():
    snap = make_snapshot()
    cap = {"expired": False, "revoked": True, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "capability_revoked" in reasons


def test_edge_04_valid_cap_no_receipt_denies():
    snap = make_snapshot()
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    ok, reasons = is_authorized(snap, cap, receipt=None)
    assert not ok
    assert "receipt_missing" in reasons
    assert snap["Psi_T"] >= PHI_INV_SQ


def test_edge_05_signature_mismatch_envelope_vs_commit_denies():
    snap = make_snapshot(source_commit="c725fd1" * 5)
    tampered = dict(snap)
    tampered["artifact_digest"] = "cafebabe" * 8
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(tampered), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "receipt_envelope_mismatch" in reasons


def test_edge_06_all_trust_context_false_pristine_math_denies():
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": False,
        "provenance_bound": False,
        "capability_scoped": False,
        "receipt_chained": False,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert any(r.startswith("trust_context_") for r in reasons)
    assert snap["Psi_T"] >= PHI_INV_SQ
    assert snap["E_Q"] == snap["A_Q"] - abs(snap["signed_C"])


# ── Five harder edge cases ─────────────────────────────────────────────────

def test_edge_07_cross_envelope_receipt_replay_denies():
    """Receipt bound to snapshot A presented against snapshot B."""
    snap_a = make_snapshot(h=0.5)
    snap_b = make_snapshot(h=0.3)
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap_a), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap_b, cap, receipt)
    assert not ok
    assert "receipt_envelope_mismatch" in reasons


def test_edge_08_destructive_operation_outside_allowlist_denies():
    snap = make_snapshot()
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "delete", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "capability_wrong_operation" in reasons


def test_edge_09_attenuated_cap_scope_exceeded_denies():
    snap = make_snapshot()
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True,
           "attenuated": True, "scope_exceeded": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "capability_scope_exceeded" in reasons


def test_edge_10_partial_trust_context_mixed_denies():
    """One false gate in an otherwise-clean trust_context must still deny."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": False,
        "receipt_chained": True,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_capability_scoped_false" in reasons
    assert len([r for r in reasons if r.startswith("trust_context_")]) == 1


def test_edge_11_source_commit_absent_from_repo_history_denies():
    snap = make_snapshot(source_commit="0" * 40)  # plausible-looking but unknown
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(
        snap, cap, receipt, known_commits={"c725fd1" * 5}
    )
    assert not ok
    assert "source_commit_unknown" in reasons


# ── XCVI type-confusion and incomplete-gate cases ──────────────────────────

def test_edge_12_string_boolean_trust_context_denies():
    """Shape-valid string \"true\" must be rejected — type(v) is bool required."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": "true",
        "provenance_bound": "true",
        "capability_scoped": "true",
        "receipt_chained": "true",
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_13_int_boolean_trust_context_denies():
    """Integer 1 must be rejected — only true bools pass."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": 1,
        "provenance_bound": 1,
        "capability_scoped": 1,
        "receipt_chained": 1,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_14_missing_required_trust_gate_denies():
    """Incomplete trust_context (missing one of REQUIRED_TRUST_GATES) denies."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": True,
        # receipt_chained deliberately absent
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_15_mixed_type_trust_context_denies():
    """One string among true bools still invalidates the whole context."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": "yes",
        "receipt_chained": True,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_16_none_value_in_trust_context_denies():
    """None is not a bool; must deny."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": True,
        "receipt_chained": None,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


# ── Longer robust suite (17–22) ────────────────────────────────────────────

def test_edge_17_empty_trust_context_dict_denies():
    """Empty dict lacks REQUIRED_TRUST_GATES."""
    snap = make_snapshot()
    snap["trust_context"] = {}
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_18_list_value_in_trust_context_denies():
    """A list is not a bool."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": [True],
        "receipt_chained": True,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_19_nested_dict_value_denies():
    """Nested dict is not a bool."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": {"ok": True},
        "receipt_chained": True,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


def test_edge_20_capability_missing_required_field_denies():
    """Capability without audience field fails closed."""
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": True,
        "receipt_chained": True,
    }
    cap = {"expired": False, "revoked": False, "operation": "accept", "signature_valid": True}
    # audience deliberately absent
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "capability_audience_missing" in reasons or "capability_wrong_audience" in reasons


def test_edge_21_not_before_far_future_denies():
    """Capability with not_before in the far future is clock-skew denied."""
    import time
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": True,
        "receipt_chained": True,
    }
    cap = {
        "expired": False,
        "revoked": False,
        "audience": "avrone-daemon",
        "operation": "accept",
        "signature_valid": True,
        "not_before": time.time() + 86400 * 365,  # one year ahead
    }
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "capability_not_before_future" in reasons


def test_edge_22_bool_subclass_rejected():
    """Only exact bool type; a custom subclass fails type(v) is bool."""
    class FakeBool(int):
        def __bool__(self):
            return True

    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": FakeBool(1),
        "provenance_bound": True,
        "capability_scoped": True,
        "receipt_chained": True,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(snap, cap, receipt)
    assert not ok
    assert "trust_context_invalid" in reasons


# ── Control ────────────────────────────────────────────────────────────────

def test_control_all_gates_pass_authorizes():
    snap = make_snapshot()
    snap["trust_context"] = {
        "numerical_gates": True,
        "provenance_bound": True,
        "capability_scoped": True,
        "receipt_chained": True,
    }
    cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
           "operation": "accept", "signature_valid": True}
    receipt = {"envelope_hash": envelope_hash(snap), "verifier": "trusted-verifier"}
    ok, reasons = is_authorized(
        snap, cap, receipt, known_commits={"c725fd1" * 5}
    )
    assert ok
    assert reasons == []


# ── Invariants ─────────────────────────────────────────────────────────────

def test_psi_cone_floor_invariant():
    assert psi(0.5) == pytest.approx(PHI_INV_SQ + 0.25)
    assert psi(0.0) == pytest.approx(PHI_INV_SQ)
    assert psi(1.0) == pytest.approx(PHI_INV_SQ)
    for h in [i / 100 for i in range(101)]:
        assert psi(h) >= PHI_INV_SQ


def test_excess_non_negative():
    for C in range(-10, 11):
        for A in [abs(C) + i for i in range(0, 6)]:
            assert A - abs(C) >= 0


def test_residual_partition_sums_to_eight():
    snap = make_snapshot()
    assert sum(snap["residual_partition"]) == 8
