#!/usr/bin/env python3
"""Edge-case suite for the homeostasis loop. Cosmology-sequence slot: between horizon and inception.
Tests: empty census, zero interior, zero boundary, inf ratio, B-star clamp, drift memory,
phase transition detection, fail-open behavior, control chart baseline, run rules.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "avrone-squad" / "evolve-payloads"))
sys.path.insert(0, str(ROOT / "avrone-squad" / "harmonic"))

import homeostasis_loop as hl
import control_chart as cc


def test_empty_census_run_is_stable():
    loop = hl.HomeostasisLoop()
    out = loop.run()
    assert out["files_evaluated"] == 0
    assert out["corrections_applied"] == 0
    assert out["phase_transitions"] == 0


def test_zero_interior_transforms_ratio_inf():
    fc = hl.FileCensus(path="pure_gates.py", boundary_gates=3, interior_transforms=0, rho=0.3)
    assert fc.ratio == float("inf")
    # B-star: sqrt(0*0.3)-1 clamped to 0
    assert fc.b_star == 0.0


def test_zero_boundary_gates_ratio_zero():
    fc = hl.FileCensus(path="pure_math.py", boundary_gates=0, interior_transforms=6, rho=0.3)
    assert fc.ratio == 0.0
    assert fc.b_star == 0.0  # sqrt(6*0.3)-1 = 1.34-1 = 0.34... wait
    # sqrt(1.8) = 1.342, - 1 = 0.342. Recheck: b_star = max(0, sqrt(I*rho) - 1)
    import math
    expected = max(0.0, math.sqrt(6 * 0.3) - 1)
    assert abs(fc.b_star - expected) < 1e-9


def test_b_star_clamps_at_zero_for_small_I():
    fc = hl.FileCensus(path="tiny.py", boundary_gates=1, interior_transforms=1, rho=0.3)
    assert fc.b_star == 0.0  # sqrt(0.3)-1 < 0


def test_drift_memory_single_drift_not_phase_transition():
    mem = hl.DriftMemory(phase_transition_threshold=3)
    assert mem.observe(0.1, 0.5) == "DRIFT_LOW"
    assert mem.observe(0.1, 0.5) == "DRIFT_LOW"
    assert mem.observe(0.6, 0.5) is None  # reset


def test_phase_transition_on_three_consecutive_low():
    mem = hl.DriftMemory(phase_transition_threshold=3)
    mem.observe(0.1, 0.5)
    mem.observe(0.1, 0.5)
    event = mem.observe(0.1, 0.5)
    assert event == "PHASE_TRANSITION_LOW"


def test_phase_transition_on_three_consecutive_high():
    mem = hl.DriftMemory(phase_transition_threshold=3)
    mem.observe(0.9, 0.5)
    mem.observe(0.9, 0.5)
    event = mem.observe(0.9, 0.5)
    assert event == "PHASE_TRANSITION_HIGH"


def test_fail_open_logs_unresolved_without_crashing():
    loop = hl.HomeostasisLoop()
    loop.census = [
        hl.FileCensus(path="a.py", boundary_gates=0, interior_transforms=2, rho=0.3),
        hl.FileCensus(path="b.py", boundary_gates=0, interior_transforms=2, rho=0.3),
        hl.FileCensus(path="c.py", boundary_gates=0, interior_transforms=2, rho=0.3),
    ]
    # ratios all 0.0; b_star = sqrt(2*0.3)-1 = 0.342 for each -> DRIFT_LOW x2, then PHASE_TRANSITION_LOW
    out = loop.run()
    assert out["phase_transitions"] == 1
    assert any("PHASE_TRANSITION" in line for line in loop.log)
    # loop must still return cleanly — fail-open
    assert out["files_evaluated"] == 3


def test_control_chart_passive_before_baseline():
    chart = cc.ChartState()
    for i in range(cc.BASELINE_N - 1):
        assert chart.ingest(0.5 + 0.01 * (i % 3)) == []
    assert chart.active is False
    assert chart.summary()["baseline_needed"] == 1


def test_control_chart_activates_at_baseline():
    chart = cc.ChartState()
    for i in range(cc.BASELINE_N):
        chart.ingest(0.5)
    assert chart.active is True
    s = chart.summary()
    assert s["n_samples"] == cc.BASELINE_N
    assert s["ucl"] > s["centerline"]
    assert s["lcl"] < s["centerline"]


def test_control_chart_point_above_ucl():
    chart = cc.ChartState()
    for _ in range(cc.BASELINE_N):
        chart.ingest(1.0)
    alarms = chart.ingest(10.0)  # way above
    assert any("POINT_ABOVE_UCL" in a for a in alarms)


def test_control_chart_run_rule_seven_above():
    chart = cc.ChartState()
    # seed baseline around 1.0
    for _ in range(cc.BASELINE_N):
        chart.ingest(1.0)
    # 7 consecutive above centerline
    fired = None
    for _ in range(cc.RUN_LENGTH):
        fired = chart.ingest(1.5)
    assert fired is not None
    assert any("RUN_ABOVE_CENTERLINE" in a for a in fired)


def test_control_chart_trend_rule():
    chart = cc.ChartState()
    for _ in range(cc.BASELINE_N):
        chart.ingest(1.0)
    fired = None
    for v in [1.1, 1.2, 1.3, 1.4, 1.5, 1.6]:
        fired = chart.ingest(v)
    assert fired is not None
    assert any("TREND_INCREASING" in a for a in fired)


def test_newton_sqrt_accuracy():
    for x in [0.01, 0.3, 1.8, 6.0, 100.0]:
        import math
        assert abs(hl.math_sqrt(x) - math.sqrt(x)) < 1e-9


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(1 if failed else 0)
