# Descent XIV — The Dormant Bridge and the Non-Reproducible Anchor

Date: 2026-11-06
Baseline: avrone-daemon main at 8eea02f38464d28a045c1d3d925f2587ad918e9d; attested-history main at 75f8188e6c11668ac9e71de6f8a5a1decfff08cf.

## Finding

The cross-repository attestation path is represented in source but is not yet demonstrated end to end.

1. `avrone/capgate_head_attest.py` defines `attest_if_enabled()`. Repository-wide code search returns the definition in that module only; there is no caller from `capgate.record()` or another runtime path.
2. The module's default history path is `<data_dir>/attested/history.jsonl`. `.env.example` has no attestation path entry, and `docker-compose.yml` mounts `./data` but does not mount the separate `attested-history` checkout.
3. The module appends an entry; it does not create a Git commit or independently invoke the external repository's verifier. A JSONL append is not yet a Git-anchored receipt.
4. The external `attested-history/main` has a first receipt, but its recorded digest does not reproduce under the checked-in verifier's canonicalization rule. The Git blob is `82951f020ec64e179af6bca5dbc8498bb220d647`; the recorded digest is `7a9e2b07d7e9cdd226a207c487e9caf34f30c86bac34ef9fd14688ce5bb30a17`; the recomputed digest is `70377ae5544bfbfc42bcf17c77857cb231db70ecd86b84accfec5e324adca483`.
5. The current external `verify.py` returns exit 0 when the history file is missing or empty. Thus the verifier has a false-green absence path in addition to the populated-ledger digest failure.
6. The `capgate_head_attest.py --selftest` corruption fixture searched for the spaced text `"seq": 0`, but the writer emits compact JSON `"seq":0`. The mutation was a no-op and the self-test failed to exercise the intended tamper path. The fix is proposed in AVRONE PR #8.
7. `avrone/evidence_pool.py` rejects duplicate trial IDs, but a trial record contains only `source`, `trial_id`, and `failed`. It does not enforce a common `check_id`, `check_version`, workflow revision, or runtime image. It correctly reports `independence: not_assessed`; callers must not pool unlike checks as though they were equivalent.

## Why this matters

The trust path has at least five distinct stages:

`capability event -> capgate receipt -> head attestation -> external append-only ledger -> independent verification + Git anchor`

An implementation at one stage does not prove the next. The current repository contains code for several stages, but the runtime invocation, external storage mount, and independent anchor are not verified. The external ledger currently fails its own digest check, so enabling writes against that file would be unsafe.

## Great Scale mapping

Use `phi = (1 + sqrt(5))/2` and `CONE = phi^-2 ~= 0.381966`. For an observed check rate `p`, `S = p(1-p)` and `Psi = CONE + S`.

| State | r | Observed failures | Jeffreys p_J | S | Psi |
|---|---:|---:|---:|---:|---:|
| No ledger evidence | 0 | unmeasured | 0.5 | 0.25 | 0.631966 |
| Existing CI census row | 7 | 6 | 0.1875 (pass-oriented) | 0.152344 | 0.534310 |
| One observed digest-verification failure | 1 | 1 | 0.75 | 0.1875 | 0.569466 |

The p/S/Psi values describe outcomes, not cryptographic integrity. Digest validity is a separate deterministic invariant. A valid-looking residual score cannot override a digest mismatch, missing file, or broken chain.

**The deeper result:** absence is not a zero-failure trial, authorship is not verification, and an append is not an anchor. The bridge must carry provenance through every boundary, and the last verifier must reject both missing evidence and non-reproducible evidence.

## Mitigation sequence

1. Keep `AVRONE_CAPGATE_ATTEST` off until the external ledger is repaired by an append-preserving migration.
2. Preserve the current `history.jsonl` bytes as a legacy artifact. Do not edit or silently re-hash its first entry.
3. Make the external verifier fail closed: missing/empty/unreadable history is HOLD; malformed or non-reproducible entries are HOLD; only a non-empty valid chain can be VERIFIED. Draft implementation and tests are in attested-history PR #3.
4. Agree on a migration genesis that references the legacy Git blob SHA, recorded digest, recomputed digest, and HOLD reason. The migration must not claim the legacy receipt verified.
5. Add a runtime hook only after specifying its failure semantics: an attestation-write failure must be visible, but must not cause an already-executed tool action to be retried as though it never happened.
6. Add an explicit host path and read/write mount for the external ledger. Keep append permission separate from Git commit/push permission.
7. Treat Git anchoring as a separate privileged step. Verify the committed blob and commit SHA independently after the append.
8. Extend trial records with `check_id`, `check_version`, `workflow_sha`, `runner_image_digest`, `run_id`, `job_id`, and an explicit outcome/censored state. Pool only compatible cohorts; retain source orientation and report independence as not assessed unless independently justified.
9. Wire the Great Scale evidence pool into Bridge/lesson retention only after the trial envelope is stable and idempotent. Repeated derivations should accumulate only when their trial IDs are disjoint and their check definitions match.

