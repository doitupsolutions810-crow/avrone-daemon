# AVRONE trial receiver mainline port

- Base: `94dc53af03523b80debfe732dff3c8b78df10b1d` (freshly inspected `main`, 2026-11-14).
- Source reference: `integration/trial-receiver-mainline-2026-11-12` at `2483eb68ac3146ff31bef5169b6cf053fc45ba30`.
- The source branch was 8 commits ahead and 79 commits behind current main; it was not merged wholesale.
- The v2 contract, SQLite receiver, bridge/fleet adapters, schemas, docs and adversarial tests are ported additively.
- Current-main BodyLimit behavior is preserved: the 4096-byte blackboard cap, all-Content-Length inspection and streaming enforcement remain intact. Trial paths add a 256 KiB cap including reverse-proxy mount prefixes.
- Current-main FleetManager gateway remains the authentication authority. Trial ingestion adds a capability and requires a non-empty explicit sender allowlist.
- Trial routes reject duplicate `x-avrone-ts`, `x-avrone-nonce` and `x-avrone-sig` ASGI headers before dispatch, nonce consumption or database initialization.
- Evidence source identity uses the existing NFKC/format-character removal/casefold policy through `canonical_source_key`; the wire `avrone.trial/v2` identity remains unchanged.
- Statistical independence remains `not_assessed`.
- This port is unverified until the new branch's Python 3.11 and 3.12 focused and full CI runs complete.
- Docker/Grok Bot verification is not claimed unless Docker Compose and both required services are available.
