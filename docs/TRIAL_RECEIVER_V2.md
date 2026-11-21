# AVRONE Trial Receiver v2 (wrapper contracts v1)

## Scope

AVRONE remains the canonical orchestration and state authority. This receiver adds a durable evidence-ingest boundary without changing the existing Bridge JSONL post format, fleet run/message wire payloads, or Great Scale calculations.

The envelope schema is avrone.trial/v2. Its ID is a SHA-256 digest of canonical execution identity. Outcome is deliberately excluded from the digest. Re-delivery of the same attempt is idempotent; a different outcome for that same attempt collides and is quarantined rather than replacing the first receipt.

## Contracts

- Fleet endpoint: POST /api/fleet/trials
- Bridge relay endpoint: POST /api/fleet/bridge/trials
- Fleet wrapper schema: avrone.trial-ingest/v1
- Bridge wrapper schema: avrone.bridge-trial-ingest/v1
- Envelope schema: avrone.trial/v2
- Capability: evidence:trial:ingest

Both endpoints pass through FleetManager.dispatch_gateway before an adapter is invoked. The request body is HMAC-SHA256 signed over method, canonical fleet path, timestamp, nonce, and body hash. Replay nonces, timestamp bounds, hop bounds, capability checks, and the explicit non-empty sender allowlist are enforced. The route also reparses the original bytes with duplicate-key and non-finite-number rejection before storage.

Trial-ingest bodies are hard-capped at 256 KiB; batches contain 1..10 envelopes; each envelope is capped at 24 KiB; configuration is capped at 16 KiB, depth 32, and 4,096 JSON values. Identity fields are NFC-normalized and bounded. Configuration keys and string values are NFC-normalized; key collisions after normalization are rejected.

## Identity and outcomes

Identity includes schema version, source system, source node, run ID, check ID, check version, canonical configuration digest, and attempt ID. System and node identifiers cannot contain the colon delimiter used by the source projection. The source projection is system:node and is capped at 127 characters to remain compatible with the existing evidence pool's 128-character source limit.

Outcomes are pass, fail, skipped, cancelled, timed_out, or inconclusive. Only pass/fail records are projected into pool_trials. Non-pass outcomes are durably stored and returned as non_pass_stored; they are never coerced to pass or fail.

## Persistence and conflict handling

SQLite WAL storage is created under <data_dir>/trials/trials.sqlite3 with FULL synchronous mode. Each envelope is committed in its own BEGIN IMMEDIATE transaction. The first canonical payload wins. An identical retry is recorded as a duplicate receipt. A changed payload with the same trial ID is stored in the quarantine table, the incoming sender is quarantined, and the original row is not modified. Subsequent deliveries from that sender return sender_quarantined.

The store caps accepted trial rows at 100,000, receipts at 100,000, quarantine records at 1,000, and quarantined senders at 1,000. Capacity exhaustion fails closed. SQLite transaction rollback prevents a partially written envelope from becoming visible. The receiver does not delete or rewrite the existing Bridge journal.

Storage hardening rejects symlinks anywhere in the database path's ancestor chain, non-regular database files, and pre-existing database files with multiple hard links. The database directory is restricted to mode 0700 and the database file to mode 0600. Strict request JSON rejects duplicate keys, non-finite and binary64-underflowing numbers, invalid UTF-8, unpaired surrogate values/keys, excessive nesting, and excessive node counts. The regression suite plants symlinks at SQLite's `-wal` and `-shm` sidecar paths and verifies that external sentinel targets remain unchanged.

These are fail-closed checks against pre-existing path redirection. They do not eliminate every possible time-of-check/time-of-use race against a hostile local process that can mutate the receiver's private directory concurrently; keep that directory owner-private and isolate the service account.

The signed from value is recorded separately from the envelope's source fields. It identifies a fleet sender assertion covered by the shared HMAC secret and sender allowlist; it is not a per-node public-key identity. Deployments that require stronger peer identity need a separate per-peer credential or mutually authenticated transport.

## Adapter boundaries

- FleetTrialAdapter sends and receives the fleet wrapper.
- BridgeTrialAdapter sends and receives a bridge-run-scoped wrapper. It leaves ordinary Bridge posts and their JSONL journal unchanged.
- Grok Bot, CycleKernel, Great Scale, and dashboards exchange only the versioned HTTP/MCP/event contracts. They must not copy AVRONE's internal implementation or write directly to the SQLite database.
- Great Scale receives only the accepted pass/fail projection. Independence remains not_assessed unless a separately reviewed sampling design supports another conclusion.

## Result states

accepted, duplicate, non_pass_stored, conflict_quarantined, and sender_quarantined are explicit per-envelope outcomes. A mixed batch reports partial. Malformed wrappers/envelopes are HTTP 400; HMAC/replay/allowlist failures remain 401/403; oversized bodies are 413; storage/capacity failures are 503.

## MCP and event integration contract

An external MCP tool may expose avrone.trial.ingest with the same wrapper schemas, but its authenticated principal must be mapped to an allowlisted fleet sender before calling the receiver. Suggested events are avrone.trial.accepted.v1, avrone.trial.duplicate.v1, avrone.trial.non_pass.v1, and avrone.trial.quarantined.v1. These are contract names only; no external MCP server or dashboard is claimed to be deployed by this change.


