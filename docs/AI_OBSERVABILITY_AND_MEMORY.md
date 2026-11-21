# AVRONE AI observability and Redis Iris memory

Both integrations are optional. AVRONE's model execution, local JSONL memory, fleet sync, and
agent tools continue to work when either service is absent or unreachable.

## PostHog AI Observability

Install the optional SDK:

```bash
pip install -e '.[observability]'
```

Configure `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` in the deployment's secret store,
then set `AVRONE_POSTHOG_ENABLED=true`. The host is region-specific; do not assume US or EU.

The model gateway emits `$ai_generation` events for successful and failed provider attempts.
The agent tool runner emits `$ai_span` events with tool name, duration, and error state.
Trace IDs correlate one agent's model turns and tool execution; session IDs group agents
under a single AVRONE run.

**Privacy default:** prompts, model outputs, and tool arguments/results are not sent.
Set `AVRONE_POSTHOG_CAPTURE_CONTENT=true` only after approving cloud capture of those
payloads for the data handled by this deployment. Input/output token counts are captured when
the provider returns recognized usage fields (OpenAI-compatible `prompt_tokens` /
`completion_tokens`, `input_tokens` / `output_tokens`, or Ollama `prompt_eval_count` /
`eval_count`). Missing counts are omitted, never estimated. PostHog can calculate costs where
its model pricing catalog supports the model; local-model costs may remain unavailable unless
an explicit pricing configuration is added.

Check **AI Observability → Traces** in the configured PostHog project after a real agent run.
No telemetry is sent in mock-model mode.

## Redis Iris Agent Memory

Create a Redis Agent Memory service in Redis Cloud Iris (or deploy the service yourself).
Install the optional SDK:

```bash
pip install -e '.[redis-memory]'
```

Configure the service's endpoint, Store ID, and API key in a protected secret store:

- `IRIS_AGENT_MEMORY_URL`
- `IRIS_AGENT_MEMORY_STORE_ID`
- `IRIS_AGENT_MEMORY_API_KEY`
- `AVRONE_IRIS_OWNER_ID`: a stable authenticated owner/account ID
- `AVRONE_IRIS_ENABLED=true`

AVRONE records completed `chat`, `squad`, and `deep` task/answer pairs in owner-scoped
sessions. The existing per-agent `memory.jsonl` remains authoritative and continues to support
namespace isolation, atomic writes, and signed fleet synchronization. The `memory_recall`
tool appends Redis semantic recall results when a query is supplied.

Redis Iris extracts long-term memories asynchronously; a newly recorded session may not be
searchable immediately. Use the Redis service health check and extraction cadence configured
in Redis Cloud to validate the service.

**Access model:** this adapter uses one operator-level `AVRONE_IRIS_OWNER_ID`. It is suitable
for a single-owner self-hosted deployment. A multi-tenant deployment must pass the authenticated
user's stable ID per request and must not share a single owner ID across tenants.

The service is deliberately disabled by default. Missing credentials, missing optional SDK,
or Redis errors never disable or corrupt AVRONE's local memory system.

## Operational validation

After configuring the secrets on a connected runtime:

1. Run one real (not mock) chat request and confirm a PostHog generation and tool spans appear.
2. Run a squad and a deep review; confirm separate agent traces group into their run sessions.
3. Confirm Redis contains one user and one assistant event for each completed task.
4. After Redis extraction runs, call `memory_recall` with a query and confirm owner-scoped long-term memories appear alongside local results.
5. Stop Redis or remove one optional integration's credentials and rerun; AVRONE should still complete using its local memory and configured model providers.
