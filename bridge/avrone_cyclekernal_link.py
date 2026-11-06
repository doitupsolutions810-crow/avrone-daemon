"""avrone_cyclekernal_link.py

The last unconnected connection: wires the AVRONE daemon's evolve loop to the
CycleKernal invariant spine.

Invariant spine state:  P = (r, delta, c, sigma, T)
  r     : reward / participation rate          in [0, 1]
  delta : drift bound (max single-step change)  >= 0
  c     : cost / resource consumption          >= 0
  sigma : stability noise / variance            >= 0
  T     : temperature analog (exploration)      >= 0

Evolution rule:  P_new = P_old + dP,  subject to  ||dP||_2 <= epsilon
Stability score: Psi(P) = H - sigma,  where H is staking/health participation.

This module enforces the bound at the boundary between off-chain workers and
on-chain state. No drift past epsilon. No silent mutation.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

State = Tuple[float, float, float, float, float]  # (r, delta, c, sigma, T)


@dataclass
class EvolutionStep:
    """One bounded evolution step."""
    p_old: State
    d_p: State
    p_new: State
    epsilon: float
    psi_before: float
    psi_after: float
    accepted: bool
    reason: str
    ts: float = field(default_factory=time.time)

    def norm_dp(self) -> float:
        return math.sqrt(sum(x * x for x in self.d_p))


@dataclass
class EvidenceRecord:
    """GS-LXI style evidence envelope. Hash-chained, tamper-evident."""
    seq: int
    kind: str
    payload: Dict[str, Any]
    prev_hash: str
    ts: float = field(default_factory=time.time)

    def digest(self) -> str:
        body = json.dumps(
            {"seq": self.seq, "kind": self.kind, "payload": self.payload,
             "prev_hash": self.prev_hash, "ts": self.ts},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Math: the spine
# ---------------------------------------------------------------------------

def clamp_state(p: State) -> State:
    """Project P onto the feasible orthant with domain bounds.

    r in [0,1]; delta, c, sigma, T >= 0.
    """
    r, d, c, s, t = p
    return (min(1.0, max(0.0, r)), max(0.0, d), max(0.0, c), max(0.0, s), max(0.0, t))


def l2_norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def evolution_bounded(p_old: State, d_p: State, epsilon: float) -> State:
    """Apply dP only if ||dP||_2 <= epsilon; otherwise scale to the sphere.

    This is the 'no drift' gate. Unbounded self-modification is rejected by
    construction: the worst case is a step exactly on the epsilon sphere.
    """
    if epsilon < 0:
        raise ValueError(f"epsilon must be >= 0, got {epsilon}")
    n = l2_norm(d_p)
    if n == 0.0:
        return clamp_state(p_old)
    if n <= epsilon:
        stepped = tuple(a + b for a, b in zip(p_old, d_p))
    else:
        scale = epsilon / n
        stepped = tuple(a + b * scale for a, b in zip(p_old, d_p))
    return clamp_state(stepped)


def stability_score(p: State, H: float) -> float:
    """Psi(P) = H - sigma. Higher is more stable."""
    return H - p[3]


def participation_from_r(r: float, base: float = 1.0) -> float:
    """Map reward rate r to a health/staking participation proxy H."""
    return base * r


# ---------------------------------------------------------------------------
# The link: daemon evolve loop -> spine
# ---------------------------------------------------------------------------

class CycleKernalLink:
    """Boundary object between AVRONE's off-chain evolve loop and on-chain state.

    Responsibilities:
      1. Accept proposed dP from OCW / squad payloads.
      2. Enforce ||dP|| <= epsilon (the invariant spine gate).
      3. Compute Psi before/after; reject if stability collapses.
      4. Emit hash-chained EvidenceRecords for every decision.
    """

    def __init__(self, epsilon: float = 0.05, psi_floor: float = -1.0):
        if epsilon < 0:
            raise ValueError("epsilon >= 0 required")
        self.epsilon = epsilon
        self.psi_floor = psi_floor
        self.p: State = (0.5, 0.01, 0.0, 0.0, 0.1)  # sane genesis
        self.history: List[EvolutionStep] = []
        self.ledger: List[EvidenceRecord] = []
        self._seq = 0
        self._emit("genesis", {"p": list(self.p), "epsilon": epsilon})

    # -- ledger --------------------------------------------------------------
    def _emit(self, kind: str, payload: Dict[str, Any]) -> EvidenceRecord:
        prev = self.ledger[-1].digest() if self.ledger else "0" * 64
        rec = EvidenceRecord(seq=self._seq, kind=kind, payload=payload, prev_hash=prev)
        self.ledger.append(rec)
        self._seq += 1
        return rec

    # -- evolve --------------------------------------------------------------
    def propose(self, d_p: Sequence[float], H: Optional[float] = None) -> EvolutionStep:
        """Propose one evolution step. Returns the (possibly clamped) result."""
        if len(d_p) != 5:
            raise ValueError(f"dP must have 5 components, got {len(d_p)}")
        d = tuple(float(x) for x in d_p)
        H = participation_from_r(self.p[0]) if H is None else float(H)

        psi_before = stability_score(self.p, H)
        p_new = evolution_bounded(self.p, d, self.epsilon)
        psi_after = stability_score(p_new, participation_from_r(p_new[0]))

        accepted = True
        reason = "within_bound"
        if psi_after < self.psi_floor:
            accepted = False
            reason = f"psi_collapse {psi_after:.4f} < floor {self.psi_floor}"
            p_new = self.p  # revert
            psi_after = psi_before

        step = EvolutionStep(
            p_old=self.p, d_p=d, p_new=p_new, epsilon=self.epsilon,
            psi_before=psi_before, psi_after=psi_after,
            accepted=accepted, reason=reason,
        )
        self.history.append(step)
        if accepted:
            self.p = p_new
        self._emit("evolve", {
            "accepted": accepted, "reason": reason,
            "p_old": list(step.p_old), "d_p": list(d), "p_new": list(p_new),
            "norm_dp": step.norm_dp(), "psi_before": psi_before, "psi_after": psi_after,
        })
        return step

    def batch_propose(self, steps: Sequence[Sequence[float]]) -> List[EvolutionStep]:
        return [self.propose(s) for s in steps]

    # -- introspection -------------------------------------------------------
    def verify_ledger(self) -> bool:
        """Re-hash the entire evidence chain. True iff no tampering."""
        prev = "0" * 64
        for rec in self.ledger:
            if rec.prev_hash != prev:
                return False
            prev = rec.digest()
        return True

    def summary(self) -> Dict[str, Any]:
        acc = [s for s in self.history if s.accepted]
        rej = [s for s in self.history if not s.accepted]
        return {
            "p": list(self.p),
            "epsilon": self.epsilon,
            "steps": len(self.history),
            "accepted": len(acc),
            "rejected": len(rej),
            "psi": stability_score(self.p, participation_from_r(self.p[0])),
            "ledger_len": len(self.ledger),
            "ledger_valid": self.verify_ledger(),
            "max_norm_dp": max((s.norm_dp() for s in self.history), default=0.0),
        }


# ---------------------------------------------------------------------------
# Edge-case battery (also exposed for verify.sh)
# ---------------------------------------------------------------------------

EDGE_CASES: List[Dict[str, Any]] = [
    {"name": "zero_step", "d_p": [0, 0, 0, 0, 0], "expect_accept": True},
    {"name": "tiny_step", "d_p": [0.001, 0, 0, 0, 0], "expect_accept": True},
    {"name": "exact_bound", "d_p": [0.05, 0, 0, 0, 0], "expect_accept": True},  # ||dP|| == epsilon
    {"name": "over_bound", "d_p": [0.5, 0.5, 0.5, 0.5, 0.5], "expect_accept": True},  # clamped to sphere
    {"name": "negative_components", "d_p": [-0.02, -0.02, -0.02, -0.02, -0.02], "expect_accept": True},
    {"name": "r_overflow", "d_p": [10.0, 0, 0, 0, 0], "expect_accept": True},  # r clamps to 1
    {"name": "sigma_spike", "d_p": [0, 0, 0, 5.0, 0], "expect_accept": False},  # psi collapse
    {"name": "wrong_dim", "d_p": [1, 2, 3], "expect_error": ValueError},
    {"name": "nan_component", "d_p": [float("nan"), 0, 0, 0, 0], "expect_error": ValueError},
    {"name": "inf_component", "d_p": [float("inf"), 0, 0, 0, 0], "expect_accept": True},  # clamps
]


def run_edge_battery(link: Optional[CycleKernalLink] = None) -> Dict[str, Any]:
    """Run every edge case. Returns pass/fail per case plus aggregate."""
    link = link or CycleKernalLink()
    results = []
    for case in EDGE_CASES:
        name = case["name"]
        try:
            step = link.propose(case["d_p"])
            ok = step.accepted == case.get("expect_accept", True)
            results.append({"name": name, "pass": ok, "accepted": step.accepted,
                            "reason": step.reason, "norm_dp": round(step.norm_dp(), 6)})
        except Exception as e:
            ok = isinstance(e, case.get("expect_error", type(None)))
            results.append({"name": name, "pass": ok, "error": type(e).__name__})
    passed = sum(1 for r in results if r["pass"])
    return {"passed": passed, "total": len(results), "all_pass": passed == len(results), "cases": results}


if __name__ == "__main__":
    link = CycleKernalLink(epsilon=0.05)
    # Monte Carlo: 10k random steps, confirm no norm exceeds epsilon
    import random
    rng = random.Random(42)
    violations = 0
    for _ in range(10_000):
        d = [rng.uniform(-1, 1) for _ in range(5)]
        s = link.propose(d)
        if s.norm_dp() > s.epsilon + 1e-12:
            violations += 1
    bat = run_edge_battery(link)
    print(json.dumps({"monte_carlo_violations": violations, "edge_battery": bat,
                      "summary": link.summary()}, indent=2))
