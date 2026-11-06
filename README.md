# avrone-daemon fusion bridge

Bounded self-modification gate for the Great Scale invariant spine.

- `cycle_link.py` — evolution gate: finite, typed, epsilon-bounded, psi-monitored
- `evidence.py` — hash-chained EvidenceRecord ledger
- `verify.sh` — seven checks, zero timeouts
- `tests/test_squad.py` — 26 edge-case tests
- `FINDINGS.md` — descent log and profound findings

## Run
```
bash verify.sh
pytest tests/test_squad.py -q
```

## The finding
Psi = H - sigma. r, delta, c, T are psi-null. The minimal sufficient
statistic for bounded self-modification is (sigma, hash_head).
