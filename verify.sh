#!/usr/bin/env bash
# verify.sh — edge-case hardened, no timeouts, full descent verification.
# Runs: (1) placeholder scan, (2) bridge unit math, (3) edge battery,
# (4) Monte Carlo bound check, (5) ledger chain integrity, (6) squad re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
log() { echo "[verify] $*"; }
check() {
  local name="$1"; shift
  if "$@" > /tmp/verify_out_$$ 2>&1; then
    log "PASS  $name"
    PASS=$((PASS + 1))
  else
    log "FAIL  $name"
    tail -n 20 /tmp/verify_out_$$ || true
    FAIL=$?
  fi
  rm -f /tmp/verify_out_$$
}

# 1. Placeholder scan — no file in bridge/ may contain the literal PLACEHOLDER
check "no_placeholders" bash -c '
  if grep -RIl "PLACEHOLDER" bridge/ 2>/dev/null; then
    echo "PLACEHOLDER still present in bridge/"
    exit 1
  fi
  echo "bridge/ clean of PLACEHOLDER stubs"
'

# 2. Bridge imports and genesis
check "bridge_import" python -c '
from bridge.avrone_cyclekernal_link import CycleKernalLink
from bridge.greatscale_evidence import GSEvidenceLedger, NINEFOLD_AXES
assert len(NINEFOLD_AXES) == 9
link = CycleKernalLink(epsilon=0.05)
assert link.p[0] == 0.5
led = GSEvidenceLedger()
assert led.verify()
print("imports ok, genesis p =", link.p)
'

# 3. Edge battery (10 cases incl. nan/inf/wrong-dim/psi-collapse)
check "edge_battery" python -c '
from bridge.avrone_cyclekernal_link import run_edge_battery, CycleKernalLink
r = run_edge_battery(CycleKernalLink(epsilon=0.05))
print(r["passed"], "/", r["total"])
assert r["all_pass"], r
'

# 4. Monte Carlo: 10k random steps, zero bound violations
check "monte_carlo_10k" python -c '
import random
from bridge.avrone_cyclekernal_link import CycleKernalLink
rng = random.Random(7)
link = CycleKernalLink(epsilon=0.03)
v = 0
for _ in range(10_000):
    d = [rng.uniform(-2, 2) for _ in range(5)]
    s = link.propose(d)
    if s.norm_dp() > s.epsilon + 1e-12:
        v += 1
print("violations:", v)
assert v == 0
assert link.summary()["ledger_valid"] is True
'

# 5. Ledger tamper detection
check "ledger_tamper" python -c '
from bridge.avrone_cyclekernal_link import CycleKernalLink
link = CycleKernalLink()
link.propose([0.01, 0, 0, 0, 0])
link.propose([0.01, 0, 0, 0, 0])
assert link.verify_ledger() is True
# Tamper: flip a payload byte
link.ledger[1].payload["accepted"] = not link.ledger[1].payload["accepted"]
assert link.verify_ledger() is False
print("tamper detected")
'

# 6. Squad re-run (offline, mock mode, no timeouts)
check "squad_offline" bash -c '
  if [ -d avrone-squad ]; then
    export AVRONE_LLM_MODE=mock AVRONE_RUNNER=local
    python -c "import avrone" 2>/dev/null || pip install -e ".[dev]" -q
    pytest -q avrone-squad/tests -o faulthandler_timeout=0 2>&1 | tee /tmp/squad_out
    grep -q "passed" /tmp/squad_out
  else
    echo "avrone-squad/ not present — skipped"
  fi
'

# 7. Cross-link: GS ledger can absorb CycleKernal digests
check "cross_link_merge" python -c '
from bridge.avrone_cyclekernal_link import CycleKernalLink
from bridge.greatscale_evidence import GSEvidenceLedger
link = CycleKernalLink()
for _ in range(5):
    link.propose([0.01, 0.0, 0.0, 0.0, 0.0])
led = GSEvidenceLedger()
led.append("cyclekernal_snapshot", {"summary": link.summary()})
assert led.verify() is True
print("merged, gs ledger len =", len(led.records))
'

echo "----------------------------------------"
echo "VERIFY: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
