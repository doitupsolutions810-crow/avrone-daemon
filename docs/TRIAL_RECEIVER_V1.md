# AVRONE Trial Receiver v1

## Contract

The envelope is avrone.trial/v1. Its deterministic trial_id binds the normalized
system, node, run, check, check version, configuration digest, and attempt identity.
The outcome is deliberately excluded from the ID so a retry that changes a result
cannot become a second trial.

Allowed outcomes are passed, failed, skipped, cancelled, timed_out, and
inconclusive. Only passed and failed are projected into
avrone.evidence_pool.pool_trials. Operational outcomes are stored and reported
separately; they are never coerced to pass. independence is always
not_assessed until a reviewed sampling design supports a stronger statement.

Identity strings and check-configuration strings are normalized to Unicode NFC.
Configuration JSON is bounded to 16 KiB, depth 32, and 4,096 nodes; object keys
must be strings and normalization collisions are rejected. source is
system:node and is bounded to 127 characters to fit the existing evidence-pool
source limit.

## Authenticated fleet transport

POST /api/fleet/trials accepts a JSON object with from, hops, capability
(evidence:ingest), request_id, and a trials list.

The request must use the existing fleet HMAC headers (x-avrone-ts,
x-avrone-nonce, x-avrone-sig) over the exact method, path, timestamp, nonce,
and body hash. The route requires a non-empty AVRONE_FLEET_ALLOW containing the
signed sender. It is bounded to 256 KiB and 100 trial records. The fleet token is
currently shared across peers: the signed from field is integrity-protected,
but any holder of that shared token can assert an allowlisted sender. This is an
existing fleet trust limitation, not a per-node credential system.

The run-scoped Bridge.merge_remote path is not used for evidence ingestion:
its current ID dedupe intentionally skips an existing ID without distinguishing
an identical retry from a conflicting outcome. The receiver writes a versioned
Codex event (evidence.trial.receipt.v1) after persistence for local bridge and
dashboard consumers; the SQLite evidence ledger remains canonical state.

## Receiver semantics

- First valid receipt: accepted.
- Same trial ID and identical canonical envelope: duplicate; receipt provenance is updated, trial count is unchanged.
- Same trial ID and different canonical envelope: store the conflicting envelope, quarantine the sending peer, and preserve the first receipt.
- Malformed record: rejected; no pool contribution.
- Quarantined peer: quarantined; no new records accepted.
- SQLite transaction/storage failure: HTTP 503; no success result is returned.
- Database integrity failure: fail closed; the existing file is not silently reset.

The SQLite ledger uses WAL, synchronous=FULL, BEGIN IMMEDIATE, unique trial IDs,
a receipt table, and a conflict quarantine table. The Great Scale snapshot is
computed from stored pass/fail records only and keeps independence=not_assessed.

## External integrations

Grok Bot, CycleKernel, and dashboards must exchange this envelope and receipt
event through versioned HTTP/MCP/event contracts. No external implementation is
copied into AVRONE. The current repository contains the Grok Bot contract notes
but not the external Grok Bot service/API implementation, so its endpoint and
authentication behavior still require review in that repository before enabling
a production sender. No public deployment or privileged action is part of this
change.