## Success criteria

- `AVRONE_CAPGATE_ATTEST=off` preserves current behavior.
- With attestation explicitly enabled, one capability event produces one receipt and one corresponding attestation attempt; retries and repeated callbacks are idempotent.
- The external ledger path resolves to the intended mounted checkout, not an accidental private data-directory copy.
- Symlink, FIFO, oversized, malformed, duplicate, digest-mismatched, and broken-chain ledgers fail closed.
- A missing or empty ledger returns HOLD, never exit 0.
- A valid non-empty chain passes an independent verifier and its exact blob is anchored by a Git commit.
- The original legacy ledger bytes remain recoverable and unchanged.
- Cross-node pooling rejects duplicate trial IDs and does not combine incompatible check versions.
- Docker-backed Grok Bot `verify.sh` is run on a host with Docker Compose and both repositories present; its report, exit code, container logs, and resulting Git state are read back separately.

## Verification status

- Uploaded AVRONE v0.82.0 snapshot: 691 focused regression tests passed across two targeted batches; deployment-hardening tests: 5 passed; Python compileall passed.
- `capgate_head_attest.py --selftest`: after correcting the fixture locally, 26 passed and 0 failed.
- Full 256-file test suite: not completed; the execution environment terminated a large batch at its 300-second tool limit.
- Docker: unavailable in this execution environment (`docker` executable absent). No Docker or Grok Bot `verify.sh` run is claimed.
- Live repository changes: documentation and review branches only; no production runtime hook or automatic ledger migration has been merged.

## Next falsifiable question

Can the runtime emit a single, uniquely identified capability receipt that is (a) attested once, (b) committed to the external history without granting the agent Git authority, and (c) independently verified after a restart? The next descent should test that complete path under duplicate callbacks, process crash between append and commit, stale mounts, partial writes, and concurrent workers.
## Additional execution-layer falsification

The live `.github/workflows/ci.yml` had a second false-negative path: both checkout-hygiene scans ran `grep | while` under `set -o pipefail`. On a clean checkout with no wrapper lines, `grep` returns 1 and the job exits before dependency installation. GitHub Actions run 42322491312 on main confirms that the install step failed before pytest. The fix is included in AVRONE PR #10; its new run has passed dependency installation and is executing the test matrix.

The same principle appears at two layers: an empty evidence search must not abort a healthy build, and an empty evidence ledger must not certify a healthy system. In both cases, absence must be represented explicitly rather than treated as success or an unhandled failure.
## Squad and Docker execution availability

The Grok Bot repository's `verify.sh` is a real local Docker verifier, not a harmless read-only probe. If `.env` is absent it copies `.env.full`; it then checks Docker/Compose, validates core and full Compose, builds and starts the full stack, runs live inference/browser/squad tests, writes `data/EVOLVE.md` and `data/VERIFY.md`, and optionally installs/runs the adjacent AVRONE suite in a virtual environment. Its default boot budget is 3600 seconds; 7200 can be selected for slower hosts. The script does not provide an unbounded wait.

The script defaults `AVRONE_DIR` to `../Avrone`; in a workspace where the repository is named `avrone-daemon`, the combined gate should explicitly set `AVRONE_DIR=../avrone-daemon VERIFY_REQUIRE_AVRONE=true` after confirming the actual checkout layout.

GitHub Actions metadata for Grok Bot main shows the latest `live-tests` run (40801560492) and scheduled `model-bench` run (41439417592) failed with `runner_id=0` and empty job steps. Those runs did not provide test execution evidence. In this session, the local environment has no Docker executable, and the connected browser cannot access the originating Codespaces tab. Therefore neither `verify.sh` nor a live Grok squad run was executed.