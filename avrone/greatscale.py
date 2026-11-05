"""Great Scale bridge: AVRONE peer of doitupsolutions810-crow/great-scale (public/residual.cjs).

A faithful Python port of the shared residual coordinate (same constants, same functions),
plus one additive channel that maps AVRONE's own verification evidence into it.

Descent CV finding (AVRONE v0.21): the Great Scale heat law heat(h) = h(1 - h) is exactly the
variance of a Bernoulli trial with success probability h. Take h = the observed failure
probability p of a check (a test, a gate, a concurrent operation) over N independent runs:

    S      = heat(p) = p(1 - p)          outcome variance of the check ("residual heat")
    4S     = 4p(1 - p) in [0, 1]         the logarithmic cascade coordinate (normalised variance)
    Psi    = CONE + S >= CONE = phi^-2   cone floor reached only by a deterministic check (p = 0 or 1)

So a deterministic suite sits exactly on the cone floor, and a flaky one carries residual heat that
peaks at p = 1/2 (S = 1/4, Psi = CONE + 1/4). A single green run cannot see that heat: the chance
one run exposes a defect of rate p is p itself, and N runs expose it with probability
1 - (1 - p)^N, so detecting it with confidence 1 - alpha needs N >= ln(alpha) / ln(1 - p).
This is why AVRONE's robustness lanes use repeated-iteration hammer tests rather than one run.

Two corollaries from the v0.21 measurements. (1) Heat is orientation-blind: heat(p) = heat(1 - p), so a
check that ALWAYS fails also sits on the cone floor (the container-reaper race was 200/200). As in the
upstream QGT descents, the local scalar cannot carry the sign; the snapshot adds a separate
`orientation` channel (pass / fail / mixed). (2) The rarest defects carry the least heat and cost the
most runs: p = 1/200 gives S ~ 0.005 (Psi within 0.005 of the floor) yet needs 598 runs for 95%.

Like the upstream repository, this is a mathematical embedding of measured evidence, not a
physical or empirical claim.

Descent XCVI (AVRONE): is_authorized enforces that trust_context values are strict booleans.
Shape-valid input (strings, ints) is rejected before any gate logic runs — the field records,
never decides.
"""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:  # pragma: no cover
    from .trinity import AgentContext

PHI = (1 + math.sqrt(5)) / 2
PHI_INV = PHI - 1
CONE = PHI_INV * PHI_INV  # phi^-2 ~ 0.381966
FIXED = 4.5
RESIDUAL_PARTITION = (4, 3, 1)
DUAL_ATLAS = {4: "Watch", 3: "Observer", 1: "Forward"}
MAX_RUNS = 10**9

REQUIRED_TRUST_GATES = (
    "numerical_gates",
    "provenance_bound",
    "capability_scoped",
    "receipt_chained",
)


def heat(h: float) -> float:
    x = max(0.0, min(1.0, float(h)))
    return x * (1 - x)


def residual_count(s: float) -> float:
    return 8 - 4 * s


def cascade_lambda(n: float) -> float:
    return PHI ** (-n)


def log_coordinate(s: float) -> float:
    return 4 * s


def psi(s: float) -> float:
    return CONE + s


def cone_compliant(s: float) -> bool:
    return psi(s) >= CONE - 1e-12


def atlas_role(block: int) -> str | None:
    return DUAL_ATLAS.get(block)


def from_topological_saturation(tau: float) -> dict[str, Any]:
    s = (1 - tau * tau) / 4
    return {"S": s, "n": residual_count(s), "lambda": cascade_lambda(residual_count(s)),
            "logCoord": log_coordinate(s), "psi": psi(s), "coneOk": cone_compliant(s), "tau": tau}


def soft_contract(x: float, k: float = 0.5) -> float:
    m = ((x % 9) + 9) % 9
    return FIXED + k * (m - FIXED)


# --------------------------------------------------------------- additive evidence channel
def runs_to_detect(p: float, alpha: float = 0.05) -> int | None:
    """Smallest N with 1 - (1 - p)^N >= 1 - alpha. None when p == 0 (nothing to detect)."""
    if not (0 < alpha < 1) or not (0 <= p <= 1):
        raise ValueError("need 0 <= p <= 1 and 0 < alpha < 1")
    if p == 0:
        return None
    if p == 1:
        return 1
    return max(1, math.ceil(math.log(alpha) / math.log(1 - p) - 1e-12))


