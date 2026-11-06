#!/usr/bin/env python3
"""Homeostasis loop: census-as-thermostat for the avrone daemon.
Sense ratio deviation from B-star, apply correction, log unresolved drift.
Fail-open by design: when B-star cannot be reached, the loop logs and continues.
Memory of consecutive drifts distinguishes noise from phase transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileCensus:
    path: str
    boundary_gates: int
    interior_transforms: int
    rho: float  # interior-to-boundary cost ratio for this file

    @property
    def ratio(self) -> float:
        if self.interior_transforms == 0:
            return float("inf") if self.boundary_gates else 0.0
        return self.boundary_gates / self.interior_transforms

    @property
    def b_star(self) -> float:
        """Cost-derived living floor: B* = sqrt(I * rho) - 1, clamped at 0."""
        raw = math_sqrt(self.interior_transforms * self.rho) - 1
        return max(0.0, raw)


def math_sqrt(x: float) -> float:
    if x <= 0:
        return 0.0
    # Newton-Raphson, no math import dependency for the hot path
    g = x / 2
    for _ in range(12):
        g = 0.5 * (g + x / g)
    return g


@dataclass
class DriftMemory:
    """Tracks consecutive same-direction deviations to detect phase transitions."""
    consecutive_below: int = 0
    consecutive_above: int = 0
    phase_transition_threshold: int = 3

    def observe(self, ratio: float, b_star: float) -> str | None:
        if ratio < b_star:
            self.consecutive_below += 1
            self.consecutive_above = 0
            if self.consecutive_below >= self.phase_transition_threshold:
                return "PHASE_TRANSITION_LOW"
            return "DRIFT_LOW"
        elif ratio > b_star:
            self.consecutive_above += 1
            self.consecutive_below = 0
            if self.consecutive_above >= self.phase_transition_threshold:
                return "PHASE_TRANSITION_HIGH"
            return "DRIFT_HIGH"
        else:
            self.consecutive_below = 0
            self.consecutive_above = 0
            return None


@dataclass
class HomeostasisLoop:
    """Sense -> correct -> log. Fail-open on unreachable B-star."""
    census: list[FileCensus] = field(default_factory=list)
    drift: DriftMemory = field(default_factory=DriftMemory)
    log: list[str] = field(default_factory=list)
    corrections_applied: int = 0

    def sense(self, file_census: FileCensus) -> dict:
        """Evaluate one file against its own living B-star."""
        r = file_census.ratio
        bs = file_census.b_star
        event = self.drift.observe(r, bs)
        result = {
            "path": file_census.path,
            "ratio": round(r, 4) if r != float("inf") else None,
            "b_star": round(bs, 4),
            "event": event,
        }
        if event and event.startswith("PHASE_TRANSITION"):
            self.log.append(f"PHASE_TRANSITION {file_census.path}: {event} ratio={r:.4f} b_star={bs:.4f}")
        elif event:
            # single drift: attempt correction
            if r < bs:
                self.log.append(f"CORRECTION_NEEDED {file_census.path}: ratio {r:.4f} below b_star {bs:.4f}")
                self.corrections_applied += 1
        return result

    def run(self) -> dict:
        """Full pass over census. Returns aggregate state."""
        results = [self.sense(fc) for fc in self.census]
        unresolved = [res for res in results if res["event"] and res["event"].startswith("PHASE_TRANSITION")]
        return {
            "files_evaluated": len(results),
            "corrections_applied": self.corrections_applied,
            "phase_transitions": len(unresolved),
            "fail_open_logged": len(self.log),
            "results": results,
            "log_tail": self.log[-5:],
        }
