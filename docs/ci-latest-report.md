# CI gate report (auto-generated)

- run: https://github.com/doitupsolutions810-crow/avrone-daemon/actions/runs/44961196902
- pytest exit: 1

```text
FAILED tests/test_cov11_a.py::test_route_of_bypass_error - AssertionError: assert 'direct' == 'env-proxy'
FAILED tests/test_greatscale_parity_contract.py::test_evidence_snapshot_shape_and_orientation_channel - assert 0.004975 == 0.004975000000000005
FAILED tests/test_master_prompt.py::test_committed_prompt_is_current - AssertionError: run: python tools/make_master_prompt.py
...(tail)
    - **exposure-hardening**: Before a node is reachable from a network, make sure nobody without a token can start agents or read runs - check the bind, AVRONE_PUBLIC_ACCESS and tokens, and say exact
    - **fleet-offboarding**: Remove a node from the fleet for good - decommissioned, misbehaving or possibly compromised - so it cannot rejoin or keep being called.
    - **fleet-restart-safety**: Restart, redeploy or move a fleet node without reopening replay of signed requests, and confirm the node rejoined cleanly.
    - **flood-response**: A node is being flooded (huge requests, request bursts, peer churn) - find which cap is absorbing it, confirm the node holds, and tighten the right knob.
    - **incident-triage**: Triage a failed or misbehaving AVRONE run from its Codex event log: find failing tool calls, reproduce, self-heal, and report.
    - **lesson-hygiene**: Keep the recursive learning loop honest across runs - recall what earlier runs learned, re-check it against the code, retract lessons that turned out wrong, and close ope
    - **load-limits**: Explain or tune why AVRONE refused a run (HTTP 429 or 413, an MCP busy result, an agent cap) and size the load caps for the host it runs on.
    - **local-model-ops**: Run AVRONE on a free local model server (Ollama), check the model route and circuits, pick a model by the live bench rather than by guesswork, and keep a remote fallback 
    - **log-integrity**: A run's history looks wrong - a run view cut short, events missing, a search that misses what you know happened - check the run logs themselves before trusting or pruning
    - **log-retention**: Keep this node's run logs from filling the disk - measure, clean secrets first, then prune old logs while keeping recent and active runs.
    - **mcp-client-check**: Confirm this node works as an MCP server for an outside client (Claude Desktop, Cursor, another agent) before wiring it up, and diagnose a client that cannot see or call 
    - **network-mapping**: Map how far your environment reaches on the network, safely, using only your own host and public endpoints.
    - **peer-onboarding**: Add a node to the fleet or review existing peers so this node only ever calls addresses it should.
    - **recon-advanced**: Authorized network reconnaissance playbook stressing one-target-per-call and allowlist discipline.
    - **release-verification**: Prove a hardening change before calling it shipped - reproduce the finding on the old version, show the new one refuses it, and leave the node's own state clean.
    - **runaway-process**: Find and handle a stuck or runaway command started by this node - a shell or python step that never returned, a zombie, or a loop that kept a child alive - without losing
    - **secret-hygiene**: Find and clean secrets that leaked into this node's run logs, data or workspace, and keep new ones out - without ever printing a secret value.
    - **self-evolve**: "How an AVRONE agent safely improves its own code via evolve_propose: isolated clone, full-suite gate, new-files-only, read test tail on reject."
    - **self-heal-environment**: When a command or library is missing, fix your own environment and continue instead of stopping.
    - **state-recovery**: Find and recover from torn, corrupt or set-aside state on this node (fleet registry, evolve proposals and loops, checkpoints, agent packs, memory, run bridges) without lo
    - **surface-audit**: Audit what this node exposes and what it is running - HTTP routes and their auth, enabled admin and fleet access, child processes, persisted state - and report risks with
    - **test-coverage-sweep**: Find AVRONE's weakest-covered modules with pytest --cov, write offline tests for them, pass the gate, and propose them via evolve_propose.
    - **toolset-hardening**: Use the self-evolve loop to harden AVRONE's own tools - audit the toolset, pick a weak tool, prove the weakness with a failing test, and land the fix through a gated prop
    
    ## 7. Environment knobs (every `AVRONE_*` the code reads)
    `AVRONE_ADMIN_TOKEN`, `AVRONE_ADMIN_TOKEN_PREVIOUS`, `AVRONE_AGENTS_CAN_SHELL`, `AVRONE_AGENT_CONTAINER`, `AVRONE_AGENT_CPUS`, `AVRONE_AGENT_DIR`, `AVRONE_AGENT_DOCKER_ISOLATE`, `AVRONE_AGENT_FS`, `AVRONE_AGENT_FS_MAX_FILE_MB`, `AVRONE_AGENT_FS_QUOTA_MB`, `AVRONE_AGENT_IDLE_SECS`, `AVRONE_AGENT_MEM_MB`, `AVRONE_AGENT_MIGRATE`, `AVRONE_AGENT_MIGRATE_INTERVAL`, `AVRONE_AGENT_TIMEOUT`, `AVRONE_ALLOW_ABS_READ`, `AVRONE_ALLOW_NETWORK`, `AVRONE_ATTESTED_HISTORY_LOG`, `AVRONE_BREAKER_COOLDOWN`, `AVRONE_BREAKER_THRESHOLD`, `AVRONE_BRIDGE_MAX_POSTS`, `AVRONE_BRIDGE_ROUNDS`, `AVRONE_CAPGATE`, `AVRONE_CAPGATE_ATTEST`, `AVRONE_CAPGATE_KEY`, `AVRONE_CAPGATE_POLICY`, `AVRONE_CAPGATE_TTL`, `AVRONE_CHECKPOINT_MAX_BYTES`, `AVRONE_CODEX_ARG_CHARS`, `AVRONE_CODEX_FIELD_MAX_CHARS`, `AVRONE_CODEX_LOG_KEEP`, `AVRONE_CODEX_LOG_MAX_BYTES`, `AVRONE_CODEX_REDACT`, `AVRONE_COMMAND_TIMEOUT`, `AVRONE_CONTEXT_CHARS`, `AVRONE_DATA_DIR`, `AVRONE_DEEP_ROUNDS`, `AVRONE_DEEP_SQUAD_SIZE`, `AVRONE_DOCKER_IMAGE`, `AVRONE_DOH_URL`, `AVRONE_ENV_FILE`, `AVRONE_EVOLVE_HISTORY`, `AVRONE_EVOLVE_LONG`, `AVRONE_EVOLVE_MAX_BG`, `AVRONE_EVOLVE_MAX_ITERS`, `AVRONE_EVOLVE_SKILL_MAX_BYTES`, `AVRONE_EVOLVE_TEST_CMD`, `AVRONE_EVOLVE_TIME_BUDGET`, `AVRONE_FLEET_ALLOW`, `AVRONE_FLEET_HEARTBEAT`, `AVRONE_FLEET_MAX_HOPS`, `AVRONE_FLEET_MAX_NODES`, `AVRONE_FLEET_NONCE_LOCK_S`, `AVRONE_FLEET_OFFLOAD`, `AVRONE_FLEET_OFFLOAD_TIMEOUT`, `AVRONE_FLEET_PARENT`, `AVRONE_FLEET_PRIVILEGED`, `AVRONE_FLEET_PRIVILEGED_MAX_HOPS`, `AVRONE_FLEET_REQUIRE_ALLOW`, `AVRONE_FLEET_TOKEN`, `AVRONE_FLEET_TOKEN_PREV`, `AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN`, `AVRONE_FUSION_STATE_PATH`, `AVRONE_FUSION_WATCH_PATH`, `AVRONE_GEOIP_URL`, `AVRONE_GOVERNOR`, `AVRONE_HOST`, `AVRONE_LEAD_MODEL`, `AVRONE_LESSONS`, `AVRONE_LESSONS_K`, `AVRONE_LESSONS_MAX`, `AVRONE_LLM`, `AVRONE_LLM_API_KEY`, `AVRONE_LLM_BACKOFF`, `AVRONE_LLM_BASE_URL`, `AVRONE_LLM_FALLBACKS`, `AVRONE_LLM_MODE`, `AVRONE_LLM_MODEL`, `AVRONE_LLM_RETRIES`, `AVRONE_LLM_TIMEOUT`, `AVRONE_LOCK_WAIT_S`, `AVRONE_LOG_ARG_MAX_CHARS`, `AVRONE_LOG_LEVEL`, `AVRONE_MAX_AGENTS_PER_RUN`, `AVRONE_MAX_BACKGROUND_RUNS`, `AVRONE_MAX_BODY_BYTES`, `AVRONE_MAX_CALLS_PER_TURN`, `AVRONE_MAX_MESSAGE_CHARS`, `AVRONE_MAX_PARALLEL`, `AVRONE_MAX_SPAWN_DEPTH`, `AVRONE_MAX_STEPS`, `AVRONE_MAX_WS_SUBSCRIBERS`, `AVRONE_MCP_CONFIG`, `AVRONE_MCP_MAX_BATCH`, `AVRONE_MCP_MAX_INFLIGHT`, `AVRONE_MCP_MAX_RESPONSE_BYTES`, `AVRONE_MCP_SERVER_CHILD`, `AVRONE_MCP_SHUTDOWN_GRACE`, `AVRONE_MCP_TIMEOUT`, `AVRONE_MEMORY_MAX_ENTRIES`, `AVRONE_MEMORY_MAX_NAMESPACES`, `AVRONE_MEMORY_MAX_REMOTE_NS`, `AVRONE_MEMORY_MAX_VALUE`, `AVRONE_MIN_PARALLEL`, `AVRONE_NET_MAX_NODES`, `AVRONE_NET_MAX_REMOTE_AGENTS`, `AVRONE_NET_SUBSQUAD`, `AVRONE_NET_WORKER_MODE`, `AVRONE_NODE_ID`, `AVRONE_PLUGINS_DIR`, `AVRONE_PORT`, `AVRONE_PRIVILEGED_NODES`, `AVRONE_PRIV_SHELL_ALLOW`, `AVRONE_PRIV_SHELL_TIMEOUT`, `AVRONE_PROC_LONG_S`, `AVRONE_PROVIDER`, `AVRONE_PUBLIC_ACCESS`, `AVRONE_PUBLIC_URL`, `AVRONE_QUEUE_KEEP_FINISHED`, `AVRONE_QUEUE_MAX_PENDING`, `AVRONE_QUEUE_RETAIN_S`, `AVRONE_RECON`, `AVRONE_RECON_ALLOWLIST`, `AVRONE_REPLAY_MAX_EVENTS`, `AVRONE_REPO_DIR`, `AVRONE_RUNNER`, `AVRONE_RUNS_CACHE_S`, `AVRONE_SEARCH_PROVIDER`, `AVRONE_SELF_CHANGES`, `AVRONE_SELF_EVOLVE`, `AVRONE_SHARED_WORKSPACE`, `AVRONE_SHELL_PIPE_GRACE`, `AVRONE_SKILLS_DIR`, `AVRONE_SPAWN_CONTEXT_CHARS`, `AVRONE_SQUAD_DISPATCH`, `AVRONE_SQUAD_SIZE`, `AVRONE_TELEMETRY_MAX_FILE_BYTES`, `AVRONE_THERMAL_LIMIT_HOT_PCT`, `AVRONE_THERMAL_MAX_C`, `AVRONE_TOOLS_ONLY`, `AVRONE_TOOL_OUTPUT_MAX`, `AVRONE_VERSION`, `AVRONE_WORKSPACE`, `AVRONE_WS_SEND_TIMEOUT`
    
    ## 8. Latest releases (from README)
    ## v0.161: Round 168: Lesson task metadata redacted
    
    - **Before:** `bridge.distill_lessons` saved each lesson with `task=str(task)[:200]` raw into memory.jsonl, which fleet memory sync ships to the parent. `MemoryStore.scrub` only masked `value`, so `secret_scan fix` left those copies in place.
    - **Now:** the task metadata is redacted first and then cut. `scrub` also masks string metadata fields; `ns`, `key` and `ts` are left alone. A changed record gets a newer LWW ts and is counted as `meta` in the scrub report.
    - **Tests:** `tests/test_round168_lesson_task_meta.py` (2). Both fail on v0.160.
    
    ## v0.160: Round 167: Checkpoint previews redacted
    
    - **Before:** `checkpoint.describe` and `list_managed` returned the task preview and the answer preview raw. These feed `/api/checkpoints` and the checkpoint list, so a key or `--password` in the task text was served back unchanged.
    - **Now:** `_preview` redacts the text first and cuts it second, so a cut can't leave part of a secret behind.
    - **Unchanged:** stored steps stay exactly as written, so resume works as before.
    - **Tests:** `tests/test_round167_checkpoint_preview.py` (3). 2 fail on v0.159.
    
    ## v0.159: Round 166: Job ids that sqlite cannot bind
    
    - **Before:** `JobQueue.get("\ud800")` raised `UnicodeEncodeError`, and `queue_status` passed `job_id` straight through to it.
    - **Now:** `get`, `complete`, `fail`, `extend` and `release` treat an id or token that is not a str, or that holds a lone surrogate, as naming no job. `get` returns `None`, `fail` returns `"missing"`, and the others return `False`.
    - **Tests:** `tests/test_round166_job_ids.py` (4). 2 fail on v0.158.
    
    ## 9. Open threads (pick up here)
    - Deploy target undecided: own VPS (preferred for Ollama/GPU), fly.io, or Railway. `data_dir` must be
      a persistent volume (fleet replay cache lives there).
    - Re-run `tools/model_bench.py` on llama3.2:3b, qwen3.5:4b, gemma3:4b on a host with enough RAM;
      pick the final default model.
    - Port the Ollama setup into the `grok-bot-` repo (packaged AVRONE); untouched so far.
    - Docker image builds only where Docker exists (not in every sandbox).
    - Remaining missed lines: run coverage with `term-missing`; `bridge.py` has one provably unreachable line.
    
    ## 10. First message to send the owner
    Run section 0, then reply in one line: the version, test count, coverage, and the next round's
    target. Example: "AVRONE v0.161.0 checked out: 3 passed, 99% coverage. Next: round <N+1> on <angle>."
3 failed, 11818 passed, 318 skipped, 64 warnings in 350.03s (0:05:50)
```
