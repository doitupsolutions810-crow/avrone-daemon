"""avrone_cyclekernal_link.py (patched)

Patch notes from extended descent (20-case battery + 100k Monte Carlo + formal claim check):
  1. METRIC BUG (found): EvolutionStep.norm() reported the PROPOSED dP magnitude,
     not the APPLIED delta. Over-bound steps looked like 100k 'violations' in MC
     even though the gate clamped every one. Fixed: norm reports ||p_new-p_old||.
  2. TEST BUG (found): sigma_spike was expected to hard-reject, but epsilon clamping
     reduced the sigma delta to 0.05 before psi was evaluated, so psi stayed 0.45.
     The gate did its job; the expectation was wrong. Psi-collapse rejection is the
     SECOND line of defense, for when clamping alone is insufficient.
  3. FORMAL CLAIM (corrected): accepted iff psi_after >= floor. Over-bound steps
     are clamped and applied, not rejected. Rejection is reserved for psi collapse.
"""
from __future__ import annotations

import hashlib, json, math, time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

State = Tuple[float, float, float, float, float]

def clamp_state(p: State) -> State:
    r, d, c, s, t = p
    return (min(1.0, max(0.0, r)), max(0.0, d), max(0.0, c), max(0.0, s), max(0.0, t))

def l2_norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in v))

def evolution_bounded(p_old: State, d_p: State, epsilon: float) -> State:
    if epsilon < 0:
        raise ValueError(f"epsilon must be >= 0, got {epsilon}")
    n = l2_norm(d_p)
    if n == 0.0:
        return clamp_state(p_old)
    if n <= epsilon:
        stepped = tuple(a + b for a, b in zip(p_old, d_p))
    else:
        stepped = tuple(a + b * (epsilon / n) for a, b in zip(p_old, d_p))
    return clamp_state(stepped)

def stability_score(p: State, H: float) -> float:
    return H - p[3]

def participation_from_r(r: float, base: float = 1.0) -> float:
    return base * r

@dataclass
class EvolutionStep:
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
        """||dP|| of the PROPOSAL (may exceed epsilon)."""
        return l2_norm(self.d_p)

    def norm_applied(self) -> float:
        """||p_new - p_old|| of what was ACTUALLY applied (always <= epsilon)."""
        return l2_norm(tuple(b - a for a, b in zip(self.p_old, self.p_new)))

