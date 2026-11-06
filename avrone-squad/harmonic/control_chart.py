#!/usr/bin/env python3
"""Shewhart control chart for the homeostasis loop.
Western Electric rules on ratio history. Passive until BASELINE_N samples,
then three-sigma bands and run rules activate.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

BASELINE_N = 25
SIGMA_K = 3.0
RUN_LENGTH = 7  # Western Electric: 7 consecutive on one side of centerline


@dataclass
class ChartState:
    samples: list[float] = field(default_factory=list)
    active: bool = False
    centerline: float = 0.0
    sigma: float = 0.0
    ucl: float = 0.0
    lcl: float = 0.0
    alarms: list[str] = field(default_factory=list)

    def ingest(self, ratio: float) -> list[str]:
        """Record one ratio sample. Returns any alarms fired this step."""
        self.samples.append(ratio)
        if len(self.samples) >= BASELINE_N and not self.active:
            self._activate()
        if not self.active:
            return []
        return self._evaluate(ratio)

    def _activate(self) -> None:
        self.active = True
        self.centerline = statistics.mean(self.samples)
        self.sigma = statistics.pstdev(self.samples) or 1e-9
        self.ucl = self.centerline + SIGMA_K * self.sigma
        self.lcl = self.centerline - SIGMA_K * self.sigma

    def _evaluate(self, ratio: float) -> list[str]:
        fired: list[str] = []
        if ratio > self.ucl:
            fired.append(f"POINT_ABOVE_UCL ratio={ratio:.4f} ucl={self.ucl:.4f}")
        if ratio < self.lcl:
            fired.append(f"POINT_BELOW_LCL ratio={ratio:.4f} lcl={self.lcl:.4f}")
        # Western Electric rule: RUN_LENGTH consecutive on one side of centerline
        tail = self.samples[-RUN_LENGTH:]
        if len(tail) == RUN_LENGTH:
            if all(x > self.centerline for x in tail):
                fired.append(f"RUN_ABOVE_CENTERLINE last_{RUN_LENGTH} samples")
            elif all(x < self.centerline for x in tail):
                fired.append(f"RUN_BELOW_CENTERLINE last_{RUN_LENGTH} samples")
        # Trend: 6 consecutive increasing or decreasing
        if len(self.samples) >= 6:
            t = self.samples[-6:]
            if all(t[i] < t[i + 1] for i in range(5)):
                fired.append("TREND_INCREASING 6 consecutive")
            elif all(t[i] > t[i + 1] for i in range(5)):
                fired.append("TREND_DECREASING 6 consecutive")
        self.alarms.extend(fired)
        return fired

    def summary(self) -> dict:
        return {
            "n_samples": len(self.samples),
            "active": self.active,
            "baseline_needed": max(0, BASELINE_N - len(self.samples)),
            "centerline": round(self.centerline, 6) if self.active else None,
            "sigma": round(self.sigma, 6) if self.active else None,
            "ucl": round(self.ucl, 6) if self.active else None,
            "lcl": round(self.lcl, 6) if self.active else None,
            "alarm_count": len(self.alarms),
            "alarms": self.alarms[-10:],
        }
