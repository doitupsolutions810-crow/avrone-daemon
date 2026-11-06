# Great Scale evidence receiver v1

The daemon exposes POST /api/evidence/events. It fails closed with 503 until configured:

- AVRONE_EVIDENCE_HMAC_SECRET: high-entropy shared secret, at least 32 characters.
- AVRONE_EVIDENCE_RECEIPT_SECRET: independent high-entropy receipt secret, at least 32 characters.
- AVRONE_EVIDENCE_DB: absolute path to a persistent SQLite file; relative paths and :memory: are rejected.

Do not reuse fleet/admin credentials. Inject secrets through a secret manager; never commit or log them. Back up the database and WAL using SQLite-consistent backup procedures.

## Request authentication

Send exact UTF-8 JSON bytes with Content-Type application/json and headers:
- X-AVRONE-Evidence-Timestamp: Unix seconds, maximum clock skew 300 seconds.
- X-AVRONE-Evidence-Nonce: unique 16–64 ASCII alphanumeric characters.
- X-AVRONE-Evidence-Signature: lowercase hex HMAC-SHA256 over METHOD, PATH, TIMESTAMP, NONCE and SHA256(BODY), newline-delimited.

The endpoint enforces a 256 KiB streaming body limit, strict JSON (duplicate keys and non-finite numbers rejected), the existing avrone.greatscale.evidence/v1 contract, and timestamp/signature validation before storage.

A nonce is single-use. A network retry must use a fresh nonce and signature; the event digest remains the idempotency key.

## Storage and receipts

SQLite uses WAL and synchronous=FULL. One transaction inserts the nonce and event. Unique constraints cover the content digest and (source, run_id, check_id) identity.

- First valid event: 201, accepted=true, duplicate=false.
- Same digest with fresh nonce: 200, accepted=true, duplicate=true, same receipt ID.
- Same identity with a different digest: 409.
- Reused nonce: 409.
- Authentication: 401; malformed JSON: 400; invalid contract: 422; unsupported media: 415; oversize body: 413; unconfigured storage: 503.

Receipts are HMAC-authenticated with AVRONE_EVIDENCE_RECEIPT_SECRET. Consumers must verify the signature and event_digest before accepting them. The endpoint stores evidence only: it does not authorize capabilities, trigger tools, or assert human identity. SHA-256 is a content identifier; HMAC proves shared-secret possession, not human identity or independent authority.

This first version is single-host SQLite. Do not run active-active multi-host replicas against local SQLite. Before production, define DB/WAL protection, filesystem permissions, backup/retention, secret rotation, and a shared transactional backend if multi-host scale is required.
