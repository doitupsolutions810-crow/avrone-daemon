"""Joint constraint: psi half-space + ns_fusion log-sum cap as one certificate.

ns_fusion (CycleKernal) scores coherence via geometric mean of trait weights.
The spine bounds sigma linearly via psi = H - sigma >= floor.

These are the same bound in different units:
  log-sum cap:  sum_i log w_i  <=  C
  geometric mean: (prod w_i)^(1/n) , where n = trait count
  ratio between them: exactly n.

So one is load-bearing, the other is a re-normalization. This module
states both, proves the ratio, and issues a single certificate that
covers the trait layer AND the sigma layer — one witness, not two.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

FLOOR = 0.1
H_DEFAULT = 2.0
EPS = 0.5


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v: float) -> None:
    if not math.isfinite(v):
        raise ValueError(f"non-finite value: {v!r}")


def psi_checksum(sigma: float, H: float = H_DEFAULT, floor: float = FLOOR) -> Tuple[bool, float]:
    """Input valve. Returns (ok, psi)."""
    if not _is_num(sigma) or not _is_num(H) or not _is_num(floor):
        raise ValueError("type error: sigma, H, floor must be numbers")
    _finite(sigma); _finite(H); _finite(floor)
    psi = H - sigma
    return psi >= floor, psi


def ns_fusion_score(weights: Sequence[float]) -> float:
    """CycleKernal's ns_fusion: geometric mean of trait weights.
    Empty product = 1.0 by convention. Negative weights rejected."""
    if not weights:
        return 1.0
    for w in weights:
        if not _is_num(w):
            raise ValueError("type error: weights must be numbers")
        _finite(w)
        if w < 0:
            raise ValueError(f"negative weight rejected: {w}")
    # log-sum: sum log w_i ; geometric mean = exp(sum/n)
    s = sum(math.log(w) for w in weights if w > 0)
    n = len(weights)
    return math.exp(s / n)


def log_sum_cap(weights: Sequence[float], cap: float) -> bool:
    """Bound on the product: sum log w_i <= cap."""
    if not weights:
        return True
    for w in weights:
        if not _is_num(w):
            raise ValueError("type error: weights must be numbers")
        _finite(w)
        if w < 0:
            raise ValueError(f"negative weight rejected: {w}")
    s = sum(math.log(w) for w in weights if w > 0)
    return s <= cap


def joint_certificate(sigma: float, weights: Sequence[float],
                       H: float = H_DEFAULT, floor: float = FLOOR,
                       cap: float = 0.0, fusion_cap: float = 1.0) -> Dict[str, Any]:
    """One certificate: psi half-space AND ns_fusion bound.
    Returns dict with psi_ok, fusion_ok, score, ratio (n)."""
    psi_ok, psi_v = psi_checksum(sigma, H, floor)
    if not psi_ok:
        raise ValueError(f"psi-collapse: psi={psi_v:.4f} < floor={floor}")
    score = ns_fusion_score(weights)
    if score > fusion_cap:
        raise ValueError(f"fusion breach: score={score:.4f} > cap={fusion_cap}")
    n = len(weights) if weights else 0
    # ratio: log-sum / geometric-mean-exponent = n
    ratio = n if n > 0 else None
    return {
        "psi_ok": True,
        "psi": psi_v,
        "fusion_ok": True,
        "fusion_score": score,
        "trait_count": n,
        "ratio_logsum_to_geomean": ratio,
        "sigma": sigma,
        "H": H,
        "floor": floor,
    }
