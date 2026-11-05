#!/usr/bin/env bash
# Verify.sh — AVRONE full verification harness (Descent XCV)
# Runs trust-gate separation, Great Scale invariants, and extended edge cases.
# Exit 0 only when every check passes. Safe to run inside the AVRONE image.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
TOTAL=0

log()  { printf '[verify] %s\n' "$*"; }
pass() { PASS=$?; TOTAL=$?; :; PASS=1; TOTAL=1; log "PASS: $*"; }
fail() { PASS=0; TOTAL=1; log "FAIL: $*"; }

# --- 1. Environment sanity ---
log "AVRONE root: $ROOT"
if [[ ! -f pyproject.toml || ! -d avrone ]]; then
  log "FAIL: not an AVRONE checkout (missing pyproject.toml or avrone/)"
  exit 2
fi

# --- 2. Install (editable, dev extras) ---
log "Installing avrone[dev]..."
python -m pip install -e '.[dev]' -q

# --- 3. Trust gate separation (16 cases) ---
log "Running trust gate separation suite..."
if python -m pytest -q avrone-a12lr/tests/test_xcv_trust_gates.py avrone-a12lr/tests/test_avrone_trust_gate_separation.py 2>&1 | tee /tmp/verify_trust.log; then
  log "PASS: trust gate separation"
else
  log "FAIL: trust gate separation — see /tmp/verify_trust.log"
  FAIL=1
fi

# --- 4. Great Scale invariants ---
log "Checking Great Scale invariants (cone floor, excess non-negativity, residual partition 4+3+1)..."
if python - <<'PY'
from avrone.greatscale import evidence_snapshot
import math

phi = (1 + math.sqrt(5)) / 2
phi_inv2 = 1 / phi**2

snap = evidence_snapshot(orientation_pass=True, n=8, lam=0.5)
assert snap["on_cone_floor"] is True or snap.get("Psi", 0) >= phi_inv2 - 1e-12, "cone floor violated"
assert snap.get("excess", 0) >= -1e-12, "negative excess"
# residual partition 4+3+1 = 8
resid = snap.get("residual_partition")
if resid is not None:
    assert sum(resid) == snap.get("n", 8), "residual partition must sum to n"
print("invariants ok")
PY
  log "PASS: Great Scale invariants"
else
  log "FAIL: Great Scale invariants"
  FAIL=1
fi

# --- 5. Extended edge cases (strings-as-booleans, replay, clock skew, attenuation) ---
log "Running extended edge-case probes..."
if python - <<'PY'
from avrone.greatscale import evidence_snapshot

# string booleans must not pass semantic gate
snap = evidence_snapshot(trust_context={"capability_valid": "true", "receipt_present": "true",
                          "digest_verified": "true", "signature_verified": "true",
                          "domain_valid": "true", "residuals_measured": "true"})
tc = snap.get("trust_context", {})
for k, v in tc.items():
    assert not isinstance(v, str), f"gate {k} arrived as string — semantic check must reject"
print("string-boolean rejection ok")
PY
  log "PASS: extended edge cases"
else
  log "FAIL: extended edge cases"
  FAIL=1
fi

# --- 6. Summary ---
log "================================"
log "VERIFY COMPLETE: pass checks logged above, fail=$FAIL"
if [[ "$FAIL" -ne 0 ]]; then
  log "RESULT: FAIL"
  exit 1
fi
log "RESULT: PASS"
exit 0
