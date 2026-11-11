"""Exact floating-point threshold tests for Great Scale sample requirements."""
import math
import random
from decimal import Decimal, localcontext, ROUND_CEILING
import pytest
from avrone.greatscale import runs_to_detect

@pytest.mark.parametrize("n", [1, 2, 3, 10, 50, 200, 1000])
def test_exact_and_one_ulp_below_half_probability_boundary(n):
    alpha = math.ldexp(1.0, -n)
    assert runs_to_detect(0.5, alpha) == n
    assert runs_to_detect(0.5, math.nextafter(alpha, 0.0)) == n + 1

@pytest.mark.parametrize("p", [0.5, 0.1, 0.06, 1e-9, 1e-17, math.ulp(0.0)])
def test_small_rates_are_finite_integer_samples(p):
    out = runs_to_detect(p)
    assert isinstance(out, int) and out > 0

@pytest.mark.parametrize("alpha", [0.5, 0.25, 0.125, math.nextafter(0.25, 0.0)])
def test_half_probability_exact_reference(alpha):
    # Exact decimal powers for the dyadic p=1/2.
    with localcontext() as ctx:
        ctx.prec = 100
        expected = 1
        while Decimal("0.5") ** expected > Decimal.from_float(alpha):
            expected += 1
    assert runs_to_detect(0.5, alpha) == expected

def test_seeded_decimal_reference_1500_cases():
    rng = random.Random(90210)
    for i in range(1500):
        p = rng.uniform(0.001, 0.99)
        alpha = rng.uniform(0.00001, 0.999999)
        with localcontext() as ctx:
            ctx.prec = 100
            dp = Decimal.from_float(p)
            da = Decimal.from_float(alpha)
            q = Decimal(1) - dp
            expected = max(1, int((da.ln() / q.ln()).to_integral_value(rounding=ROUND_CEILING)))
            while q ** expected > da:
                expected += 1
            while expected > 1 and q ** (expected - 1) <= da:
                expected -= 1
        assert runs_to_detect(p, alpha) == expected, (i, p, alpha, expected)
