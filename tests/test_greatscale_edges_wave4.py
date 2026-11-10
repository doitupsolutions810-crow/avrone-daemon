"""Wave 4: longer Great Scale edge cases for avrone-daemon.

Focus: numerical extremes, multi-silo pooling algebra, tool surface under
concurrent load, and invariants that the fusion layer will need when it
begins emitting evidence snapshots.
"""
from __future__ import annotations

import concurrent.futures
import json
import math

import pytest

from avrone import greatscale as gs


def jeffreys(f: int, r: int) -> float:
    return (f + 0.5) / (r + 1)


def pool(*snaps: dict) -> dict:
    f = sum(s["failures"] for s in snaps)
    r = sum(s["runs"] for s in snaps)
    return gs.evidence_snapshot(f, r)


# ---- pooling algebra under mixed orientations --------------------------------

def test_three_silo_pool_mixed_orientations():
    a = gs.evidence_snapshot(10, 10)   # fail
    b = gs.evidence_snapshot(0, 10)    # pass
    c = gs.evidence_snapshot(5, 10)    # mixed
    p = pool(a, b, c)
    assert p["failures"] == 15 and p["runs"] == 30
    assert p["p"] == 0.5
    assert p["S"] == 0.25
    assert p["orientation"] == "mixed"
    assert not p["onConeFloor"]


def test_pool_of_identical_floor_silos_stays_on_floor():
    snaps = [gs.evidence_snapshot(0, 5) for _ in range(8)]
    p = pool(*snaps)
    assert p["onConeFloor"] and p["orientation"] == "pass"
    assert p["runs"] == 40


def test_jeffreys_descends_monotonically_with_confirmations():
    prev_psi = 1.0
    for r in (1, 2, 4, 8, 16, 32, 64, 128):
        p = jeffreys(r, r)
        psi = gs.psi(gs.heat(p))
        assert psi < prev_psi
        assert psi >= gs.CONE - 1e-12
        prev_psi = psi


# ---- numerical extremes ------------------------------------------------------

def test_heat_symmetry_dense_grid():
    for i in range(0, 1001):
        p = i / 1000
        assert gs.heat(p) == pytest.approx(gs.heat(1 - p), abs=1e-15)


def test_psi_strictly_above_cone_off_floor():
    for i in range(1, 1000):
        p = i / 1000
        assert gs.psi(gs.heat(p)) > gs.CONE + 1e-15


def test_runs_to_detect_p_equals_alpha():
    # when p == alpha the formula still returns a finite positive N
    n = gs.runs_to_detect(0.05, 0.05)
    assert n is not None and n >= 1
    assert 1 - (1 - 0.05) ** n >= 0.95 - 1e-12


def test_detect_in_runs_at_p_one_is_one():
    snap = gs.evidence_snapshot(50, 50)
    assert snap["detectInRuns"] == 1.0


def test_detect_in_runs_at_p_zero_is_zero():
    snap = gs.evidence_snapshot(0, 50)
    assert snap["detectInRuns"] == 0.0


# ---- concurrent tool surface -------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_tool_calls_identical_json():
    async def one():
        return await gs.t_great_scale(None, {"failures": 7, "runs": 100, "format": "json"})

    import asyncio
    results = await asyncio.gather(*[one() for _ in range(16)])
    first = json.loads(results[0])
    for r in results[1:]:
        assert json.loads(r) == first


def test_concurrent_snapshot_under_thread_pool():
    def one(_):
        return gs.evidence_snapshot(3, 17)

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        out = list(ex.map(one, range(64)))
    assert all(o == out[0] for o in out)


# ---- cascade / residual identities under extremes ----------------------------

def test_cascade_lambda_at_partition_blocks():
    for block in gs.RESIDUAL_PARTITION:
        n = float(block)
        lam = gs.cascade_lambda(n)
        assert 0 < lam <= 1
        assert lam == pytest.approx(gs.PHI ** (-n))


def test_residual_count_linear():
    for s in (0.0, 0.05, 0.1, 0.2, 0.25):
        assert gs.residual_count(s) == pytest.approx(8 - 4 * s)


# ---- soft_contract under hostile inputs --------------------------------------

@pytest.mark.parametrize("x", [float("nan"), float("inf"), float("-inf")])
def test_soft_contract_non_finite(x):
    # implementation uses % which yields NaN for non-finite; result must not raise
    try:
        y = gs.soft_contract(x)
        assert y != y or not math.isfinite(y)  # NaN or non-finite is acceptable
    except (ValueError, OverflowError):
        pass  # also acceptable: explicit rejection


def test_soft_contract_large_negative_mod():
    y = gs.soft_contract(-1e18)
    assert 2.25 - 1e-6 <= y <= 6.75 + 1e-6


# ---- evidence_snapshot alpha grid --------------------------------------------

@pytest.mark.parametrize("alpha", [1e-9, 1e-6, 0.001, 0.01, 0.05, 0.1, 0.25, 0.5])
def test_alpha_grid_runs_for_95_monotone(alpha):
    n = gs.runs_to_detect(0.1, alpha)
    assert n is not None and n >= 1
    # smaller alpha => larger or equal N
    n_looser = gs.runs_to_detect(0.1, min(0.99, alpha * 2))
    assert n >= n_looser or abs(n - n_looser) <= 1