## Receiver database schema version 3

**Version 3 is reserved for the provenance-digest layout on this integration stack.** A separate
open event-sequence proposal also used schema version 3 for a different table layout. Those two
branches are not interchangeable: do not merge or cherry-pick the event-sequence v3 migration
verbatim. Re-port its high-water feature as a subsequent schema version (v4) after this migration,
with one ordered transaction and explicit tests for both feature sets. A database that claims v3
but lacks the provenance columns now fails closed instead of receiving nullable columns without
a backfill.

The receiver database uses transactional schema migrations. Version 0 is the legacy unversioned
layout; version 1 adds per-delivery `bridge_run_id` provenance to receipts and quarantine records.
Version 2 adds a unique `source_identity_key` index. The key uses the evidence pool's NFKC,
format-character removal, whitespace-collapse, and case-fold source equivalence rules, combined
with run ID, check ID, check version, configuration digest, and attempt ID. The wire-level
`avrone.trial/v2` trial ID is unchanged.

During migration, existing canonical envelopes are revalidated and their source identity keys are
backfilled inside `BEGIN IMMEDIATE`. If legacy rows contain two different trial IDs for the same
canonical source/check/attempt identity, migration fails closed and rolls back; an operator must
review the conflicting records before retrying. Future user_version values are refused without
downgrade. DDL is executed statement-by-statement so a failed migration does not leave partial
columns or indexes.

Bridge run provenance remains per delivery: accepted, duplicate, and conflicting bridge deliveries
retain their own canonical bridge run ID in the receipt or quarantine record. Fleet deliveries retain
NULL. The original trial row continues to preserve first-seen sender, transport and bridge scope.

New ingestion also compares the storage-only source identity key before accepting a new trial ID.
A source alias that the evidence pool would normalize to the same source and attempt is quarantined
instead of becoming a second confirmation. A changed check version, configuration digest, or
attempt ID remains a distinct identity.

The unique index is supplemented by insert/update triggers that reject NULL, non-text, non-hex,
or non-64-character source identity keys. This preserves the invariant on migrated databases,
where SQLite's additive column migration cannot retroactively add a NOT NULL constraint.



## Receiver database schema version 3 — provenance digests

Schema v3 adds a domain-separated SHA-256 provenance digest to first-seen trial rows,
per-delivery receipts, conflict-quarantine rows, and sender-quarantine enforcement anchors.
The digest binds the associated trial/source-identity/payload digest or receipt result to
sender, transport, bridge-run scope, reason, and the exact binary64 receive timestamp.
Read paths recompute the digest before exposing the corresponding record. The bounded
receipt_records() audit reads at most 100,000 retained delivery receipts and verifies each
record before returning it.

The v2-to-v3 migration adds nullable columns transactionally, backfills in batches of 128,
validates the existing provenance field shapes, installs insert and provenance-digest-update triggers requiring a
lowercase 64-character hexadecimal digest, and advances PRAGMA user_version only after all
backfills and triggers succeed. The migration runs under BEGIN IMMEDIATE; any malformed
legacy provenance or write failure rolls back the column additions, digest backfills, and
version change. This migration snapshots the currently stored v2 metadata; it cannot retroactively
authenticate changes made before v3 because v2 had no receiver-metadata digest. Existing trial IDs,
envelope bytes, outcomes, source/check-version fields,
receipt order, and retention limits are unchanged.

These digests are unkeyed tamper-evident checks against partial or accidental metadata
rewrites. They are not signatures and do not stop a privileged actor from coherently
rewriting metadata and recomputing all local digests. Per-peer public-key authentication
and off-host signed anchoring remain separate controls.

## Remaining trust boundaries

Fleet HMAC uses the existing shared token and signed sender assertion. This authenticates the relay
under the shared-secret trust domain; it does not prove an individual peer's public-key identity or
independently attest the producer's claimed system/node. Statistical independence remains
\`not_assessed\`. Filesystem path checks reject static symlink/hardlink cases, but descriptor-based
protection against path replacement races remains a separate hardening item.


## Read-side evidence integrity

Before returning the pass/fail evidence pool or non-pass records, the receiver now streams
all retained trial rows in bounded batches and verifies the canonical envelope bytes,
payload SHA-256, identity projection, source identity key, first-sender/transport
provenance, and receive timestamp. Non-pass rows are verified as well, so a corrupted
status cannot hide outside the pass/fail pool. Quarantine reads similarly verify the
stored incoming envelope, digest, sender, transport, bridge-run provenance, reason,
and timestamp before exposing a conflict record. They also cross-check every conflict
sender against the first-trial/reason/timestamp anchor in the sender-enforcement table.
Ingestion verifies the authenticated sender's quarantine row and first conflict record
inside the same write transaction before deciding whether that sender remains blocked.
Quarantine audits use one SQLite read transaction so both tables are checked from one
consistent snapshot.

These checks detect partial, accidental, or single-field local rewrites. They are not
a cryptographic defense against a privileged actor who can coherently rewrite the
canonical envelope, its digest, every projected column, and related SQLite state.
Off-host signed anchoring remains a separate control.

