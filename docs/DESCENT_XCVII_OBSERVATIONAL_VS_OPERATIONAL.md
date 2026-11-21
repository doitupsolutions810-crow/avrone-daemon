# Descent XCVII — Observational vs Operational Trust

**Date:** 2026-11-20
**Branch:** `descent/xcv-trust-gates`
**Builds on:** Descent XCV, XCVI, XCIV, CIV, `avrone/greatscale.py`, `Verify.sh`

## Profound finding

Green tests and green CI do not equal operational refusal.

`avrone/greatscale.py` defines `is_authorized`. The separation suite defines a parallel stand-in. `Verify.sh` exercises the stand-in. Docker-verify and the full pytest matrix are green on real runners.

Yet:

1. No production accept path (fleet ingest, CodeX emit, bridge merge, trial receiver) imports or calls `greatscale.is_authorized`.
2. `evidence_snapshot` documents trust_context as *purely observational — never used in any acceptance decision*.
3. Two key schemas coexist:
   - `REQUIRED_TRUST_GATES` = numerical_gates, provenance_bound, capability_scoped, receipt_chained
   - `TRUST_CONTEXT_KEYS` = source_commit, artifact_digest, signature_verified, capability_valid, execution_receipt, numerical_gates_passed, domain_assumptions_valid

A payload can satisfy one schema and fail the other. Detection without refusal is residual heat: the system can *see* a broken gate and still *proceed*.

## Great Scale mapping (same form)

| Channel | Meaning under XCVII | Required invariant |
|---|---|---|
| $S = p(1-p)$ | Variance of gate outcomes over repeated accept attempts | Peak heat at $p=1/2$ (half the accepts are false) |
| $\Psi = \phi^{-2} + S$ | Cone floor only when every operational gate is deterministic | $\Psi \ge \phi^{-2}$; observational-only checks sit on the floor while production still accepts |
| Orientation | Signed authorize / deny, not recoverable from $S$ alone | Deny must be recorded as a separate signed channel |
| Residual partition $4+3+1$ | Dual atlas: Watch (observe) / Observer (record) / Forward (refuse) | Forward role must be able to stop the accept path |
| Evidence chain | Additive, never mutates geometry | Calling `is_authorized` must not alter $S$, $\Psi$, or orientation of the scalar |

The system-limiting technicality is the gap between **observational trust** (we measured it) and **operational trust** (we refused to proceed).

## Success criteria (mitigation)

An envelope is accepted only when:

1. `greatscale.is_authorized(snap, cap, receipt)` is invoked on the production accept path (not only in tests).
2. A non-empty `reasons` list from that call causes fail-closed refusal before CodeX receipt emit / fleet ack.
3. The two key schemas are unified or explicitly dual-mapped with a single source of truth.
4. Failure injection: missing capability, string-boolean trust_context, and dual-schema mismatch each produce a recorded deny and zero accept side effects.

Until those hold, integration remains a research contract — green CI is necessary, not sufficient.

## What this descent adds

- This document.
- Edge cases 23–26: production-module import of `is_authorized`, dual-schema mismatch, observational-only snapshot cannot authorize, and empty-reasons control through the production function.
- No overwrite of existing geometry, capgate, or fleet code. Wiring into accept paths is the ranked next merge (post-XCVII).

## What this does not do

- Does not treat residual heat as an authorization decision by itself.
- Does not widen recon or ambient power.
- Does not replace the stand-in test module; it adds a production-import path so both are exercised.
