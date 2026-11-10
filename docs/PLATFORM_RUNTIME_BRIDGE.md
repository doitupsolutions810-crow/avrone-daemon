# Runtime platform bridge verification

The host application and `avrone_runtime` are separate services. This adapter
connects the existing tool registry to fixed, operator-configured HTTP endpoints.
The mission/task/swarm operation creates durable records; the gateway does not
expose agent launch or mission start operations. Record creation therefore does
not claim that an external agent or model has executed a mission.

The daemon previously lacked this plugin. The additive port preserves its newer
checkpoint, persistence, and fleet code. The bridge uses tenant API keys, stable
phase-specific idempotency keys, a 64 KiB response cap, a 12-second total deadline,
strict UTF-8 and bounded finite JSON, and explicit handling of uncertain writes.
Sibling endpoints must honor `Accept-Encoding: identity`.

## Configuration

```sh
AVRONE_PLATFORM_TARGETS_JSON='{"runtime":"http://127.0.0.1:8700"}'
AVRONE_BRIDGE_RUNTIME_TOKEN='<tenant API key>'
AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON='[]'
AVRONE_PLATFORM_WRITE_ENABLED=false
```

Enable writes only for an intentionally configured local or deployed runtime.
These are configuration examples; no production token is provided or changed.
Plugin discovery uses the application's existing `./plugins` directory.

## Repeat the actual HTTP compatibility check

Supply a trusted unpacked Runtime source tree:

```sh
python scripts/verify_runtime_bridge.py --runtime-source /path/to/runtime --output bridge-verification.json
```

The verifier exits nonzero on any failed invariant. It creates a temporary SQLite
database and loopback HTTP gateway, generates disposable tenant keys, and checks:

- default write denial without records;
- authenticated health and mission/task/swarm creation;
- stable replay, changed-payload conflict, and tenant isolation;
- twelve simultaneous same-key workflows;
- recovery after a task commits but its acknowledgement is lost;
- gateway restart and durable audit reconciliation.

The output records hashes of the actual plugin, runtime core, and gateway source.
The tested Runtime archive SHA-256 is
`868268433c68c9b81e05564378838dab4492c9a6a24ecaf07268a0209b7a7d84`.
This check validates record orchestration and replay, not external provider
availability, a VM sandbox, or exactly-once effects in third-party systems.

## Source registry

`docs/registry/repositories-2026-11-10.json` identifies the public repository
heads, PR heads and bases, syntax checks, and inspection limits. Private source
metadata is excluded. A PR census is not a full review of every diff.
