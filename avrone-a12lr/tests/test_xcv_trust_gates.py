"""
Descent XCV separation test — canonical module.
Asserts that a perfect Great Scale snapshot never implies authorization
when any independent trust gate is absent, expired, mismatched, or false.
All denial cases: geometry pristine, authorization denied.
Control case: every gate clean -> authorized.
Invariants: psi >= phi^-2 (cone floor), E_Q >= 0, residual partition sums to 8.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

# ── Minimal stand-ins mirroring avrone/greatscale.py contracts ──────────────

GOLDEN_RATIO = (5 ** 0.5 + 1) / 2
PHI_INV_SQ = GOLDEN_RATIO ** -2  # phi^-2 ≈ 0.381966


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
        if cap.get("expired"):
            reasons.append("capability_expired")
        if cap.get("revoked"):
            reasons.append("capability_revoked")
        if cap.get("audience") != "avrone-daemon":
            reasons.append("capability_wrong_audience")
        if cap.get("operation") not in ("accept", "execute", "attest"):
            reasons.append("capability_wrong_operation")
        if not cap.get("signature_valid"):
            reasons.append("capability_signature_invalid")
        # Attenuation: reduced scope must not be exceeded
        if cap.get("attenuated") and cap.get("scope_exceeded"):
            reasons.append("capability_scope_exceeded")

    # Gate 4: execution receipt
    if receipt is None:
        reasons.append("receipt_missing")
    else:
        if receipt.get("envelope_hash") != envelope_hash(snap):
            reasons.append("receipt_envelope_mismatch")
        if receipt.get("verifier") != verifier_key:
            reasons.append("receipt_untrusted_verifier")

    # Gate 5: trust_context explicit falses
    tc = snap.get("trust_context")
    if isinstance(tc, dict):
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


# ── Control ────────────────────────────────────────────────────────────────

def test_control_all_gates_pass_authorizes():
    snap = make_snapshot()
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
