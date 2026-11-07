# AVRONE / Grok Bot squad handoff — Descent XIV

Status: prepared for review; not sent to a Grok Bot conversation and not executed by a remote squad in this session.

## Objective

Independently review the dormant AVRONE-to-attested-history edge and design a non-destructive, fail-closed integration. Do not enable production attestation or rewrite the existing ledger.

## Squad assignments

### Auditor — repository and trust-boundary review
- Confirm every call site of `capgate.record()` and whether `attest_if_enabled()` has any runtime caller.
- Confirm environment variable propagation, Compose mounts, UID/GID write permissions, and whether the external repository is actually mounted.
- Distinguish local JSONL append, Git commit, push, and independent verification as separate events.

### Adversarial tester — ledger and race cases
- Missing, empty, malformed, duplicate-key, NaN/Infinity, oversized, symlink, FIFO, non-regular, truncated, and permission-denied history.
- Digest mismatch, broken previous hash, sequence gap, invalid provenance, duplicate receipt, duplicate callback, and replayed trial ID.
- Crash after local append but before Git commit; crash after commit but before acknowledgement; concurrent writers; restart with stale mount.
- Verify that tool execution is never retried solely because the later attestation step failed.

### Integration engineer — proposed implementation only
- Propose an opt-in hook with explicit result reporting and no default behavior change.
- Define a stable event/trial envelope: source, trial_id, check_id, check_version, workflow_sha, runner_image_digest, run_id, job_id, outcome, timestamp, provenance refs.
- Keep agent append capability separate from Git commit/push authority.
- Do not wire evidence pooling into lessons until idempotency and check comparability are enforced.

### Independent verifier — evidence and release gate
- Recompute the existing receipt digest from the exact checked-in bytes.
- Review attested-history PR #3 and confirm missing/empty ledgers cannot return VERIFIED.
- Require a non-empty valid chain and a read-back of the exact Git blob/commit.
- Record unmeasured states explicitly; do not turn unavailable Docker/CI evidence into PASS.

## Required output

1. Per-role findings with exact file paths and line ranges.
2. Reproduction commands and observed exit codes.
3. Proposed patch split into test-only, runtime hook, mount/configuration, and privileged anchoring changes.
4. A threat model for the append/commit boundary.
5. A go/no-go decision against every success criterion in `docs/DESCENT_XIV.md`.

## Stop conditions

- Do not modify `main`.
- Do not overwrite or delete `history.jsonl`.
- Do not enable `AVRONE_CAPGATE_ATTEST` against the current external ledger.
- Do not run a live evolution loop or deployment until Docker host, target repositories, and the side effects are explicitly reviewed.
- Do not claim squad execution without fresh conversation readback and artifact verification.
## Current execution constraints

- Grok Bot main `live-tests` run 40801560492 and scheduled `model-bench` run 41439417592 ended with `runner_id=0` and empty step lists; treat them as runner/startup failures, not as executed test suites.
- The local execution environment has no Docker executable, so do not claim `verify.sh`, Compose builds, live inference, browser tests, or the squad round ran.
- `verify.sh` copies `.env.full` to `.env` only when `.env` is absent, starts the full Docker profile, writes `data/VERIFY.md` and `data/EVOLVE.md`, and runs the adjacent AVRONE suite only if `AVRONE_DIR` resolves. Its default `AVRONE_DIR=../Avrone` may not match the `avrone-daemon` checkout name.
- On the intended Docker host, after reviewing `.env` and confirming the checkout paths, use `AVRONE_DIR=../avrone-daemon VERIFY_REQUIRE_AVRONE=true VERIFY_BOOT_TIMEOUT_SECONDS=7200 ./verify.sh`. This is a bounded 2-hour boot wait, not an infinite no-timeout mode.
- Preserve `data/VERIFY.md`, `data/EVOLVE.md`, container logs, exact image digests, and the final Git status as evidence. Do not auto-merge or enable attestation based only on a successful boot.