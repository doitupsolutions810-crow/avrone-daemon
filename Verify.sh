#!/usr/bin/env bash
# Verify.sh — AVRONE full verification harness (Descent XCV)
# Runs trust-gate separation, Great Scale invariants, and extended edge cases.
# Exit 0 only when every check passes. Safe to run inside the AVRONE image.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

FAIL=0

log()  { printf '[verify] %s\n' "$*"; }

# --- 1. Environment sanity ---
log "AVRONE root: $ROOT"
if [[ ! -f pyproject.toml || ! -d avrone || ! -d avrone-a12lr/tests ]]; then
  log "FAIL: not an AVRONE checkout (missing pyproject.toml, avrone/, or avrone-a12lr/tests/)"
  exit 2
fi

# --- 2. Install (editable, dev extras) ---
log "Installing avrone[dev]..."
python -m pip install -e '.[dev]' -q

# --- 3. Trust gate separation (16 cases, canonical module) ---
log "Running trust gate separation suite..."
if python -m pytest -q avrone-a12lr/tests/test_xcv_trust_gates.py 2>&1 | tee /tmp/verify_trust.log; then
  log "PASS: trust gate separation"
else
  log "FAIL: trust gate separation — see /tmp/verify_trust.log"
  FAIL=1
fi

# --- 4. Great Scale invariants ---
# evidence_snapshot(failures, runs, alpha=0.05) -> keys: p, S, psi, coneOk, onConeFloor,
# orientation, n, lambda, runsFor95. Residual count n = 8 - 4S; partition 4+3+1.
log "Checking Great Scale invariants (cone floor, orientation, residual count)..."
if python - <<'PY'
from avrone.greatscale import evidence_snapshot, CONE, RESIDUAL_PARTITION
import math

phi = (1 + math.sqrt(5)) / 2
phi_inv2 = 1 / phi**2
assert abs(CONE - phi_inv2) < 1e-12

# Deterministic pass: 0 failures / 200 runs -> cone floor, orientation pass
snap = evidence_snapshot(0, 200)
assert snap["onConeFloor"] is True, "deterministic pass must sit on cone floor"
assert snap["orientation"] == "pass"
assert snap["psi"] >= CONE - 1e-12
assert snap["n"] == 8.0
assert RESIDUAL_PARTITION == (4, 3, 1) and sum(RESIDUAL_PARTITION) == 8

# Deterministic fail: 200/200 -> also cone floor but orientation fail (heat is orientation-blind)
snap_f = evidence_snapshot(200, 200)
assert snap_f["onConeFloor"] is True
assert snap_f["orientation"] == "fail"
assert snap_f["S"] == 0.0

# Flaky mid-range: 100/200 -> peak heat S = 0.25
snap_m = evidence_snapshot(100, 200)
assert abs(snap_m["S"] - 0.25) < 1e-12
assert snap_m["psi"] == CONE + 0.25
assert snap_m["orientation"] == "mixed"
print("invariants ok")
PY
  log "PASS: Great Scale invariants"
else
  log "FAIL: Great Scale invariants"
  FAIL=1
fi

# --- 5. Extended edge cases: string-booleans must fail semantic authorization ---
# is_authorized lives in the canonical test module; trust_context string values
# must be rejected because gate semantics require booleans, not shapes.
log "Running extended edge-case probes (string-boolean semantic rejection)..."
if python - <<'PY'
import importlib.util, sys
sys.path.insert(0, "avrone-a12lr/tests")
spec = importlib.util.spec_from_file_location(
    "test_xcv_trust_gates", "avrone-a12lr/tests/test_xcv_trust_gates.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

snap = mod.make_snapshot()
# trust_context with string values — shape-valid, semantically invalid
snap["trust_context"] = {
    "numerical_gates": "true",
    "provenance_bound": "true",
    "capability_scoped": "true",
    "receipt_chained": "true",
}
cap = {"expired": False, "revoked": False, "audience": "avrone-daemon",
       "operation": "accept", "signature_valid": True}
receipt = {"envelope_hash": mod.envelope_hash(snap), "verifier": "trusted-verifier"}
ok, reasons = mod.is_authorized(snap, cap, receipt)
assert not ok, "string-boolean trust_context must deny"
assert any("trust_context_" in r for r in reasons)
# and the pristine-math snapshot with no trust_context and no cap still denies
snap2 = mod.make_snapshot()
ok2, reasons2 = mod.is_authorized(snap2, None, None)
assert not ok2 and "capability_missing" in reasons2
print("string-boolean rejection ok")
PY
  log "PASS: extended edge cases"
else
  log "FAIL: extended edge cases"
  FAIL=1
fi

# --- 6. Summary ---
log "================================"
log "VERIFY COMPLETE: fail=$FAIL"
if [[ "$FAIL" -ne 0 ]]; then
  log "RESULT: FAIL"
  exit 1
fi
log "RESULT: PASS"
exit 0
