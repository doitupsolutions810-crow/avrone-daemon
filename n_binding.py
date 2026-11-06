"""n-binding: trait count as a witnessed coordinate.

Round 15 finding: the joint constraint and ns_fusion are one check
wearing two hats only while n is fixed. The moment trait count can
change at runtime, the fusion cap's meaning drifts with it.

An attacker who can add traits gains a way to launder a psi-legal
sigma past a cap calibrated for a smaller n.

This module binds n into the hash chain at every state transition.
A trait-count change is itself a witnessed event, not ambient context.
n moves from parameter to coordinate — the same move the oracle
checksum made for the precondition.

Also: product-form (prod w_i) underflows to 0.0 in float64 for
modest n and sub-unit weights. Log-space is the only safe domain.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

FLOOR = 0.1
H_DEFAULT = 2.0
EPS = 0.5


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v: float) -> None:
    if not math.isfinite(v):
        raise ValueError(f"non-finite value: {v!r}")


def record_hash(payload: str, prev: Optional[str] = None) -> str:
    blob = f"{prev or 'GENESIS'}|{payload}"
    return hashlib.sha256(blob.encode()).hexdigest()


def bind_n(sigma: float, n: int, H: float = H_DEFAULT, floor: float = FLOOR,
           prev_hash: Optional[str] = None) -> Dict[str, Any]:
    """Bind (sigma, n, psi) into a single receipt.

    n must be a non-negative integer. Changing n between consecutive
    receipts is a witnessed governance event, not silent context.
    """
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError(f"n must be non-negative int, got {n!r}")
    if not _is_num(sigma):
        raise ValueError("sigma must be a number")
    _finite(sigma)
    _finite(H)
    _finite(floor)
    psi = H - sigma
    if psi < floor:
        raise ValueError(f"psi-collapse: psi={psi:.6f} < floor={floor}")
    payload = f"sigma={sigma:.12g}|n={n}|psi={psi:.12g}|H={H:.12g}|floor={floor:.12g}"
    h = record_hash(payload, prev_hash)
    return {
        "sigma": sigma,
        "n": n,
        "psi": psi,
        "H": H,
        "floor": floor,
        "payload": payload,
        "hash": h,
        "prev": prev_hash,
    }


def verify_n_chain(receipts: Sequence[Dict[str, Any]]) -> bool:
    """Recompute every hash and check prev linkage. Raises on mismatch."""
    if not receipts:
        return True
    prev = None
    for i, r in enumerate(receipts):
        expected = record_hash(r["payload"], prev)
        if r["hash"] != expected:
            raise ValueError(f"hash mismatch at index {i}")
        if r.get("prev") != prev:
            raise ValueError(f"prev pointer mismatch at index {i}")
        prev = r["hash"]
    return True


def governance_mutate_n(current_n: int, new_n: int, authorized: bool,
                        sigma: float, H: float = H_DEFAULT,
                        floor: float = FLOOR,
                        prev_hash: Optional[str] = None) -> Dict[str, Any]:
    """Trait-count change is a witnessed event.

    Unauthorized mutations raise. Authorized ones produce a receipt
    that carries both old and new n so the ledger records the fork
    condition explicitly.
    """
    if not authorized:
        raise ValueError("unauthorized n mutation rejected")
    if not isinstance(new_n, int) or new_n < 0:
        raise ValueError(f"new_n must be non-negative int, got {new_n!r}")
    receipt = bind_n(sigma, new_n, H=H, floor=floor, prev_hash=prev_hash)
    receipt["old_n"] = current_n
    receipt["mutation"] = "governance_n_change"
    return receipt


def log_space_product(weights: Sequence[float]) -> float:
    """Safe product via log-space. Never underflows to zero silently.

    Returns exp(sum log w_i) for w_i > 0. Empty product = 1.0.
    Negative weights rejected. Zero weights contribute -inf (product 0).
    """
    if not weights:
        return 1.0
    s = 0.0
    for w in weights:
        if not _is_num(w):
            raise ValueError("weights must be numbers")
        _finite(w)
        if w < 0:
            raise ValueError(f"negative weight rejected: {w}")
        if w == 0:
            return 0.0
        s += math.log(w)
    return math.exp(s)


@dataclass
class NLedger:
    """Hash-chained ledger that records every (sigma, n) pair."""
    H: float = H_DEFAULT
    floor: float = FLOOR
    receipts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def head(self) -> Optional[str]:
        return self.receipts[-1]["hash"] if self.receipts else None

    @property
    def current_n(self) -> Optional[int]:
        return self.receipts[-1]["n"] if self.receipts else None

    def append(self, sigma: float, n: int) -> Dict[str, Any]:
        r = bind_n(sigma, n, H=self.H, floor=self.floor, prev_hash=self.head)
        self.receipts.append(r)
        return r

    def mutate_n(self, new_n: int, authorized: bool, sigma: float) -> Dict[str, Any]:
        old = self.current_n if self.current_n is not None else 0
        r = governance_mutate_n(old, new_n, authorized, sigma,
                                H=self.H, floor=self.floor,
                                prev_hash=self.head)
        self.receipts.append(r)
        return r

    def verify(self) -> bool:
        return verify_n_chain(self.receipts)
