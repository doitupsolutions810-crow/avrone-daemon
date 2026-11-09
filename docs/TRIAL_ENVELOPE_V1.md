# AVRONE trial envelope and receiver v1

## Identity and provenance

The contract is avrone.trial/v1. trial_id is SHA-256 over canonical JSON identity fields: schema, system, node, run ID, check ID, check version, canonical check-configuration digest, and attempt ID. Outcome is excluded deliberately. Replaying the same attempt with a different outcome therefore collides and is quarantined; a genuine retry must use a new attempt ID.

Identity strings are NFC-normalized by the producer. The receiver rejects non-canonical strings, control/format/surrogate code points, unknown fields, invalid digests, and inconsistent outcome flags. system is limited to 64 characters and node to 63 so the composite source fits the existing evidence pool's 128-character source limit. The full envelope is retained, including source, check version, configuration digest, run, and attempt provenance.

The configuration is hashed, not transmitted. Canonical configuration is bounded to 16 KiB, depth 32, and 4,096 JSON nodes. The complete trial envelope is bounded to 4 KiB. Only JSON primitives, arrays, and string-keyed objects are accepted.

## Outcomes and pooling

outcome is one of pass, fail, skipped, cancelled, timed_out, or inconclusive. failed is false for pass, true for fail, and null for every non-pass/fail operational outcome. Non-pass outcomes are durably stored but never projected into evidence_pool.pool_trials. They are not converted to passes or failures.

The pool snapshot delegates to the existing AVRONE evidence pool. Its statistical independence field remains not_assessed; distinct trial IDs do not prove independent sampling, causal comparability, or representative coverage.

## Fleet HTTP adapter

The receiver is POST /api/fleet/evidence. The body follows avrone.trial-delivery/v1 and carries the envelope in trial. It is authenticated by the existing fleet HMAC headers (x-avrone-ts, x-avrone-nonce, x-avrone-sig) over method, path, timestamp, nonce, and body hash. The capability is data-only: evidence:ingest.

The receiver additionally requires the signed from node to be in a non-empty AVRONE_FLEET_ALLOW list. This is intentionally stricter than the fleet's optional allow-all mode. The body cap is 64 KiB, duplicate JSON keys and non-finite numbers are rejected, and deeply nested JSON fails closed. FleetManager.push_trial(node_id, envelope) sends only to a peer already present in the fleet registry and allowed by the existing outbound policy.

The response includes a receipt HMAC signed with the existing fleet token and binds both the authenticated sender claim and receiver node. FleetManager.push_trial verifies the receipt before returning it. Statuses are accepted (201), duplicate (200), stored_non_pass (202), and quarantined (409). A conflicting replay is appended to the quarantine table; repeated identical conflicts reuse the same quarantine row, and the first accepted record is never overwritten.

## Bridge adapter

post_trial_to_bridge(bridge, envelope) writes a typed evidence post containing the canonical envelope. Existing netsquad transport relays bridge posts inside the already signed /api/fleet/run request. The receiving netsquad path accepts evidence posts only when the signed sender is explicitly allowlisted, records the authenticated fleet sender separately from the post's self-asserted origin_node, then merges the bridge post into the ordinary bridge journal.

The bridge post itself is not an authentication mechanism. Do not call the bridge ingestion helper with an unverified sender. The helper enforces an explicit allowlist as a second check.

## Persistence and safety

Records, delivery provenance, and conflict quarantine are stored in trial_evidence.sqlite3 under the configured AVRONE data directory using SQLite WAL and synchronous=FULL, with BEGIN IMMEDIATE transactions and bounded row quotas. A duplicate with identical canonical content is idempotent. A duplicate trial ID with changed content is quarantined. The transport sender, transport kind, delivery identifier, and claimed bridge origin are recorded separately.

Fleet HMAC uses the current shared fleet token. This authenticates possession of the shared secret and binds the signed sender claim, but it is not a unique per-node cryptographic identity: any holder of the shared token can claim another allowlisted node. Treat authenticated_sender as the authenticated fleet sender claim, not as independent hardware identity. Per-node keys are not configured or introduced by this change.

No execution, deployment, secret mutation, network discovery, or privileged capability is added. The adapter is opt-in at the call site; no producer begins sending automatically. Grok Bot, CycleKernel, Great Scale, and external dashboards should exchange this versioned HTTP/event contract rather than copy AVRONE internals.
