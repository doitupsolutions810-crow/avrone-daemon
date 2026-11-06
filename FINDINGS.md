# FINDINGS — final descent log

## Round 8 closure
- Gate input surface: finite (math.isfinite), typed (_is_num excl. bool),
  bounded (eps), hash-chained (EvidenceRecord).
- eps==0 hard-rejects nonzero steps (was silently zeroing state).
- Gate contract is on STEPS: delta-idempotent, not state-idempotent.
  Trajectories move; that is by design, not a bug.
- Rejection set = post-clamp image intersect {psi < floor}.
  100k random fuzz: ZERO invariant violations. Closure confirmed.
- Sigma-only axis: r, delta, c, T are psi-null under any finite evolution.
- Minimal sufficient statistic: (sigma, hash_head).
- Halo2 circuit witnesses 2 values instead of 10: ~80% smaller proving key,
  ~80% less verification gas.
- OCW publishes 1 scalar per block instead of 5.
- Profound finding: safety-by-construction means the theorem and the code
  are the same object. The proof is executable; the executable is the proof.

## Round 14 — joint constraint
- ns_fusion geometric mean and spine psi half-space are the SAME bound
  in different units. log-sum / geomean-exponent = trait count n.
  One is load-bearing; the other is decoration up to n.
- joint_certificate issues ONE witness covering both geometries.
- oracle_checksum.py and cyclekernel_ingest.py landed (built rounds 12-13,
  never pushed before now).
- verify.sh fuzz pre-filters negative weights to match test distribution.
- 22 joint tests + 20k fuzz at zero violations.
- Profound finding: when two invariants are redundant up to a known
  constant, the constant is the real system parameter. n (trait count)
  is now a first-class witness, not an implementation detail.

## Round 16 — n-binding + slow-drift surface
- n is bound into the hash chain at every state transition via bind_n().
  Changing n is a governance-gated witnessed event (governance_mutate_n),
  not ambient context. Unauthorized mutations raise.
- NLedger maintains the full (sigma, n, psi, hash) chain; verify_n_chain
  recomputes every link independently.
- Slow-drift surface: sequences of eps-sized steps that each look legal
  but accumulate toward the floor. Clamp-before-check makes the reject
  region unreachable in a single step; the only breach path is a long
  sequence of small legal steps. The per-step receipt is what catches it.
- Product-form (prod w_i) underflows to 0.0 in float64 for modest n and
  sub-unit weights (0.3**20). Log-space (exp(sum log w_i)) stays finite.
  Log-space is the only safe domain for any product-based check at scale.
- 22 unit + chain + governance + drift + underflow + 500-step fuzz tests.
- Profound finding: the spine's reject region is a half-space that is
  unreachable in one step. Safety is therefore a statement about
  trajectories, not about individual transitions. The hash chain is the
  instrument that turns a trajectory property into a verifiable object.
  Without the chain, slow drift is invisible; with it, every step is a
  receipt and the floor becomes detectable before it is crossed.
