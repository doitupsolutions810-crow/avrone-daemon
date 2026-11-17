# Trial receiver mainline integration port

## Repository boundary

- Repository: `doitupsolutions810-crow/avrone-daemon`
- Fresh base: `3a1fbc19c867449a3b781f4dc9557b694cd7c974`
- Branch: `integration/trial-receiver-mainline-2026-11-16`
- The original v1 draft remains PR #7 and is not modified.
- PR #68 remains an audit/reference branch; this port does not merge or rewrite it.

## Current-main API review

The port uses the current `FleetManager.dispatch_gateway`, `check_policy`, `SYNC_CAPABILITIES`, `sign`, and `_post` contracts. Trial ingestion is a distinct gateway kind named `trials`, with capability `evidence:trial:ingest`. It requires a non-empty explicit `AVRONE_FLEET_ALLOW` even if the general fleet policy permits an empty allowlist.

The current ASGI body limiter remains authoritative. Trial endpoints receive a 256 KiB cap, including reverse-proxy mount prefixes. Duplicate raw authentication headers are rejected before gateway dispatch and nonce consumption. The exact signed body is then parsed again with strict duplicate-key, finite-number, UTF-8, depth, and node-count checks before storage.

## Contract and storage

- Envelope: `avrone.trial/v2`
- Fleet wrapper: `avrone.trial-ingest/v1`
- Bridge wrapper: `avrone.bridge-trial-ingest/v1`
- Storage: SQLite WAL, transactional per-envelope receipts, idempotent duplicates, conflicting-envelope quarantine, and sender quarantine.
- Provenance: source system/node, source string, run ID, check ID, check version, configuration digest, attempt ID, first sender/transport, bridge run ID, and per-delivery receipt.
- Outcomes: pass/fail are projected into the evidence pool; skipped/cancelled/timed_out/inconclusive remain separate non-pass records.
- Statistical independence: `not_assessed`.

## Prior exact-head failure analysis

PR #68 workflow 1448, commit `77fa7ba669aacd1677df055dcbedb36c42165686`, produced 221 focused receiver tests passed on Python 3.11 and 3.12. The Python 3.12 full suite reported 9,294 passed, 2 failed, 318 skipped. Python 3.11 full-suite execution was cancelled before completion.

The two observed failures were:

1. `tests/test_cov11_a.py::test_route_of_bypass_error`: the test expected the urllib fallback to report `env-proxy`, but the preferred `_httpx_route` probe returned `direct` first. The test now explicitly stubs `_httpx_route` to `None`, so it tests the fallback branch it names.
2. `tests/test_master_prompt.py::test_committed_prompt_is_current`: generated inventory omitted the three newly added trial modules and test count. The committed generated tree now includes those modules and the updated count.

These are targeted corrections based on the actual workflow log. A new exact-head CI run is still required; no pass is inferred from the prior run.

## Limits

The fleet sender identity is a signed `from` assertion under the existing shared HMAC secret and allowlist, not an independent public-key identity. Local SQLite checks do not prevent a privileged actor from coherently rewriting the database and its local evidence. Off-host signed anchoring remains separate work. No external Grok Bot, CycleKernel, Great Scale, Docker Compose, or deployment result is claimed.
