"""Great Scale D9 projection/reflection diagnostic; mathematical, not physical proof."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Sequence

N = 9

def _vector(values: Sequence[float]) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)) or len(values) != N:
        raise ValueError("expected exactly nine real components")
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("components must be real numbers, not booleans")
        try:
            v = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("component outside finite real range") from exc
        if not math.isfinite(v):
            raise ValueError("components must be finite")
        result.append(v)
    return tuple(result)

def rotate(values: Sequence[float], steps: int = 1) -> tuple[float, ...]:
    x = _vector(values)
    if isinstance(steps, bool) or not isinstance(steps, int):
        raise ValueError("steps must be an integer")
    return tuple(x[(j - steps) % N] for j in range(N))

def reflect(values: Sequence[float]) -> tuple[float, ...]:
    x = _vector(values)
    return tuple(x[(-j) % N] for j in range(N))

def project_mean(values: Sequence[float]) -> tuple[float, ...]:
    x = _vector(values)
    mean = math.fsum(x) / N
    return (mean,) * N

def residual(values: Sequence[float]) -> tuple[float, ...]:
    x = _vector(values)
    p = project_mean(x)
    return tuple(a - b for a, b in zip(x, p))

def norm_sq(values: Sequence[float]) -> float:
    x = _vector(values)
    return math.fsum(v * v for v in x)

def orientation_witness(values: Sequence[float]) -> float:
    """Reflection-odd first sine coefficient; not rotation invariant."""
    x = _vector(values)
    return math.fsum(x[j] * math.sin(2 * math.pi * j / N) for j in range(N))

def heat(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("heat input must be finite")
    try:
        v = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("heat input must be finite") from exc
    if not math.isfinite(v) or not 0 <= v <= 1:
        raise ValueError("heat input must be in [0,1]")
    return v * (1 - v)

@dataclass(frozen=True)
class WitnessReport:
    n: int
    mean: float
    residual_energy: float
    projection_commutator_error: float
    reflection_energy_error: float
    reflection_odd_witness: float
    reflected_witness: float
    evidence_sha256: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, allow_nan=False, separators=(",", ":"))

def analyze(values: Sequence[float]) -> WitnessReport:
    x = _vector(values)
    px = project_mean(x)
    rpx = reflect(px)
    prx = project_mean(reflect(x))
    qx = residual(x)
    rx = reflect(x)
    raw = json.dumps(list(x), allow_nan=False, separators=(",", ":")).encode("utf-8")
    return WitnessReport(
        n=N, mean=px[0], residual_energy=norm_sq(qx),
        projection_commutator_error=math.sqrt(math.fsum((a-b)**2 for a,b in zip(rpx,prx))),
        reflection_energy_error=abs(norm_sq(qx)-norm_sq(residual(rx))),
        reflection_odd_witness=orientation_witness(x),
        reflected_witness=orientation_witness(rx),
        evidence_sha256=hashlib.sha256(raw).hexdigest(),
    )

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ninefold projection and reflection witness")
    parser.add_argument("values", type=float, nargs=9)
    args = parser.parse_args()
    print(analyze(args.values).to_json())
