"""Heat contract: S = p(1-p), soft_contract band, kind weights, post_heat.

Pure math, no I/O. Peer of avrone.greatscale.heat / soft_contract.
"""
from __future__ import annotations

KIND_WEIGHT = {"finding": 0.5, "question": 0.25, "answer": 0.5,
               "correction": 0.75, "conclusion": 1.0}
FIXED = 4.5


def heat(p: float) -> float:
    x = max(0.0, min(1.0, float(p)))
    return x * (1.0 - x)


def soft_contract(x: float, k: float = 0.5) -> float:
    m = ((x % 9) + 9) % 9
    return FIXED + k * (m - FIXED)


def post_heat(kind: str, text: str) -> float:
    w = KIND_WEIGHT.get(str(kind or "finding").lower(), 0.5)
    return w * soft_contract(len(text or ""))