@dataclass
class EvidenceRecord:
    seq: int
    kind: str
    payload: Dict[str, Any]
    prev_hash: str
    ts: float = field(default_factory=time.time)

    def digest(self) -> str:
        body = json.dumps(
            {"seq": self.seq, "kind": self.kind, "payload": self.payload,
             "prev_hash": self.prev_hash, "ts": self.ts},
            sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(body).hexdigest()

class CycleKernalLink:
    def __init__(self, epsilon: float = 0.05, psi_floor: float = -1.0):
        if epsilon < 0:
            raise ValueError("epsilon >= 0 required")
        self.epsilon = epsilon
        self.psi_floor = psi_floor
        self.p: State = (0.5, 0.01, 0.0, 0.0, 0.1)
        self.history: List[EvolutionStep] = []
        self.ledger: List[EvidenceRecord] = []
        self._seq = 0
        self._emit("genesis", {"p": list(self.p), "epsilon": epsilon, "psi_floor": psi_floor})

    def _emit(self, kind: str, payload: Dict[str, Any]) -> EvidenceRecord:
        prev = self.ledger[-1].digest() if self.ledger else "0" * 64
        rec = EvidenceRecord(seq=self._seq, kind=kind, payload=payload, prev_hash=prev)
        self.ledger.append(rec)
        self._seq += 1
        return rec

    def propose(self, d_p: Sequence[float], H: Optional[float] = None) -> EvolutionStep:
        if len(d_p) != 5:
            raise ValueError(f"dP must have 5 components, got {len(d_p)}")
        # Non-finite input is a hard error: inf in state poisons every downstream check.
        for x in d_p:
            if not math.isfinite(x):
                raise ValueError(f"non-finite component: {x}")
        d = tuple(float(x) for x in d_p)
        H = participation_from_r(self.p[0]) if H is None else float(H)
        psi_before = stability_score(self.p, H)
        p_new = evolution_bounded(self.p, d, self.epsilon)
        psi_after = stability_score(p_new, participation_from_r(p_new[0]))
        accepted, reason = True, "within_bound"
        if psi_after < self.psi_floor:
            accepted, reason = False, f"psi_collapse {psi_after:.4f} < floor {self.psi_floor}"
            p_new, psi_after = self.p, psi_before
        step = EvolutionStep(self.p, d, p_new, self.epsilon, psi_before, psi_after, accepted, reason)
        self.history.append(step)
        if accepted:
            self.p = p_new
        self._emit("evolve", {"accepted": accepted, "reason": reason,
            "norm_dp": round(step.norm_dp(), 6), "norm_applied": round(step.norm_applied(), 6),
            "psi_before": psi_before, "psi_after": psi_after})
        return step

    def verify_ledger(self) -> bool:
        prev = "0" * 64
        for rec in self.ledger:
            if rec.prev_hash != prev:
                return False
            prev = rec.digest()
        return True

    def summary(self) -> Dict[str, Any]:
        acc = [s for s in self.history if s.accepted]
        rej = [s for s in self.history if not s.accepted]
        return {"p": list(self.p), "epsilon": self.epsilon, "psi_floor": self.psi_floor,
            "steps": len(self.history), "accepted": len(acc), "rejected": len(rej),
            "psi": stability_score(self.p, participation_from_r(self.p[0])),
            "ledger_len": len(self.ledger), "ledger_valid": self.verify_ledger(),
            "max_norm_dp": max((s.norm_dp() for s in self.history), default=0.0),
            "max_norm_applied": max((s.norm_applied() for s in self.history), default=0.0)}

# Psi-collapse battery: cases where EVEN AFTER epsilon clamping, psi still falls below floor.
# These require large epsilon so clamping doesn't save the step.
PSI_COLLAPSE_CASES = [
    # eps=2.0: sigma delta 3.0 clamps to 2.0 -> psi = 0.5-2.0 = -1.5 < -1.0 -> reject
    {"name": "psi_collapse_sigma", "eps": 2.0, "floor": -1.0, "d_p": [0, 0, 0, 3.0, 0], "expect_accept": False},
    # eps=1.5: sigma 2.0 clamps to 1.5 -> psi = 0.5-1.5 = -1.0 >= -1.0 -> accept (boundary)
    {"name": "psi_boundary", "eps": 1.5, "floor": -1.0, "d_p": [0, 0, 0, 2.0, 0], "expect_accept": True},
    # eps=3.0, floor=0.0: sigma 2.0 clamps to 2.0 -> psi = 0.5-2.0 = -1.5 < 0 -> reject
    {"name": "psi_collapse_high_floor", "eps": 3.0, "floor": 0.0, "d_p": [0, 0, 0, 2.0, 0], "expect_accept": False},
]

def run_psi_battery() -> Dict[str, Any]:
    results = []
    for ca in PSI_COLLAPSE_CASES:
        link = CycleKernalLink(epsilon=ca["eps"], psi_floor=ca["floor"])
        try:
            s = link.propose(ca["d_p"])
            ok = s.accepted == ca["expect_accept"]
            results.append({"name": ca["name"], "pass": ok, "accepted": s.accepted, "reason": s.reason,
                            "norm_applied": round(s.norm_applied(), 6), "psi_after": round(s.psi_after, 6)})
        except Exception as e:
            results.append({"name": ca["name"], "pass": False, "error": type(e).__name__})
    passed = sum(1 for r in results if r["pass"])
    return {"passed": passed, "total": len(results), "all_pass": passed == len(results), "cases": results}

if __name__ == "__main__":
    import random
    link = CycleKernalLink(epsilon=0.05)
    rng = random.Random(42)
    v_prop = v_app = 0
    for _ in range(10_000):
        d = [rng.uniform(-1, 1) for _ in range(5)]
        s = link.propose(d)
        if s.norm_dp() > s.epsilon + 1e-12: v_prop += 1
        if s.norm_applied() > s.epsilon + 1e-12: v_app += 1
    print(json.dumps({"proposal_norm_exceedances": v_prop, "applied_norm_exceedances": v_app,
                      "psi_battery": run_psi_battery(), "summary": link.summary()}, indent=2))
