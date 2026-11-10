# Cross-system trial integration

AVRONE is the canonical orchestration and state authority. External systems integrate through versioned contracts rather than copied modules or shared database access.

## Producer requirements

A producer emits one avrone.trial/v2 envelope per completed attempt. It must preserve source system, source node, run ID, check ID, check version, configuration digest, and attempt ID. The outcome is one of pass, fail, skipped, cancelled, timed_out, or inconclusive. A producer must not generate a new attempt ID merely to retry delivery.

## Transport

- Fleet peers use POST /api/fleet/trials and avrone.trial-ingest/v1.
- Bridge relays use POST /api/fleet/bridge/trials and avrone.bridge-trial-ingest/v1.
- Both require the fleet HMAC, replay protection, hop bound, capability, and explicit receiver allowlist.
- Grok Bot and CycleKernel may expose the same schema through their own HTTP/MCP clients. They must forward the full envelope and preserve the receiver's result states.
- Great Scale and dashboards consume an AVRONE pass/fail snapshot; non-pass outcomes remain operational evidence and do not enter the pool.

## Prohibited shortcuts

Do not trust system, node, or source fields as authentication. Do not rewrite a conflicting record, convert a timeout into a failure, infer statistical independence from unique IDs, or write to AVRONE's SQLite file from another process. Do not widen privileged actions, network access, recon access, or public exposure as part of this integration.
