# AVRONE trial receiver review — 2026-11-08

## Executive result

This review is based on live GitHub repository reads performed on 2026-11-08. No runtime checkout was available in the review environment. The receiver implementation already exists on PR #12, with an opt-in PostgreSQL extension on PR #13. Do not duplicate the receiver or merge either PR on the basis of this handoff.

## Repository and branch state

- Current main snapshot observed by GitHub compare: `0646c06f047b230df62c0ba1d9399dd0dd3cb287`.
- PR #7 branch `integration/trial-envelope-v1-2026-11-06`: 11 commits ahead and 22 behind main; four additive files. It is materially superseded by the richer receiver branch. The connector did not expose the branch's current tip SHA or draft flag.
- PR #12 branch `integration/trial-envelope-receiver-v1-2026-11-07`: 8 commits ahead and 6 behind main; 15 changed files, including the receiver, adapters, contracts, route, tests and CI.
- Exact PR #12 commit with verified CI: `6574c10f9778af3c3d9f5a0b37a4729ff3e2b407`.
- PR #13 branch `integration/postgres-trial-store-2026-11-08`: 14 commits ahead and 6 behind main; adds the PostgreSQL store and service-backed tests on top of the receiver work. Its exact current tip SHA was not exposed by the available branch/compare response.
- Main was not changed by this review.

## Review-branch CI hygiene patch

A fresh Actions run for this review PR (run `42504592638`) reproduced the same no-match pipeline failure on both Python versions. The actual job logs show the two checkout-hygiene `grep | while` pipelines running under `set -o pipefail` without a no-match guard; both jobs stopped in the install step before pip or pytest.

The review branch now adds `|| true` to those two hygiene pipelines only. The dependency-install and pytest pipelines retain their failure behavior. This is a narrow CI reliability fix, not a change to runtime behavior or security policy. The fix is not considered validated until a new Actions run on the post-fix commit completes.

## PR #7 CI failure diagnosis

- Workflow run `42317688198`: Python 3.12 failed and Python 3.11 was cancelled before steps; job step lists were null.
- Retry run `42317875925`: Python 3.12 reached the install step and failed before dependency installation. The actual log shows `set -o pipefail` followed by a `grep -rlI` hygiene scan with no match; the pipeline returned 1 and stopped the step.
- PR #12's CI workflow makes this no-match scan non-fatal with `|| true`. Its later exact-head workflow completed successfully.

## PR #12 receiver and adapter review

### Identity contract

- `avrone.trial/v1` identity includes schema, system, node, run, check ID/version, canonical configuration digest and attempt ID.
- Outcome is excluded from the identity, so contradictory replay payloads collide instead of manufacturing a second observation.
- System/node components are bounded; identifiers and configuration are NFC-normalized; control/format/surrogate characters, unsafe numeric values, key collisions, cycles, excessive depth/node count and oversized configuration are rejected.
- `avrone.trial-outcome/v1` represents skipped, cancelled, timed-out, inconclusive and blocked outcomes without coercing them into pass/fail.
- Source length is constrained to fit the existing evidence pool without truncation.

### SQLite receiver

- `POST /api/fleet/trials` uses the existing Fleet HMAC gateway and dedicated `trials:ingest` capability.
- The route enforces UTF-8 strict JSON, duplicate-key rejection, bounded nesting, body and batch limits, header cardinality/content-length checks, timestamp/nonce/signature checks, and an explicit sender allowlist.
- SQLite uses WAL, `synchronous=FULL`, `busy_timeout` and `BEGIN IMMEDIATE`.
- Accepted identity is immutable. Exact replay returns the original signed receipt; a different payload for the same trial ID is quarantined; every delivery retains transport provenance.
- Non-pass outcomes are stored operationally and excluded from the pooled pass/fail snapshot.
- Batch members commit independently, allowing a partially completed batch to be retried with a fresh Fleet nonce.

### Bridge and Fleet adapters

- Bridge adapter adds read-only correlation metadata and verifies run/node and locally originated post references. It does not turn the Bridge journal into the authoritative evidence store.
- Fleet adapter requires an explicit target allowlist, uses the existing signed Fleet post path, and bounds the batch.
- Receiver provenance labels the authentication assurance as `shared_secret_group_member`.