def evidence_snapshot(
    failures: int,
    runs: int,
    alpha: float = 0.05,
    trust_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map `failures` out of `runs` independent executions of one check into the Great Scale.

    Optional `trust_context` is attached verbatim under key `trust_context` when provided.
    Default-off: when omitted the key is absent. Purely observational — never used in any
    acceptance decision. Authorization logic lives in is_authorized.
    """
    if isinstance(failures, bool) or isinstance(runs, bool):
        raise ValueError("failures and runs must be integers")
    failures, runs = int(failures), int(runs)
    if not (1 <= runs <= MAX_RUNS) or not (0 <= failures <= runs):
        raise ValueError("need 1 <= runs <= 1e9 and 0 <= failures <= runs")
    p = failures / runs
    s = heat(p)
    out: dict[str, Any] = {
        "failures": failures, "runs": runs, "p": p,
        "S": s, "variance": s, "logCoord": log_coordinate(s), "psi": psi(s), "cone": CONE,
        "coneOk": cone_compliant(s), "onConeFloor": s == 0.0,
        # heat is orientation-blind (heat(p) == heat(1-p)): an always-failing check sits on the cone
        # floor like an always-passing one, so correctness rides a separate signed channel.
        "orientation": "pass" if failures == 0 else ("fail" if failures == runs else "mixed"),
        "n": residual_count(s), "lambda": cascade_lambda(residual_count(s)),
        "singleRunDetect": p, "detectInRuns": 1 - (1 - p) ** runs,
        "runsFor95": runs_to_detect(p, alpha),
    }
    if trust_context is not None:
        if not isinstance(trust_context, dict):
            raise ValueError("trust_context must be a dict when provided")
        out["trust_context"] = trust_context
    return out


# --------------------------------------------------------------- authorization channel
REQUIRED_CAP_FIELDS = ("audience", "operation", "signature_valid")
ALLOWED_OPERATIONS = ("accept", "execute", "attest")


def is_authorized(
    snap: dict[str, Any],
    cap: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    verifier_key: str = "trusted-verifier",
    known_commits: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """Return (authorized, reasons). ALL independent gates must pass.

    Gates: mathematical/numerical, provenance, capability (issuer scope via audience,
    operation, expiry, revocation, signature), execution receipt, and trust_context.
    trust_context values must be strict booleans — type(v) is bool. Strings, ints,
    and any non-bool are rejected with trust_context_invalid before gate logic runs.
    The field records gate presence; it never decides authorization by itself.
    """
    reasons: list[str] = []

    # Gate 1: mathematical / numerical
    if snap.get("psi", 0.0) < CONE - 1e-12:
        reasons.append("psi_below_cone_floor")
    if snap.get("gates_passed") is False:
        reasons.append("numerical_gates_failed")
    aq, sc = snap.get("A_Q"), snap.get("signed_C")
    if aq is not None and sc is not None and aq != abs(sc) + snap.get("E_Q", aq - abs(sc)):
        # E_Q must equal A_Q - |C|; if E_Q absent, derive and cross-check
        if "E_Q" in snap and snap["E_Q"] != aq - abs(sc):
            reasons.append("excess_invariant_broken")

    # Gate 2: provenance
    sc_str = snap.get("source_commit")
    if not sc_str or len(str(sc_str)) < 7:
        reasons.append("source_commit_missing")
    elif known_commits is not None and str(sc_str) not in known_commits:
        reasons.append("source_commit_unknown")
    ad = snap.get("artifact_digest")
    if not ad or len(str(ad)) < 8:
        reasons.append("artifact_digest_missing")

    # Gate 3: capability
    if cap is None:
        reasons.append("capability_missing")
    else:
        for f in REQUIRED_CAP_FIELDS:
            if f not in cap:
                reasons.append(f"capability_{f}_missing")
        if cap.get("expired") is True:
            reasons.append("capability_expired")
        if cap.get("revoked") is True:
            reasons.append("capability_revoked")
        if cap.get("audience") != "avrone-daemon":
            reasons.append("capability_wrong_audience")
        if cap.get("operation") not in ALLOWED_OPERATIONS:
            reasons.append("capability_wrong_operation")
        if cap.get("signature_valid") is not True:
            reasons.append("capability_signature_invalid")
        if cap.get("attenuated") is True and cap.get("scope_exceeded") is True:
            reasons.append("capability_scope_exceeded")

    # Gate 4: execution receipt
    if receipt is None:
        reasons.append("receipt_missing")
    else:
        env_hash = receipt.get("envelope_hash")
        if env_hash is not None:
            canon = json.dumps({k: v for k, v in snap.items() if k != "trust_context"},
                               sort_keys=True, separators=(",", ":"))
            # compare against hash of snap minus trust_context for cross-envelope binding
            import hashlib
            expected = hashlib.sha256(canon.encode()).hexdigest()
            # Also accept hash of full snap (including trust_context) for forward compat
            canon_full = json.dumps(snap, sort_keys=True, separators=(",", ":"))
            expected_full = hashlib.sha256(canon_full.encode()).hexdigest()
            if env_hash not in (expected, expected_full):
                reasons.append("receipt_envelope_mismatch")
        if receipt.get("verifier") != verifier_key:
            reasons.append("receipt_untrusted_verifier")

    # Gate 5: trust_context — strict boolean enforcement
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


def _fmt(snap: dict[str, Any]) -> str:
    n95 = snap["runsFor95"]
    return "\n".join([
        f"great_scale evidence: {snap['failures']}/{snap['runs']} failed  p={snap['p']:.6g}",
        f"S = heat(p) = p(1-p) = {snap['S']:.6g}   (Bernoulli outcome variance)",
        f"4S = {snap['logCoord']:.6g}   Psi = CONE + S = {snap['psi']:.6g}   CONE = phi^-2 = {CONE:.6f}",
        f"cone floor: {'yes (deterministic ' + snap['orientation'] + ')' if snap['onConeFloor'] else 'no (residual heat present)'}"
        f"   orientation: {snap['orientation']}",
        f"one run exposes it with prob {snap['singleRunDetect']:.4g}; these {snap['runs']} runs: "
        f"{snap['detectInRuns']:.4g}; runs for 95%: {'n/a' if n95 is None else n95}",
    ])


async def t_great_scale(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        if "h" in args and "runs" not in args:
            h = args["h"]
            if isinstance(h, bool) or not isinstance(h, (int, float)) or not math.isfinite(h) or not 0 <= h <= 1:
                return "ERROR: h must be a number in [0, 1]"
            s = heat(h)
            out = {"h": h, "S": s, "logCoord": log_coordinate(s), "psi": psi(s), "coneOk": cone_compliant(s)}
        else:
            alpha = args.get("alpha", 0.05)
            if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
                return "ERROR: alpha must be a number in (0, 1)"
            f, r = args.get("failures", 0), args.get("runs")
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (f, r)):
                return 'ERROR: give {"failures": int, "runs": int} or {"h": 0..1}'
            tc = args.get("trust_context")
            if tc is not None and not isinstance(tc, dict):
                return "ERROR: trust_context must be an object when provided"
            out = evidence_snapshot(f, r, float(alpha), trust_context=tc)
            if str(args.get("format", "text")).lower() != "json":
                return _fmt(out)
    except ValueError as e:
        return f"ERROR: {e}"
    return json.dumps(out, sort_keys=True)


def register_greatscale_tools(registry) -> None:
    registry.register(Tool(
        "great_scale",
        "Map verification evidence into the Great Scale residual coordinate (peer of the great-scale repo): "
        "S = heat(p) = p(1-p) is the check's Bernoulli outcome variance, Psi = phi^-2 + S, plus how many runs "
        "are needed to expose a defect of that rate. Pure math, read-only. Optional trust_context records "
        "independent gate presence (booleans only) without affecting the scalar.",
        '{"failures": int, "runs": int, "alpha"?: float, "trust_context"?: object, "format"?: "text"|"json"} or {"h": float}',
        t_great_scale, source="builtin"))
