# Descent XCV — Trust Gates: The System-Limiting Technicality

**Date:** 2026-11-05
**Branch:** `descent/xcv-trust-gates`
**Builds on:** Descent XCIV, Descent CIV, `docs/EVOLUTION.md`, `avrone/greatscale.py`

## The finding

The scalar geometry of the Great Scale — heat law $S = h(1-h)$, $\Psi = \phi^{-2} + S$, topological excess $e$, orientation-blindness of the local scalar — is correctly embedded in both `great-scale` and `avrone-daemon` (`avrone/greatscale.py`). Descent XCIV and CIV correctly state what that scalar *cannot* certify:

- orientation / signed topology
- absolute scale / provenance of the numbers
- authorization (capability, audience, operation, expiry, revocation)
- execution outcome (immutable receipt)

**The system-limiting technicality is the incomplete independent-gate wiring.** Mathematical/numerical acceptance, cryptographic provenance, scoped capability, and execution receipt remain separate channels. A trusted consumer can still treat an envelope as "accepted" if any one gate is missing or only shape-checked. This is the residual that prevents the merged system from becoming greater than the sum of its parts.

## Mapping onto the Great Scale (same form)

| Channel | Meaning | Required invariant |
|---|---|---|
| $S_T$, $\Psi_T$ | Compact geometric/residual diagnostic from measured evidence | $\Psi_T \ge \phi^{-2}$; agrees with signed absolute tuple |
| $C$ (signed) | Topology / orientation | Must be preserved; never recovered from $\Psi_T$ alone |
| $\mathcal{A}_Q$, $E_Q$ | Absolute geometry + excess | $\mathcal{A}_Q \ge \|C\|$, $E_Q = \mathcal{A}_Q - \|C\|$ |
| Numerical residuals + gates | Evidence quality | All configured gates measured and passed |
| Source commit + artifact digest | Reproducibility / provenance | Bound to exact bytes |
| Scoped capability + expiry + audience | Authorization | Validated independently of scientific correctness |
| Exit code + hash-chained receipt | Execution outcome | Success is *not* implied by formula or capability |

Evidence chain is **additive**. It must never replace or silently mutate the canonical phase-driven Great Scale state (CONE floor, residual partition 4+3+1, dual-atlas roles).

## Mitigation / success criteria

A claim or action is accepted only when **all** independent gates pass:

1. domain/model assumptions valid
2. numerical residuals + configured gates measured
3. exact source + artifact digest verified
4. cryptographic signature verified by trusted verifier
5. capability (issuer, scope, audience, operation, expiry, revocation) verified
6. execution result reconciled + immutable receipt recorded

Until the full producer → validator → authorization → receipt path with failure injection is exercised, the integration stays a research/engineering contract.

## Implementation (this descent)

1. `docs/DESCENT_XCV_TRUST_GATES.md` — this document.
2. `avrone/greatscale.py` — optional `trust_context` field on `evidence_snapshot`, purely additive, default-off. Records gate presence; does not decide.
3. `docs/EVOLUTION.md` — appended Post-XCIV section.
4. `tests/test_xcv_trust_separation.py` — asserts a perfect geometric snapshot with a missing capability is *not* treated as authorized.

## What this does not do

- It does not let residual heat become an authorization decision.
- It does not widen recon. Recon stays default-deny.
- It does not copy any other repo's code over AVRONE's package.
