"""Round 16 squad: n-binding, governance mutation, slow-drift, log-space safety."""

import math
import random
import sys
sys.path.insert(0, ".")

from n_binding import (
    bind_n, verify_n_chain, governance_mutate_n, log_space_product,
    NLedger, record_hash,
)
from cycle_link import evolve, GateError

passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")

print("=== Round 16 n-binding suite ===")

# 1 basic bind
r = bind_n(0.5, 3)
check("bind basic", r["n"] == 3 and r["sigma"] == 0.5 and r["psi"] == 1.5)

# 2 hash deterministic
r2 = bind_n(0.5, 3)
check("hash deterministic", r["hash"] == r2["hash"])

# 3 prev changes hash
r3 = bind_n(0.5, 3, prev_hash="deadbeef")
check("prev binds", r3["hash"] != r["hash"])

# 4 negative n rejected
try:
    bind_n(0.5, -1)
    check("neg n reject", False)
except ValueError:
    check("neg n reject", True)

# 5 float n rejected
try:
    bind_n(0.5, 2.5)
    check("float n reject", False)
except ValueError:
    check("float n reject", True)

# 6 psi-collapse rejected
try:
    bind_n(2.5, 3, H=2.0, floor=0.1)
    check("psi collapse", False)
except ValueError as e:
    check("psi collapse", "psi-collapse" in str(e))

# 7 chain of 5
ledger = NLedger()
for i in range(5):
    ledger.append(0.3 + i * 0.05, n=2 + i)
check("chain length", len(ledger.receipts) == 5)
check("chain verify", ledger.verify() is True)
check("current n", ledger.current_n == 6)

# 8 tamper detection
ledger.receipts[2]["payload"] = "tampered"
try:
    ledger.verify()
    check("tamper detect", False)
except ValueError:
    check("tamper detect", True)
# restore for later tests
ledger = NLedger()
for i in range(5):
    ledger.append(0.3 + i * 0.05, n=2 + i)

# 9 unauthorized mutation
try:
    ledger.mutate_n(99, authorized=False, sigma=0.4)
    check("unauth mutate", False)
except ValueError as e:
    check("unauth mutate", "unauthorized" in str(e))

# 10 authorized mutation
r_mut = ledger.mutate_n(10, authorized=True, sigma=0.4)
check("auth mutate", r_mut["n"] == 10 and r_mut["old_n"] == 6)
check("post-mut verify", ledger.verify() is True)
check("post-mut n", ledger.current_n == 10)

# 11 log-space empty
check("log empty", log_space_product([]) == 1.0)

# 12 log-space simple
check("log simple", abs(log_space_product([2.0, 3.0]) - 6.0) < 1e-12)

# 13 log-space underflow safety: 0.1**100 underflows in product form
ws = [0.1] * 100
prod_naive = 1.0
for w in ws:
    prod_naive *= w
log_p = log_space_product(ws)
check("underflow naive", prod_naive == 0.0)
check("underflow log", log_p > 0.0 and math.isfinite(log_p))

# 14 negative weight
try:
    log_space_product([1.0, -0.5])
    check("neg weight", False)
except ValueError:
    check("neg weight", True)

# 15 zero weight
check("zero weight", log_space_product([1.0, 0.0, 2.0]) == 0.0)

# --- Slow-drift surface ---
print("--- slow-drift ---")

# 16 single eps step from near floor stays legal
p = evolve({"sigma": 0.85}, {"sigma": 0.5}, eps=0.5, H=2.0, floor=0.1)
check("near-floor clamp", p["psi"] >= 0.1 and p["sigma"] <= 1.35)

# 17 40-step slow drift; each step eps-legal, stays above floor
sigma = 0.2
H = 2.0
floor = 0.1
eps = 0.03
breached = False
for step in range(40):
    try:
        p = evolve({"sigma": sigma}, {"sigma": eps}, eps=eps, H=H, floor=floor)
        sigma = p["sigma"]
        if p["psi"] < floor:
            breached = True
            break
    except GateError:
        breached = True
        break
check("40-step drift no breach", not breached, f"sigma={sigma:.4f} psi={H-sigma:.4f}")
check("40-step sigma increased", sigma > 0.2)

# 18 drift eventually hits floor if continued far enough
sigma = 0.2
hit = False
for step in range(300):
    try:
        p = evolve({"sigma": sigma}, {"sigma": eps}, eps=eps, H=H, floor=floor)
        sigma = p["sigma"]
    except GateError as e:
        if "psi-collapse" in str(e):
            hit = True
            break
check("eventual floor hit", hit, f"final sigma={sigma:.4f}")

# 19 n-binding on every drift step
ledger2 = NLedger(H=2.0, floor=0.1)
sigma = 0.2
for step in range(20):
    p = evolve({"sigma": sigma}, {"sigma": 0.03}, eps=0.05, H=2.0, floor=0.1)
    sigma = p["sigma"]
    ledger2.append(sigma, n=4)
check("drift ledger verify", ledger2.verify() is True)
check("drift ledger length", len(ledger2.receipts) == 20)

# 20 n change mid-drift is witnessed
ledger2.mutate_n(7, authorized=True, sigma=sigma)
check("mid-drift n mut", ledger2.current_n == 7 and ledger2.verify())

# 21 ratio identity still holds for n
from joint_constraint import ns_fusion_score
w = [1.2, 0.8, 1.1, 0.9]
n = len(w)
geo = ns_fusion_score(w)
logsum = sum(math.log(x) for x in w)
check("ratio identity", abs(logsum - n * math.log(geo)) < 1e-10)

# 22 fuzz: random n + sigma, all receipts chain
random.seed(16)
ledger3 = NLedger()
for i in range(500):
    n = random.randint(0, 12)
    sig = random.uniform(0.0, 1.8)
    try:
        ledger3.append(sig, n)
    except ValueError:
        pass  # psi-collapse expected for high sigma
check("fuzz chain verify", ledger3.verify() is True)
check("fuzz non-empty", len(ledger3.receipts) > 100)

print(f"\n=== {passed} passed, {failed} failed ===")
sys.exit(0 if failed == 0 else 1)
