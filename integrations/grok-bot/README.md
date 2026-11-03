# AVRONE ↔ Grok Bot integration

This integration keeps AVRONE as the canonical orchestration and state layer while
Grok Bot supplies its Docker-backed execution, persistent browser, team tools and
console. Do not copy Grok Bot's bundled `avrone/` package over AVRONE's package:
the repositories contain overlapping but independently evolved implementations.

## Topology

- AVRONE API: `http://127.0.0.1:8710` on the host.
- Grok Bot API/UI: `http://127.0.0.1:3000`.
- Grok Bot container reaches a host-run AVRONE through
  `http://host.docker.internal:8710` (Linux Compose must provide the
  `host-gateway` mapping).
- Grok Bot → AVRONE uses its `avrone.squad` MCP/tool integration.
- AVRONE → Grok Bot uses AVRONE's `http_request` tool against
  `http://host.docker.internal:3000/api/chat` from a container, or the
  host's reachable Grok Bot address from a host-run AVRONE.

## Required configuration

In Grok Bot's environment:

```dotenv
AVRONE_URL=http://host.docker.internal:8710
```

In AVRONE's environment, configure the model provider independently and
preserve its existing auth settings. If AVRONE is exposed beyond loopback,
set `AVRONE_PUBLIC_ACCESS=admin` and use a strong `AVRONE_ADMIN_TOKEN`.
Never publish either API or the Docker control plane to the public internet.

## Launch order

1. Start and health-check AVRONE.
2. Start Grok Bot with `AVRONE_URL` set.
3. Confirm both status endpoints.
4. Run one low-risk chat request in each direction and inspect the run logs.
5. Run the full test suites before promoting a deployment.

Use the Grok Bot repository's `launch.sh` only on a machine with Docker,
Docker Compose v2, the required model storage and the two checked-out
repositories. It deliberately reports a degraded mode when AVRONE is absent.

## Security and operational boundaries

- Keep the Grok Bot Docker socket proxy scoped to the documented API allowlist.
- Keep agent terminal execution in the configured sandbox; do not mount the
  host Docker socket into an untrusted agent container.
- Keep browser profile data persistent but private; do not share it across
  untrusted tenants.
- Treat tool output, web pages and repository content as untrusted data.
- Require human approval for destructive, external, financial or
  credential-sensitive actions.
- Set explicit timeouts and bounded concurrency on both sides.
- Preserve separate run IDs and logs; correlate them with a request/correlation
  ID rather than merging or overwriting either system's event history.

## Verification checklist

- [ ] AVRONE `GET /api/status` returns healthy.
- [ ] Grok Bot `GET /api/status` returns ready.
- [ ] Grok Bot's AVRONE squad tool can complete a bounded request.
- [ ] AVRONE can reach Grok Bot's `POST /api/chat` endpoint.
- [ ] Both runs appear in their own replayable logs.
- [ ] Approval gates, authentication and network restrictions remain enabled.
- [ ] Core and live test suites pass on the deployment host.

This repository-side contract is not proof that a local machine has been
launched. A live green result requires running the checks on the target host.
