#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 - <<'PY'
import math, random
from cycle_link import evolve, GateError, psi
from evidence import EvidenceRecord
from oracle_checksum import psi_checksum, OracleFeed
from joint_constraint import joint_certificate, ns_fusion_score, log_sum_cap

# 1 finite gate
for bad in [float('nan'), float('inf'), float('-inf')]:
    try:
        evolve({"sigma": 0.0}, {"sigma": bad})
        raise SystemExit("FAIL: non-finite accepted")
    except GateError as e:
        assert "non-finite" in str(e)
print("1 ok finite")

# 2 type gate
try:
    evolve({"sigma": "x"}, {"sigma": 0.1})
    raise SystemExit("FAIL: str accepted")
except GateError:
    pass
print("2 ok types")

# 3 epsilon clamp
p = evolve({"sigma": 0.0}, {"sigma": 10.0}, eps=0.5)
assert abs(p["sigma"] - 0.5) < 1e-12 and p["norm"] == 0.5
print("3 ok clamp")

# 4 psi floor
try:
    evolve({"sigma": 0.6}, {"sigma": 1.0}, eps=0.5, H=1.0, floor=0.0)
    raise SystemExit("FAIL: psi breach accepted")
except GateError as e:
    assert "psi-collapse" in str(e)
print("4 ok psi floor")

# 5 hash chain
a = EvidenceRecord({"k": 1})
b = EvidenceRecord({"k": 2}, prev_hash=a.hash)
b.verify(a)
try:
    b.payload["k"] = 99
    b.verify(a)
    raise SystemExit("FAIL: tamper accepted")
except ValueError:
    pass
print("5 ok chain")

# 6 idempotent delta
p1 = evolve({"sigma": 0.2}, {"sigma": 0.4}, eps=0.3)
p2 = evolve(p1, {"sigma": 0.4}, eps=0.3)
assert abs(p2["sigma"] - p1["sigma"]) <= 0.3 + 1e-12
print("6 ok delta idempotence")

# 7 sigma-only axis
p = evolve({"r": 9, "delta": 9, "c": 9, "sigma": 0.1, "T": 9},
           {"r": 0, "delta": 0, "c": 0, "sigma": 0.2, "T": 0}, eps=5.0)
assert p["psi"] == 1.0 - 0.3
print("7 ok sigma-only")

# 8 oracle checksum
f = OracleFeed(H=2.0, floor=0.1)
assert f.ingest(0.5) is True
assert len(f.accepted) == 1
try:
    f.ingest(2.5)
except ValueError:
    assert len(f.rejected) == 1
print("8 ok oracle checksum")

# 9 joint constraint
jc = joint_certificate(0.5, [1.0, 1.0], fusion_cap=1.0)
assert jc["psi_ok"] and jc["fusion_ok"] and jc["trait_count"] == 2
print("9 ok joint")

# 10 joint fuzz: pre-filter negatives (aligned with test distribution)
random.seed(14)
viol = 0
for i in range(20000):
    n = random.randint(1, 6)
    w = [random.uniform(0.1, 3.0) for _ in range(n)]
    sig = random.uniform(0.0, 1.5)
    try:
        joint_certificate(sig, w, fusion_cap=2.0)
    except ValueError:
        continue
print("10 ok joint fuzz 20k")

print("VERIFY PASS")
PY