### Statistical and trust boundary

- The pool keeps `independence: not_assessed`. Distinct trial IDs establish identity uniqueness, not independent sampling, causal comparability, equivalent environments or representativeness.
- Fleet HMAC plus allowlisting proves shared-secret group membership, not unique cryptographic node identity. Any holder of the shared token can assert a sender name inside a signed payload. The code and documentation disclose this limit; do not describe it as per-node authentication or mTLS.
- Evidence ingestion remains separate from execution authorization. Privileged ingress is refused.

## CI evidence

For exact commit `6574c10f9778af3c3d9f5a0b37a4729ff3e2b407`, Actions run `42435527395` completed successfully:

| Python | Full suite |
|---|---|
| 3.11 | 5,787 passed, 318 skipped |
| 3.12 | 5,787 passed, 318 skipped |

The focused trial contract/receiver/adapter step completed in both jobs. Warnings were reported (1 on Python 3.11 and 64 on Python 3.12), including Starlette/httpx deprecation and multiprocessing fork warnings.

These results validate the PR #12 tested commit only. They do not validate the later PR #13 PostgreSQL code.

## PR #13 PostgreSQL extension review

- Optional `avrone[postgres]` dependency; PostgreSQL is selected explicitly with `AVRONE_TRIAL_STORE=postgres` and `AVRONE_TRIAL_DATABASE_URL`.
- One PostgreSQL primary uses a primary-key uniqueness constraint, `INSERT ... ON CONFLICT DO NOTHING`, a subsequent READ COMMITTED winner read, transaction-scoped advisory schema lock, signed receipts and transactional delivery/quarantine writes.
- The implementation does not silently fall back to independent SQLite files.
- Added tests cover restart receipt stability, 16-process duplicate races, 16-process conflicting-outcome races, crash before commit, committed-but-lost-response retry, batch retry, non-pass exclusion and unavailable-database fail-closed behavior.
- PR #13's own description says its PostgreSQL tests have not yet completed hosted CI. The green run above is for the parent receiver commit, not this extension.
- A single PostgreSQL primary is a shared transactional uniqueness boundary, not multi-primary consensus, exactly-once external side effects or statistical independence.

## Validation not performed in this environment

- No local repository checkout was available (`git rev-parse --show-toplevel` returned unavailable).
- Docker CLI and Compose were unavailable; `docker ps` could not run.
- `scripts/verify-grok-bridge.sh` exists on main, but was not executed. Its preflight requires reachable AVRONE `/api/status` and Grok Bot `/api/status` endpoints; no live service availability was established.
- No local focused pytest or full pytest suite was run.
- No live Grok Bot squad dispatch, CycleKernel call, external dashboard write, or deployment was performed.

## Required next bounded steps

1. Obtain the exact current PR #13 head SHA and its Actions runs; do not use parent PR #12 CI as evidence for the PostgreSQL extension.
2. Run the PR #13 PostgreSQL-focused lane and full Python 3.11/3.12 suite on that exact head; inspect logs for any failure before editing.
3. Add/confirm SQLite abrupt-process-death rollback coverage in addition to the existing trigger-injected rollback and corrupt-database tests.
4. Add a test that makes the shared-secret identity assurance explicit: a signed sender claim is not unique node identity. Do not pretend a test can establish per-node authentication without per-peer credentials or mTLS.
5. Reconcile the six-commit mainline drift on a dedicated branch and rerun CI after any rebase/merge-base update.
6. Run Docker Compose bridge verification only on a host where Docker and both services/repos are actually present.
7. Keep Grok Bot, CycleKernel, Great Scale and dashboards behind separately versioned, authenticated contracts. No direct writes or execution authority are implied by evidence ingestion.

## Safety disposition

Review-only. No merge, deployment, production secret changes, capability expansion, network/recon permission expansion or mainline mutation. Keep PR #12 and PR #13 in draft/HOLD until exact-head PostgreSQL CI, branch synchronization and runtime integration gates are independently verified.
