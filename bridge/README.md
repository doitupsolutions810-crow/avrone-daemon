# bridge/ — the last unconnected connection, now connected

Two modules that were eleven-byte PLACEHOLDER stubs. They are not anymore.

## avrone_cyclekernal_link.py
Boundary between AVRONE's off-chain evolve loop and the CycleKernal invariant spine.

- State: **P = (r, δ, c, σ, T)**
- Gate: **||ΔP||₂ ≤ ε** — bounded self-modification, no drift past epsilon
- Score: **Ψ(P) = H − σ** — stability, where H is staking/health participation
- Every decision emits a hash-chained **EvidenceRecord** (GS-LXI schema)

Edge battery: 10 cases (zero step, exact bound, over-bound clamp, negative
components, r-overflow, σ-spike psi collapse, wrong dimension, NaN, Inf).
Monte Carlo: 10k random steps, zero bound violations by construction.

## greatscale_evidence.py
GS-LXI evidence runtime. Ninefold circle maps nine brand sectors to nine state
axes. Append-only hash-chained ledger, mergeable across CycleKernal and GS.

## verify.sh
Seven checks, no timeouts: placeholder scan, imports, edge battery, Monte Carlo,
ledger tamper detection, squad offline re-run, cross-link merge.

```bash
chmod +x verify.sh && ./verify.sh
```
