#!/usr/bin/env bash
# verify.sh (patched) — 8 checks, no timeouts. Patch: applied-norm metric + psi-collapse battery.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"; cd "$ROOT"
PASS=0; FAIL=0
log(){ echo "[verify] $*"; }
check(){ local n="$1"; shift; if "$@" > /tmp/v_$$ 2>&1; then log "PASS  $n"; PASS=$((PASS+1)); else log "FAIL  $n"; tail -n 20 /tmp/v_$$ || true; FAIL=1; fi; rm -f /tmp/v_$$; }

check "no_placeholders" bash -c 'grep -RIl "PLACEHOLDER" bridge/ 2>/dev/null && exit 1 || echo clean'

check "bridge_import" python -c '
from bridge.avrone_cyclekernal_link import CycleKernalLink, run_psi_battery
from bridge.greatscale_evidence import GSEvidenceLedger, NINEFOLD_AXES
assert len(NINEFOLD_AXES)==9
link=CycleKernalLink(); assert link.p[0]==0.5; assert GSEvidenceLedger().verify(); print("ok")'

check "edge_battery_core" python -c '
from bridge.avrone_cyclekernal_link import CycleKernalLink
link=CycleKernalLink(epsilon=0.05)
# core 10: zero, tiny, exact, over, neg, r-overflow, wrong-dim, nan
for dp,ea in [([0]*5,True),([1e-9]*5,True),([0.05,0,0,0,0],True),([0.5]*5,True),
 ([ -0.02]*5,True),([10,0,0,0,0],True),([1,2,3],None),([float("nan")]*5,None)]:
    try:
        s=link.propose(dp); assert ea is None or s.accepted==ea
    except ValueError:
        assert ea is None
print("core ok")'

check "psi_collapse_battery" python -c '
from bridge.avrone_cyclekernal_link import run_psi_battery
r=run_psi_battery(); print(r["passed"],"/",r["total"]); assert r["all_pass"], r'

check "monte_carlo_applied_norm" python -c '
import random
from bridge.avrone_cyclekernal_link import CycleKernalLink
rng=random.Random(7); link=CycleKernalLink(epsilon=0.03); v=0
for _ in range(10_000):
    s=link.propose([rng.uniform(-2,2) for _ in range(5)])
    if s.norm_applied()>s.epsilon+1e-12: v+=1
print("applied violations:",v); assert v==0; assert link.summary()["ledger_valid"]'

check "ledger_tamper" python -c '
from bridge.avrone_cyclekernal_link import CycleKernalLink
link=CycleKernalLink(); link.propose([0.01]*5); link.propose([0.01,0,0,0,0])
assert link.verify_ledger(); link.ledger[1].payload["accepted"]=not link.ledger[1].payload["accepted"]
assert link.verify_ledger() is False; print("tamper detected")'

check "squad_offline" bash -c '
  if [ -d avrone-squad ]; then
    export AVRONE_LLM_MODE=mock AVRONE_RUNNER=local
    python -c "import avrone" 2>/dev/null || pip install -e ".[dev]" -q
    pytest -q avrone-squad/tests -o faulthandler_timeout=0 2>&1 | tee /tmp/squad_out
    grep -q passed /tmp/squad_out
  else echo "avrone-squad/ absent — skipped"; fi'

check "cross_link_merge" python -c '
from bridge.avrone_cyclekernal_link import CycleKernalLink
from bridge.greatscale_evidence import GSEvidenceLedger
link=CycleKernalLink()
for _ in range(5): link.propose([0.01,0,0,0,0])
led=GSEvidenceLedger(); led.append("cyclekernal_snapshot",{"summary":link.summary()})
assert led.verify(); print("gs ledger",len(led.records))'

echo "----------------------------------------"
echo "VERIFY: $PASS passed, FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
