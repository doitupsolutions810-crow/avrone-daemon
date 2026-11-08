# AVRONE trial receiver v1

## Route and trust boundary

AVRONE accepts bounded batches at \`POST /api/fleet/trials\`. The route reuses the
existing Fleet HMAC gateway and adds a dedicated \`trials:ingest\` capability. It
requires a non-empty explicit \`AVRONE_FLEET_ALLOW\` entry for the signed sender even
when the fleet's compatibility policy would otherwise allow all token holders.
The request body must be UTF-8 and is capped at 256 KiB and 32 events; each record is capped at 16 KiB.

Fleet HMAC signs method, canonical path, timestamp, nonce, and the exact body hash.
The shared fleet token authenticates membership in a shared-secret trust group; it
does not establish unique per-node cryptographic identity because any token holder
can assert an allowed node name. Stored provenance labels this accurately as
\`shared_secret_group_member\`. Strong per-node identity requires a future per-peer
credential or mTLS contract. The route rejects privileged flags and does not grant
agent execution, shell, admin, or recon capabilities.

## Storage and idempotency

Set:
- \`AVRONE_TRIAL_DB\`: absolute path to a persistent SQLite database file.
- \`AVRONE_TRIAL_RECEIPT_SECRET\`: independent high-entropy receipt key, at least
  32 characters and not the Fleet token.

The receiver fails closed with HTTP 503 until both are configured. SQLite uses WAL,
\`synchronous=FULL\`, \`busy_timeout\`, and \`BEGIN IMMEDIATE\`. The full record and
transport provenance are retained. \`trial_id\` is the immutable primary key.
An identical record is an idempotent duplicate and returns the original signed
receipt. The same ID with a different canonical payload or outcome writes a durable
quarantine row; the first accepted record and receipt are never overwritten.
Each delivery, including duplicates and conflicts, has a separate provenance row.

Batch members commit in individual transactions. If a later write is interrupted,
retry the batch with a fresh Fleet nonce: already committed records return stable
duplicates, and the uncommitted suffix can be accepted. Reusing the same Fleet nonce
is rejected by the Fleet replay cache. This is single-host SQLite; do not run
active-active multi-host receivers on independent local database files.

## Bridge adapter

The Bridge adapter reads the existing \`Bridge.run_id\`, \`Bridge.node_id\`, and
\`Bridge.get\` APIs and attaches a versioned \`avrone.bridge-trial/v1\` correlation
object. It verifies the run/node binding and that referenced posts are locally
originated. It does not write authoritative trial records into the generic Bridge
journal and does not treat Bridge's post-ID deduplication as trial idempotency.
The Fleet transport authenticates the resulting batch.

## Results

- First completed pass/fail: \`accepted\`, poolable.
- Exact retry with a fresh nonce: \`duplicate\`, same receipt ID.
- First non-pass operational outcome: \`operational_outcome_stored\`, not poolable.
- Exact non-pass retry: \`operational_outcome_duplicate\`, not poolable.
- Same trial ID with different payload/outcome: \`conflict_quarantined\`, HTTP 409.
- Malformed record/batch: HTTP 422; malformed or duplicate-key JSON: HTTP 400.
- Authentication/replay failure: HTTP 401; sender/capability refusal: HTTP 403.
- Oversized body/batch: HTTP 413/422; unconfigured or inaccessible storage: HTTP 503.

Only completed pass/fail records enter the pool snapshot. It retains the existing
\`independence: not_assessed\` label. Grok Bot, CycleKernel, Great Scale, and dashboards
remain external consumers: no writes are sent to them until each exposes a reviewed,
versioned HTTP/MCP/event contract and an explicit authentication policy.
