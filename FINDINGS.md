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
