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
