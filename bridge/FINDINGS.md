# FINDINGS.md — extended descent, 2026-11-06

Three bugs surfaced running the 20-case battery, 100k-step Monte Carlo, and a
formal claim check against the bridge pushed in commit f2077b6.

## Finding 1 — The metric was lying (profound)

`EvolutionStep.norm()` reported `||dP||` of the **proposal**, not the step that
was actually applied. Consequence: the Monte Carlo reported **100,000 bound
violations at every epsilon** — while the gate had clamped every single one.

The gate worked. The number didn't. This is the exact failure class the
EvidenceRecord ledger exists to catch: a scalar that looks like a fault but
measures the wrong object. **A system that cannot distinguish proposal from
application cannot audit itself.**

Fix: split into `norm_dp()` (proposal, may exceed epsilon) and `norm_applied()`
(always ≤ epsilon by construction). MC now reports applied violations = 0.

Mathematics: for any dP with ||dP|| > ε, the applied step is
`dP · (ε / ||dP||)`, whose norm is exactly ε. The sphere is the boundary, never
crossed. ∀dP: ||applied(dP,ε)||₂ ≤ ε. Proven by scaling.

## Finding 2 — Psi-collapse is the second line, not the first

The sigma_spike case (d=(0,0,0,3,0), ε=0.05) was expected to hard-reject.
It didn't — because epsilon clamping reduced the sigma delta to 0.05 *before*
psi was evaluated, leaving psi = 0.50 − 0.05 = 0.45, above the floor.

The bound ate the spike. Rejection only fires when clamping is insufficient:
ε=2.0, sigma delta 3.0 → clamped to 2.0 → psi = 0.50 − 2.00 = −1.50 < −1.00
→ rejected. Verified with a dedicated psi-collapse battery (3 cases, all pass).

Ordering: **epsilon gate first, psi floor second.** Two defenses, one spine.

## Finding 3 — The formal claim was misstated

Claim as written: "accepted iff ||dP|| ≤ ε AND psi ≥ floor." False — over-bound
steps are clamped and *applied*, which is acceptance. Corrected claim:

**accepted(dP) ⇔ psi(p + applied(dP), H') ≥ floor**

where applied(dP) = dP if ||dP|| ≤ ε else dP·(ε/||dP||). Checked over 5,000
random steps: holds.

## What didn't break

- Hash-chain tamper detection: flipping one ledger payload byte → verify() False.
- Non-finite input (NaN, ±Inf): hard ValueError. Inf in state would poison every
  downstream psi and norm computation; rejecting at the boundary is correct.
- Ninefold axis mapping, GS ledger merge, cross-link snapshot: all green.
- Ledger validity across 10k+ steps: True throughout.

## Next descent seed

The two-line defense (epsilon, then psi) is itself a bounded operator on the
decision space. Ask: does the *composition* of clamp-then-reject preserve the
||ΔP|| ≤ ε invariant on the accepted set? Yes — rejected steps revert to p_old,
so ΔP = 0. The invariant holds on every terminal state. That is the spine's
closure property, and it is now machine-checked rather than assumed.
