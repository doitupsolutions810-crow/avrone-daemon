# AVRONE trial receiver integration handoff — 2026-11-07

**Disposition: HOLD / reviewable handoff only.** This is a documentation-only commit on a new branch from the current main head. No runtime code, production data, secrets, permissions, or deployment state changed.

## 1. Repository and PR state observed

- Current `main` head at inspection: `5d08835329656ab944c1bd4a954c27b90430f2ab` (2026-11-07 15:00:26 UTC; v0.93.0).
- PR [#7 — Add deterministic cross-system evidence trial identity](https://github.com/doitupsolutions810-crow/avrone-daemon/pull/7):
  - State at final refresh: open, draft, not merged; GitHub currently reports mergeable=true. The branch is still 16 commits behind current main, so this is not a substitute for a current-main rebase and CI run.
  - Base at PR metadata read: `8a4d4c...`; head: `5ad87b6f37c62960feb69a2cf7c1b4c6768c482b`.
  - Compared with current main: diverged, 11 commits ahead / 16 behind; merge base is older than current main.
  - Four additive files: `avrone/trial_contract.py`, `schemas/avrone.trial.v1.schema.json`, `tests/test_trial_contract.py`, `docs/TRIAL_ENVELOPE_V1.md`.
  - No review submissions or review threads were returned.
- PR #7 CI run `42317875925`: Python 3.12 job failed; Python 3.11 job cancelled. Both jobs currently expose `steps: null`. Fetching the Python 3.12 job log returned GitHub's 404 `BlobNotFound`. **The failure cause for PR #7 is not established.**
- Current main commit `5d088353...` returned no associated workflow runs and no combined-status entries in this inspection. Empty status data is not a pass.
- The PR #7 branch's `.github/workflows/ci.yml` and current main's workflow both contain `set -o pipefail` followed by `grep -rlI ... | while ...` in the install and test steps. An empty grep result can fail that pipeline. Open draft PR #10 contains the no-match fix plus parser/capgate changes. Its current hosted run (42323869626) has both Python 3.11 and 3.12 jobs green, but the PR is unmerged, mergeable=false, and its head is 6 commits ahead / 11 behind current main. Current main still contains the old pipeline. Treat PR #10 as a candidate patch set requiring current-main integration and a fresh run, not as current-main verification. This is a known workflow risk, **not a confirmed explanation for PR #7's steps-null run**.
- Separate actual-log evidence: PR #5 run `42319131257` failed both matrix jobs in `Install avrone (dev)`; its logs stop at the first checkout-hygiene grep pipeline before pip installation and pytest, and the test step was skipped. That supports the no-match/pipefail failure mode for that PR #5 run only. Do not transfer that diagnosis to PR #7 without its logs.

## 2. Runtime and verification boundary

The available container has Python 3.13.5 and pytest 9.0.2, but no repository checkout was found under `/workspace`, `/workspaces`, `/repo`, `/mnt/data`, or `/root`. Docker and `docker compose` are not installed.

Consequently, this run did **not** execute:
- focused pytest;
- full pytest;
- compileall or git diff checks against a checkout;
- `scripts/verify-grok-bridge.sh`;
- Docker Compose verification;
- a Grok Bot, CycleKernel, or live multi-agent run.

The verifier was not run because Docker, the daemon checkout, and both live services were not demonstrably available. No test counts or service-health results are claimed.

## 3. Trial contract review

Inspected the exact PR #7 files at head `5ad87b6f...`:

- `avrone/trial_contract.py` blob `8f51f131177464a57eb9bd5321b7069958b45dd5`
- `schemas/avrone.trial.v1.schema.json` blob `608a9c18bb5ef30a6aa43e16ec24ac9c4989b51c`
- `tests/test_trial_contract.py` blob `882da2dab81bcd9c07056dee2a61f9ed46d1dd3c`
- `docs/TRIAL_ENVELOPE_V1.md` blob `3580d673fafcb2582ffcc17a3c3d9bc51bb4c28e`

The identity rule is directionally sound: `trial_id` binds schema, system, node, run, check ID/version, configuration digest, and attempt ID while excluding the outcome. A changed outcome for the same attempt therefore collides instead of creating a new sample. The documentation correctly says that distinct IDs do not establish statistical independence and that skipped/cancelled/timed-out/inconclusive outcomes must not be converted into passes.

Issues to resolve before using this envelope as a receiver contract:

1. **Pool source-length incompatibility.** The PR permits `system` and `node` up to 256 characters each and constructs `source = system + ":" + node`, up to 513 characters. Current `avrone/evidence_pool.py` sets `MAX_SOURCE_CHARS = 128`. The PR test `test_composite_source_accepts_maximum_component_lengths` explicitly creates a 513-character source, so that valid envelope cannot be projected into the current pool. Enforce a bounded composite source or define a separate stable source key; do not silently truncate.
2. **Validator canonical-form gap.** `validate_trial_envelope` calls `_identity(...)` but discards the normalized return value, then hashes the original record strings. The builder NFC-normalizes and trims; the validator should require the received identity fields to already equal their canonical form.
3. **Unicode edge coverage.** Current tests cover NFC/NFD equivalence for one identity field, but not C1 controls, bidi/format characters, zero-width characters, lone surrogates, or Unicode normalization inside configuration keys/values. Configuration JSON is sorted and compact, but recursive NFC normalization and a cross-language canonicalization rule are not defined. Python's `json.dumps` behavior should not be treated as a universal canonical-JSON standard without an explicit specification.
4. **Outcome-state model.** The envelope has only boolean `failed`. It cannot represent `skipped`, `cancelled`, `timed_out`, `inconclusive`, or `blocked` as first-class outcomes. Keep those outside the pass/fail pool, but define an explicit versioned operational outcome contract rather than encoding them as `failed=false`.
5. **Receiver semantics are absent from PR #7.** The contract validates identity; it does not persist accepted envelopes, deduplicate retries, quarantine conflicting outcomes, authenticate a sender, or issue receipts. Self-asserted `system` and `node` are not transport identity.
6. **Keep full provenance.** The complete envelope, especially `check_version`, `attempt_id`, configuration digest, and authenticated transport provenance, must remain in durable storage. Do not reduce it to only `source/trial_id/failed` before the receipt/audit record is written.

## 4. Existing AVRONE APIs inspected

Current main blobs inspected:

- `avrone/evidence_pool.py`: `249d0f8a78e0d7bccbb822fbab5f6f3636ec4522`
- `avrone/bridge.py`: `cf2298243c16bb10c4930efb31b70a26e8d70576`
- `avrone/fleet.py`: `47cce9b2a3542609e4346ed542b79c8dc93ff214`
- `avrone/server.py`: `3b27971ba102e5f3e211efb73ad10330419b2f98`
- `avrone/safeio.py`: `f7e02aac8a5797f0ebdec0be4c56450588e02be1`

### Evidence pool

`pool_trials` accepts a source (max 128 chars), trial ID, and boolean `failed`; duplicate IDs raise rather than being silently ignored. The result explicitly reports `independence: "not_assessed"`. Therefore retry idempotency belongs in a durable receiver before pool projection. Do not change the pool to count duplicates as new trials.

### Existing Bridge

`Bridge.export(since)` and `Bridge.merge_remote(posts, origin_node)` are run-scoped blackboard APIs. `merge_remote` deduplicates by post ID and silently skips IDs already present; it does not compare payload digests or quarantine conflicting content. The bridge journal is not a structured trial store and has no dedicated trial HTTP endpoint. Do not place authoritative trial records into generic bridge posts and treat a skipped merge as an accepted duplicate.

### Existing Fleet gateway

The current fleet path already provides HMAC authentication over method, canonical path, timestamp, nonce, and body hash; it checks a timestamp window, nonce replay, sender policy, hops, and per-kind capability. Outbound `FleetManager._post` uses the fleet token. Current `SYNC_CAPABILITIES` has `memory:sync` and `agent:migrate`, not a trial-ingest capability. The server has `/api/fleet/memory`, `/api/fleet/agentpack`, `/api/fleet/run`, and `/api/fleet/message`; there is no trial route on main.

Important trust limit: fleet nodes share `AVRONE_FLEET_TOKEN`. A signed `from` field plus allowlist proves possession of the shared fleet secret and membership in the configured trust group; it is not a unique per-node cryptographic identity. The global allowlist is optional by default. A trial endpoint must require explicit sender allowlisting for this operation and record the authentication method honestly. Strong per-node identity requires per-peer credentials or mTLS; do not claim it from the shared key.

### Existing evidence receiver work in other draft PRs

PR #5 and PR #6 both contain an earlier `avrone.greatscale.evidence/v1` receiver implementation, but neither is on main:

- PR #5 head `eee63f90edf159044fd9032262c85cca2a8e090c`, base branch `fix/fleet-test-process-cleanup`; compared with current main it is 58 ahead / 64 behind.
- PR #6 head `4d3c811435ac32b7f99e3202500902c28f0508a8`, base main SHA `c9f5dc348a65cda4f4ce8e509bfcd59c803a3905`; compared with current main it is 13 ahead / 22 behind.

Their receiver uses SQLite WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`, a nonce table, strict JSON parsing, a 256 KiB request cap, separate HMAC/receipt secrets, and an absolute persistent DB path. It is a useful storage/authentication pattern, not a drop-in trial receiver:
- it accepts `avrone.greatscale.evidence/v1`, not `avrone.trial/v1`;
- its uniqueness key is `(source, run_id, check_id)`, which omits check version and attempt ID;
- a changed payload for an existing identity raises a conflict but rolls back; it does not persist a conflict-quarantine record;
- the Great Scale projection helper accepts only `runs == 1` and projects to a reduced pool record, losing the trial contract's version/attempt provenance.

Do not merge either stale branch wholesale or copy its implementation. Reuse the reviewed transaction and receipt principles only after a current-main integration design is agreed.

## 5. External contract survey

Read-only inspection only; no external repo or service was modified.

- **Grok Bot** — `doitupsolutions810-crow/grok-bot-`, main latest commit `c4d5e95b7a23639c103552ea1cc0470e86c98a21`. Its README documents REST routes for chat, runs, approvals, ledger, tools, and RAG. The existing `avroneTool` calls `POST /api/chat`; no versioned trial-evidence endpoint was found in the inspected server/router. `src/router.js` imports `authRequired`, but the global `r.use(authRequired)` is commented out. Do not send evidence to the current chat API or treat it as an authenticated evidence receiver.
- **CycleKernel showcase** — private `cyclekernel-showcase`. Its README lists legacy simulation and monitoring routes such as `/health`, `/metrics`, `/state`, `/simulate/<cycles>`, `/api/chat/save`, and `/api/simulation/snapshot`. The inspected Flask server enables wildcard CORS; the monitoring server exposes REST routes without a versioned trial schema/auth contract in the inspected code. This is not an acceptable direct evidence-ingest target. The separate public `cyclekernel-attestpipe_q6-platform-upgrade-2` repository reports size 0 and only an initial commit.
- **Great Scale** — `great-scale` documents `GET /api/v1/scale/state` and `POST /api/v1/scale/step` in `LATTICE_BRIDGE.md`; those are scale/control endpoints, not a trial-event receiver. The separate `GreatScale-Avrone-Grok-Fusion-Bridge` repository's README is currently `PLACEHOLDER`; no reviewable OpenAPI/event contract was found there.
- **External dashboards** — no dashboard-specific, versioned trial-event contract was verified in this run.

Until each peer publishes a versioned HTTP/MCP/event contract and authentication policy, AVRONE remains the canonical orchestration/state layer and sends no trial writes to those systems.

## 6. Receiver design gate for the next implementation

Use one canonical durable trial store and thin adapters; do not copy storage logic into Grok Bot, CycleKernel, Great Scale, or dashboards.

1. Validate the complete `avrone.trial/v1` envelope, canonical identity, source bounds, and strict JSON before storage. Resolve the source-length mismatch and define Unicode canonicalization before freezing cross-language vectors.
2. Store the full envelope and transport provenance. Key accepted records by `trial_id` (or the exact full identity tuple including check version and attempt ID), not merely `(source, run_id, check_id)`.
3. In one SQLite transaction, claim the request nonce, insert the first accepted trial, and persist its signed receipt. An identical envelope with a fresh nonce returns the same receipt as `duplicate`. The same trial ID with a different canonical payload/outcome writes a durable quarantine record, retains the first accepted record, and returns an explicit `conflict_quarantined` outcome; it never overwrites or enters the pool twice.
4. Preserve `source`, `system`, `node`, `run_id`, `check_id`, `check_version`, `attempt_id`, configuration digest, envelope digest, authenticated sender, auth method, and receipt. Do not infer identity from self-asserted envelope fields.
5. Keep non-pass operational outcomes explicit in a separate versioned outcome contract. Only completed pass/fail trials enter `pool_trials`; non-pass outcomes return explicit statuses and are never coerced to a pass.
6. For fleet ingress, add a narrow `trials:ingest` capability and a `/api/fleet/trials` route through the existing `dispatch_gateway`. Require a non-empty explicit sender allowlist for this route even when the global fleet policy is in compatibility allow-all mode. Apply a route-specific body cap and batch/item caps.
7. For Grok Bot, CycleKernel, Great Scale, and dashboards, use thin authenticated HTTP/MCP adapters against the versioned receiver contract. Do not reuse admin credentials or silently treat the shared fleet token as unique node identity. Keep Bridge's generic journal separate from authoritative trial storage.
8. Keep `independence = "not_assessed"` unless a separately reviewed sampling design establishes the relevant independence assumptions. Distinct IDs alone are insufficient.

### Required edge-case suite before acceptance

- identical retry with a fresh nonce; exact nonce replay; duplicate and reordered delivery;
- same trial ID with conflicting outcome or provenance; durable quarantine and stable first receipt;
- malformed JSON, duplicate keys, NaN/Infinity, wrong field types, unknown fields, and invalid digests;
- oversize body, oversize batch, deeply nested JSON, and long identifiers/source values;
- NFC/NFD identity and config strings, C1 controls, bidi/zero-width characters, lone surrogates, and canonicalization golden vectors across languages;
- concurrent threads and independent processes racing identical and conflicting writes;
- restart/reopen, WAL recovery, transaction interruption between writes, torn/corrupt storage, and fail-closed storage errors;
- missing/duplicate auth headers, wrong method/path/body signature, stale timestamp, reused nonce, unlisted sender, self-sender, and absent fleet token;
- explicit `skipped`, `cancelled`, `timed_out`, `inconclusive`, and `blocked` outcomes remain outside the pass/fail pool.

## 7. Bounded next step

First obtain the missing exact PR #7 workflow log or rerun that exact head in a runner that records step logs. Confirm whether the failure occurred before steps or inside the checkout-hygiene command. Then create a fresh integration branch from the then-current main, resolve the contract issues above, and implement the SQLite trial store plus the narrow fleet adapter. Run focused and full pytest on that exact commit. Run `scripts/verify-grok-bridge.sh` only after Docker, the required Compose services, and both service repositories are demonstrably available.

No merge, deployment, production secret change, network/recon widening, or privileged action is authorized by this handoff.

## 8. Machine-readable decision summary

```jsonl
{"decision":"preserve_main","status":"preserved","main_sha":"5d08835329656ab944c1bd4a954c27b90430f2ab"}
{"decision":"pr_7","status":"open_draft_hold","head_sha":"5ad87b6f37c62960feb69a2cf7c1b4c6768c482b","ahead_by":11,"behind_by":16,"ci_run":"42317875925","root_cause":"unavailable_logs"}
{"decision":"trial_receiver","status":"not_implemented","reason":"exact_pr_7_failure_logs_unavailable_and_runtime_checkout_absent"}
{"decision":"pytest","status":"not_run","reason":"no_repository_checkout"}
{"decision":"docker_compose","status":"not_run","reason":"docker_cli_and_compose_unavailable"}
{"decision":"independence","value":"not_assessed"}
{"decision":"production_actions","status":"denied","merge":false,"deploy":false,"secret_changes":false,"privilege_changes":false}
```
