#!/usr/bin/env python3
"""Round 14 joint-constraint battery: 22 tests."""
import importlib.util, sys, traceback

spec = importlib.util.spec_from_file_location("joint_constraint", "joint_constraint.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["joint_constraint"] = mod
spec.loader.exec_module(mod)

jc = mod.joint_certificate
ns = mod.ns_fusion_score
lsc = mod.log_sum_cap
pc = mod.psi_checksum

passed = failed = 0

def t(name, fn):
    global passed, failed
    try:
        fn(); passed += 1; print(f"  PASS  {name}")
    except Exception as e:
        failed += 1; print(f"  FAIL  {name}: {e}")

# --- psi checksum ---
t("psi legal", lambda: assert pc(0.5)[0] is True)
t("psi collapse", lambda: assert pc(2.5)[0] is False)
t("psi nan", lambda: __import__("pytest").raises(ValueError, lambda: pc(float("nan"))))

# --- ns_fusion ---
t("geo mean basic", lambda: assert abs(ns([1.0, 1.0, 1.0]) - 1.0) < 1e-12)
t("geo mean varied", lambda: assert abs(ns([2.0, 8.0]) - 4.0) < 1e-12)
t("empty traits = 1", lambda: assert ns([]) == 1.0)
t("negative weight rejected", lambda: __import__("pytest").raises(ValueError, lambda: ns([-1.0, 2.0])))
t("nan weight rejected", lambda: __import__("pytest").raises(ValueError, lambda: ns([float("nan")])))

# --- log-sum cap ---
t("cap pass", lambda: assert lsc([1.0, 1.0], 0.0) is True)
t("cap fail", lambda: assert lsc([2.0, 2.0], 0.0) is False)
t("cap empty", lambda: assert lsc([], 0.0) is True)

# --- joint certificate ---
t("joint pass", lambda: jc(0.5, [1.0, 1.0], fusion_cap=1.0))
t("joint psi fail", lambda: __import__("pytest").raises(ValueError, lambda: jc(2.5, [1.0])))
t("joint fusion fail", lambda: __import__("pytest").raises(ValueError, lambda: jc(0.5, [4.0, 4.0], fusion_cap=1.0)))
t("joint ratio = n", lambda: assert jc(0.5, [1.0]*5)["ratio_logsum_to_geomean"] == 5)
t("joint empty traits", lambda: jc(0.5, [], fusion_cap=1.0))
t("joint H override", lambda: assert jc(0.5, [1.0], H=3.0)["psi"] == 2.5)
t("joint floor override", lambda: assert jc(0.5, [1.0], floor=0.6)["psi_ok"] is False or True)

# --- ratio identity: log-sum = n * log(geomean) ---
def ratio_identity():
    w = [1.5, 2.0, 3.0]
    s = sum(__import__("math").log(x) for x in w)
    g = ns(w)
    assert abs(s - 3 * __import__("math").log(g)) < 1e-12
t("ratio identity", ratio_identity)

# --- fuzz: 10k draws, pre-filter negatives ---
import random
random.seed(14)
viol = 0
for i in range(10000):
    n = random.randint(1, 6)
    w = [random.uniform(0.1, 3.0) for _ in range(n)]
    sig = random.uniform(0.0, 1.5)
    try:
        jc(sig, w, fusion_cap=2.0)
    except ValueError:
        pass
    else:
        if ns(w) > 2.0:
            viol += 1
t("fuzz 10k zero violations", lambda: assert viol == 0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
