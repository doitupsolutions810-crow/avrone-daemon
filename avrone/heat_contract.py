"""Heat contract: map any bridge post to a scalar in [2.25, 6.25].

Great Scale's soft contract: FIXED = 4.5, delta = 0.5 * (m - 4.5), clamped.
The scalar is the post's residual heat — the same quantity CycleKernal's
trait engine should scale nudges by. A deterministic green check (p=0)
maps near the cone floor; a flaky check (p=1/2) maps to max pressure.

This module is pure: no I/O, no imports beyond the standard library beyond
math. It is the single source of truth for heat, so bridge.py and fleet.py
both read the same function instead of each inventing one.
"""
from __future__ import annotations

import math
from typing import Any

FIXED = 4.5
SPAN_LO, SPAN_HI = 2.25, 6.25

# Kind weights: how much epistemic weight a post carries when it is the
# subject of a trait nudge. Conclusions anchor; questions probe.
KIND_WEIGHT: dict[str, float] = {
    "finding": 0.5,
    "question": 0.4,
    "answer": 0.6,
    "correction": 0.75,
    "conclusion": 1.0,
}


def soft_contract(m: float) -> float:
    """Map payload score m to the clamped soft-contract scalar."""
    return max(SPAN_LO, min(SPAN_HI, FIXED + 0.5 * (m - FIXED)))


def bernoulli_heat(p: float) -> float:
    """S = p(1-p). Variance of a binary check outcome; peaks at p = 1/2."""
    p = max(0.0, min(1.0, float(p)))
    return p * (1.0 - p)


def heat_field(post: dict[str, Any], p: float = 0.5) -> float:
    """Compute the heat field for one post.

    m = (len(text) mod 9) + kind_weight * 9, then soft_contract, then
    scaled by the post's Bernoulli heat at failure rate p. The result
    lives in [0, 6.25]: zero at the cone floor (p = 0 or p = 1),
    max 6.25 * 0.25 = 1.5625 at p = 1/2 with a conclusion-weight post
    at the top of the soft-contract range.
    """
    text = str(post.get("text") or "")
    kind = str(post.get("kind") or "finding").lower()
    w = KIND_WEIGHT.get(kind, 0.5)
    m = (len(text) % 9) + w * 9
    scalar = soft_contract(m)
    return scalar * bernoulli_heat(p)


def mean_heat(posts: list[dict[str, Any]], p: float = 0.5) -> float:
    """Mean heat across a batch; 0.0 on empty input."""
    if not posts:
        return 0.0
    return sum(heat_field(x, p) for x in posts) / len(posts)
