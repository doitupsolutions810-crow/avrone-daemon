# Descent VII — The Duplicate-Trial Trap

## Finding

The existing Great Scale evidence snapshot correctly summarizes failures and runs, and the bridge/fleet paths relay artifacts across processes and nodes. But an additive counter is safe only when the observations being added are actually disjoint. A retry, replayed event, or the same run relayed through two peers can otherwise make one trial look like two confirmations. Counts alone cannot detect that overlap.

The missing invariant is **idempotent trial identity before pooling**:

```text
unique trial IDs  -> pool observed failures/runs -> calculate p, S, Psi
repeated trial ID -> reject the merge (do not silently add or discard)
```

This is a narrower and more actionable result than assuming all evidence can be summed: namespace isolation remains intact, but a future cross-node evidence edge must carry stable trial identity along with source and outcome. `avrone.evidence_pool.pool_trials` introduces this contract additively, without changing Bridge, fleet memory, lesson persistence, or existing APIs.

## Great Scale mapping

For the pooled records, `p = failures / runs`, `S = p(1-p)`, and `Psi = phi^-2 + S`. Source-level orientations are retained separately. If every source is deterministic but one reports `pass` and another `fail`, the pooled report marks `orientation_conflict=true`; it does not average away the disagreement. Empty input is reported as unmeasured (`evidence: null`), not as a zero-failure cone-floor result.

For example, two failed trials from one source and two passing trials from another produce `p = 2/4 = 0.5`, `S = 0.25`, and `Psi = phi^-2 + 0.25 ~= 0.631966`, while preserving the node-level `fail` versus `pass` conflict. These are descriptive pooled outcomes; the helper explicitly reports independence as `not_assessed`. Distinct IDs prevent duplicate counting but do not prove independent sampling, identical environments, or comparable test definitions.

## Mitigation and success criteria

1. Carry a stable trial ID, source, check identity/version, and boolean outcome across every relay.
2. Reject duplicate IDs before aggregation; make retry behavior explicit rather than incrementing counters again.
3. Preserve per-source orientation and raise a visible conflict when deterministic silos disagree.
4. Label empty input unmeasured and never infer a pass from absence.
5. Do not claim statistical independence, causal equivalence, or a verified system-wide result from IDs alone.
6. Keep the helper bounded (100,000 records, 10,000 sources) and test malformed types, duplicate relays, conflicting floors, and cap behavior.

## Scope and verification status

This change adds a pure, offline pooling helper and regression tests only. It is not yet wired to `Bridge`, fleet synchronization, lessons, Grok Bot, or the API; those integrations require agreeing on a stable cross-repository trial-ID and check-version contract first. The current tool environment supports repository reads and GitHub commits, but not local shell/Docker execution, so the new pytest suite and Grok Bot `verify.sh` have **not** been run here. In particular, do not interpret this commit or the test definitions as a green runtime verification.

Run the focused tests and full offline suite from the repository root with:

```bash
python -m pytest tests/test_evidence_pool.py -q
python -m pytest -q
```

Run Grok Bot's Docker-backed verifier only on a host with Docker Compose, its required model storage, and both repositories checked out; see the integration contract in `integrations/grok-bot/README.md`.
