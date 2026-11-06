# Descent XCV — Projection–Involution Commutation and Orientation Witness

**Status:** proved mathematical identities and deterministic numerical tests; no empirical physics claim or production-execution proof.

For a nine-component real vector x, define reflection (Rx)_j=x_{-j mod 9}, rotation (Tx)_j=x_{j-1 mod 9}, mean projection P=11ᵀ/9, and residual Q=I-P. Since R and T permute components, P commutes with each. Thus P²=P=Pᵀ, PR=RP, PT=TP, R²=I, and RTR=T⁻¹. Orthogonality implies:

    ||x||² = ||Px||² + ||Qx||²
    P(Rx)=Px, ||Q(Rx)||²=||Qx||².

**New identifiability finding:** the mean and unsigned residual energy cannot distinguish x from its reflection. The loss is exact, not numerical noise.

Add a *coordinate-frame-bound* signed witness W(x)=Σ[j=0..8] x_j sin(2πj/9), for which W(Rx)=-W(x) but W(Px)=0. For W(x)≠0 this witness separates the reflected pair, though W can vanish on asymmetric states; it is not a complete classifier and is not invariant under arbitrary rotations. Great Scale's heat h(u)=u(1-u) has an analogous 2:1 ambiguity, h(u)=h(1-u).

**AVRONE bridge:** attach the immutable nine-vector fingerprint, transform version, mean, residual energy, and signed witness to a research claim. The SHA-256 here is a data fingerprint, **not** a signed execution receipt. Production completion must separately verify an authorized, signed, run-specific AVRONE receipt and downstream evidence. Never equate a queued Swarm receipt with successful specialist execution.

**Acceptance:** pinned source revision; finite nine-vector; reflection/projection commutator and energy error within tolerance; sign flip when |W| exceeds tolerance; signed operational completion proof separately required. This addition changes no existing runtime modules, runner privileges, network configuration, or merge state.

Run from this directory:

```bash
python -m unittest -v test_great_scale_projection_witness.py
python great_scale_projection_witness.py 0 1 2 3 4 5 6 7 8
```

Test program uses 2,500 seeded random vectors and malformed/non-finite edge cases. It requires only the Python standard library. It is not a replacement for the full AVRONE suite.
