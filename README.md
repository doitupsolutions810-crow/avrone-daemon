# AVRONE

A self-hosted chat + search system where agents don't just answer, they **work**:
they get a real terminal, a workspace, network access and Docker, they spawn
sub-agents, and every action they take is logged and shown live on a HUD.

Orchestration follows the **Grok Heavy** pattern: a squad of agents attacks the
same request in parallel from different angles, then a **Lead** agent compares
their work like a study group, resolves disagreements, and writes one answer.

The model brain is your **OpenClaw** Gateway by default (any OpenAI-compatible
backend works too: xAI Grok, OpenAI, Ollama, LM Studio, vLLM).

## Architecture (from the sketch)

```
 Ghost Shell   (server.py, static/)   Web UI, REST API, WebSocket HUD, admin terminal
      │
 Trinity Core  (trinity.py)           Daemon kernel: PLAN → SQUAD (parallel) → LEAD merge,
      │                               tool loop, sub-agent spawning
      ├── Eternal CodeX (codex.py)    Event bus + permanent JSONL action log per run (replayable)
      │
 Sovereign OS  (sovereign.py)         Environment established FIRST on launch: Docker container
                                      ("VM environment") or local sandbox, health-checked
      │
 Model backend (llm.py)               OpenClaw Gateway /v1/chat/completions (or any OpenAI-compatible)
```

| Sketch item | Where it lives |
|---|---|
| AVRONE chatbot/search, website/phone/app | Web UI (mobile-friendly) + REST API any app can call |
| Chat orchestrate / Agent orchestrate | `chat` mode (one agent) / `squad` mode (Grok Heavy) |
| Agent action logs | Eternal CodeX: `/api/runs`, `/api/runs/{id}`, live Logs tab |
| Admin-level / terminal access | `/api/terminal` + Terminal tab, gated by `AVRONE_ADMIN_TOKEN` |
| Backend environment hosting so agents can install resources | Sovereign OS runner, `shell` tool |
| Long-term artifacts, Docker, network, real terminal | `background: true` runs, Docker runner, `fetch`, `shell` |
| HUD display for hosted environments | HUD tab: live agent tree, current tool, status |
| Agents spawn sub-agents | `spawn` tool (depth-limited by `AVRONE_MAX_SPAWN_DEPTH`) |

## v0.2: Deep Analysis, MCP, plugins, 17 tools

**Deep Analysis (second wave).** Give AVRONE a question plus an answer that
already exists (from you, another AI, or its own squad) and a second squad
stress-tests it: Fact-Checker, Gap-Finder, Contrarian, Deep-Diver, Forecaster,
Source-Synthesizer. The Deep Lead returns: Verdict (confirmed / corrected /
extended + confidence), Corrections, Additions, Counterpoints, Sources, Bottom line.

```bash
avrone-cli deep "Is X true?" --first-answer-file answer.txt -n 4
avrone-cli deep "Plan a home lab"            # runs wave 1 squad, then wave 2
```

**Tools.** shell, python, read_file, write_file, edit_file, list_files,
search_files, fetch, http_request, web_search (DuckDuckGo free; Brave/Tavily
if keyed), git (no force push), docker_run, memory_save, memory_recall,
artifact_save, think, spawn. Plus plugin and MCP tools.

**MCP, both ways.** AVRONE loads any MCP server from `mcp.json` (Claude Desktop
format, stdio or Streamable HTTP; see `examples/mcp.json.example`); its tools
appear as `mcp__<server>__<tool>`. And `avrone-mcp` serves AVRONE itself over
stdio with `avrone_squad`, `avrone_chat`, `avrone_deep`, `avrone_tools`, so
OpenClaw, Claude Desktop or Cursor can call the squad.

**Plugins.** Drop a `.py` with `def register(registry, settings)` in `plugins/`
(examples: calculator, current_time) or publish an `avrone.plugins` entry point.

**Providers.** `AVRONE_PROVIDER=openclaw|xai|openai|openrouter|ollama|custom`,
with `AVRONE_LEAD_MODEL` for a stronger merge model. Retries with backoff.

**CLI.** `avrone-cli squad|chat|deep|tools|runs [--json out.json] [--quiet]`.

## v0.3: Telemetry + System Map

AVRONE traces its own nodes: Trinity Core, Eternal CodeX, the Sovereign OS
runner, worker and lead models, every tool (built-in, plugin, MCP) and every
agent role, with the traffic between them built from the action logs. It never
probes outside networks.

- `GET /api/telemetry?hours=24`: runs (total, live, by mode, p50/p95 duration), agents by role, per-tool calls, errors and average latency
- `GET /api/map?format=json|mermaid`: node/edge graph, or a Mermaid flowchart you can paste into GitHub
- **Map** tab in the UI: live graph + headline metrics
- `avrone-cli telemetry` and `avrone-cli map [--format mermaid|json]`

## v0.76: Round 83: env-secret cache, codex run logs never follow links. 5,584 tests + 19 integration

- **Env-secret cache.** `ctxspawn._env_secret_values` rebuilt the secret list from `os.environ` for every string of every CodeX event, which cost 751 µs per call with 500 variables. It is now cached against a snapshot of the environment, checked with a C-level dict compare. A variable set, changed or removed at runtime takes effect on the next call. A short redact now takes 19.5 µs.
- **Run logs.** `Codex._append` used `path.open("ab+")` on `runs/<run>.jsonl`.
  - A symlink planted there made every event of that run append to the link's target.
  - A FIFO raised `io.UnsupportedOperation` out of `emit()`, which killed the agent step that was logging.
  - The log now opens with `O_RDWR|O_APPEND|O_CREAT|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`, and only a regular file is written. Any other log is skipped and counted in `Codex.dropped`, with one warning per name. Subscribers still receive the event.
  - The torn-last-line repair and the single `O_APPEND` write are kept, and partial writes are now looped.
- **Tests:** `tests/test_round83_env_cache_and_log_links.py` has 6 tests. On v0.75, 4 fail and 2 are guards.
- **Not changed:** with many secret env vars, a 1 MB string still costs about 0.4 s, because there is one `str.replace` pass per secret. A combined regex would change results when secrets overlap, so it was left as is.

## v0.75: Round 82: redaction skips rules whose literals are absent. 5,578 tests + 19 integration

- **Before:** every CodeX event string went through all 11 redaction rules on the event loop: 10 regex passes plus the key/value tokeniser. That cost about 0.9 s per MB of plain text, so the 1,000,000-char field cap from round 81 still allowed a near-second stall.
- **Now:** `ctxspawn.redact` gates each rule on the literals it needs (`PRIVATE KEY-----`, `sk-`/`pk-`/`rk-`, `ghp_`…, `xox`, `AKIA`, `AIza`, `eyJ` + `.`, `://` + `@`). For the case-insensitive rules (authorization headers, bearer, key=value secret words) it checks case-folded text. Folding matches `re.IGNORECASE`: `str.lower()` plus U+0130/U+0131 → i, U+017F → s, U+212A → k. Those are the only non-ASCII code points `(?i)` matches against ASCII letters (exhaustive scan of all code points).
- **Speed:** 1 MB of plain text went from 0.91 s to 0.011 s. 1 MB of prose with URLs went from 0.22 s to 0.011 s.
- **Output unchanged:** byte-identical to running every rule. A differential fuzz over 140,000 random secret-fragment strings found 0 diffs. `secret_scan` still uses the same `_PATTERNS`.
- **Tests:** `tests/test_round82_redact_gates.py`, 7 tests. On v0.74, 2 fail (the fold table and the speed test). The other 5 are equivalence guards and pass on both versions.

## v0.74: Round 81: no state write follows a planted temp link, codex events size-capped. 5,571 tests + 19 integration

- **New `avrone/safeio.py`.** `atomic_write` creates its temp file with `mkstemp` (random name, `O_CREAT|O_EXCL`, never follows a link), writes, optionally fsyncs, sets the mode a plain `open()` would give, and `os.replace`s. On failure it removes the temp and re-raises.
- **Fixed predictable temp names.** `fleet.json`, `fleet_nonces.json`, the bridge journal (still fsynced), evolve `history.jsonl`, loop files and the boot-reconcile `.json.tmp`, agentkit's task board, and agent pack exports all used pid/thread-id temp names with `write_text()`. A symlink planted at that name had its target overwritten. All now use `atomic_write`. Evolve proposal meta and loop saves are now atomic too; before, a crash could tear them.
- **Nonce journal truncate** opens with `O_NOFOLLOW`, so a symlinked journal no longer truncates its target.
- **Codex event size.** Every event string runs through about ten redaction regexes on the event loop. One `tool.call` with a 20 MB `write_file` content took 10.9 s to emit, and the whole content went into the log. Strings are now clipped before redaction: `AVRONE_CODEX_FIELD_MAX_CHARS` (default 1,000,000, floor 1,024, 0 = off) for every event, and `AVRONE_CODEX_ARG_CHARS` (default 65,536) for `tool.call` args. The tool still gets the full args. A 20 MB emit now takes 0.65 s at the field cap.
- Tests `tests/test_round81_tmp_links_and_event_size.py`: 8 tests. On v0.73, 4 fail and the 2 `safeio` tests skip (module absent). The other 2 are guards.

## v0.73: Round 80: memory store read cache, clipped unknown tool names, temp-file symlinks. 5,563 tests + 19 integration

- **Memory reads are cached.** `MemoryStore._records` re-parsed all of `memory.jsonl` on every `load()`: 0.88 s at 200,000 lines, and `ctxspawn` calls it on the event loop for every spawn. Parsed records are now cached per file identity (dev, inode, size, mtime_ns), across `MemoryStore` instances, for up to `RECORDS_CACHE_FILES` (8) files. A warm load is 0.16 s. Callers get shallow copies, so mutating a result never changes the cache. Saves, rewrites and hand edits change the identity and force a re-parse.
- **FIFO memory file.** `read_text()` and `open("a")` blocked forever on a FIFO at `memory.jsonl`, so every memory op hung. Reads now open with `O_NONBLOCK` and treat a non-regular file as empty. Appends raise `MemoryRefused`.
- **Over-long keys.** A hand-edited key over `MAX_KEY`, which no save can make, is dropped at read. Large values stay readable, as decided in v0.20.
- **Unknown tool names are clipped.** A model that put a 1 MB string in `"tool"` got the whole string back in the error. It also landed in the codex `tool.call`/`tool.arg_error` events and in its next prompt as `TOOL RESULT (<1 MB>)`. `tools.clip_tool_name` caps the echo at `UNKNOWN_NAME_MAX` (120) chars plus a count, and `suggest()` skips difflib for names that long.
- **Temp files don't follow symlinks.** The capgate head temp file (`receipts.jsonl.head.<pid>.tmp`) and the memstore rewrite temp file use predictable names, and a symlink planted there was followed and its target overwritten. Both now unlink first and create with `O_CREAT|O_EXCL|O_NOFOLLOW`.
- Tests: `tests/test_round80_memstore_cache_and_names.py`, 9 tests. On v0.72, 6 fail and the FIFO case hangs (deselected with `-k "not fifo"`). The other 2 guard cache correctness.

## v0.72: Round 79: receipt chain under junk files, receipts off the loop, bounded spawn dir scan. 5,541 tests + 19 integration

- **Receipt lines that aren't objects.** `capgate.verify_chain` raised `AttributeError` on a line that's valid JSON but not an object (`[1]`, `5`, `"x"`, `null`), and `RecursionError` on deeply nested JSON. It now returns `unreadable line` with the line number. `_last_hash` returned a junk `hash` as is (`5`, 64 non-hex chars), and the next receipt linked to it. It now takes only a 64-char hex hash.
- **Chain files are plain files only.** As a FIFO, `capgate/receipts.jsonl` blocked `record()` forever. That call runs on every gated tool call, so one FIFO hung every agent. As a symlink, receipts were appended to the link's target. The chain, its lock and its head now open with `O_NOFOLLOW|O_NONBLOCK` and must be regular files. If they aren't, `record()` raises and `verify_chain` reports `receipts file unreadable`.
- **Receipts off the event loop.** `capgate.record` ran inline in tool dispatch for every gated call, with a cross-process flock, SHA-256 of the args and output, and two fsyncs. Another process holding the chain lock froze the node. An exception from it, like a full disk, lost the output of a tool that had already run. `tools._receipt` now runs it in a thread and logs a failure instead of raising.
- **Spawn context dir scan.** `ctxspawn._dir_files` sorted the whole `rglob` of the parent agent dir on every spawn, on the loop. A 100,000-file dir took 6.15 s; it now takes 0.026 s. It walks with `os.scandir`, depth-first, in the same order as before (checked against 30 random trees), and stops at `DIR_FILES`, or after `DIR_SCAN_MAX` (20,000) entries. Symlinked dirs are not entered.
- Tests: `tests/test_round79_receipts_and_spawn_scan.py`, 16 tests. On v0.71, 11 fail and the FIFO case hangs (deselected with `-k "not fifo"`). The other 4 guard behavior that already held. `test_cov8_a::test_ctxspawn_dir_files_oserror_and_cap` now patches `os.scandir` instead of `Path.rglob`.

## v0.71: Round 78: evolve state files and queued job payloads. 5,525 tests + 19 integration

- **Evolve state reads are plain-file only.** Proposal, loop and history files were read with `read_text()`, which follows symlinks, blocks forever on a FIFO and reads any size whole. `reconcile_interrupted()` runs at boot over `evolve/loops/*.json`, so a FIFO named `x.json` there hung startup. `list_loops`/`list_accepted`/`evolve_list` read symlinked files from outside the data dir. Every read now goes through `_read_state`: `O_NOFOLLOW|O_NONBLOCK`, regular files only, `STATE_JSON_MAX_BYTES` (16 MB) per file, `HISTORY_MAX_BYTES` (64 MB) for `history.jsonl`. Anything else is skipped as unreadable.
- **Malformed queued jobs go dead at once.** `run_job` trusted the payload from `jobs.db`, which every process on the data dir shares and which outlives versions. A job without `task` failed with `KeyError` on every attempt with backoff. A non-string task reached `squad()`/`dispatch()`, and an unknown kind ran as a dispatch. They now raise `jobqueue.PermanentJobError`, and the Worker calls `fail(..., permanent=True)`: dead now, attempts left or not. Ordinary errors still retry.
- Tests: `tests/test_round78_state_reads_and_jobs.py`, 15 tests. 12 fail on v0.70; the other 3 guard behavior that already held (valid jobs run, ordinary errors retry, a symlinked loop is not marked).
- Note: tag `v0.71.0` (`9b69f64`) has the code and tests but not this README section, which landed in the next commit on main. The tag is not moved.

## v0.70: Round 77: poisoned state files at boot. 5,510 tests + 19 integration

- **Nonce expiries are clamped.** Saved expiries in `fleet_nonces.json` and its journal were trusted. An `Infinity` entry never expired, so a file holding `NONCE_CACHE_MAX` of them would refuse every fleet request forever with "replay cache full". A 400-digit integer raised `OverflowError` out of `FleetManager()`, which left the node with no fleet at all. `nonce_expiry()` now drops junk and clamps an expiry to `now + 2 * WINDOW_S` (+5 s), the newest a real nonce can have.
- **Oversized nonce journal.** The journal was read whole into memory. Above `NONCE_JOURNAL_MAX_BYTES` (64 MB) it is now skipped, and the boot guard refuses anything signed before now.
- **Corrupt `jobs.db`.** A "file is not a database" error raised out of `start_queue` at boot, so `fleet.start()` and the fleet's cleanup never ran. The file (with its `-wal` and `-shm` files) is now moved aside as `*.corrupt-<ts>` and the queue starts empty. A locked database (`OperationalError`) still raises.
- **Non-object job payloads.** A payload that is valid JSON but not an object (a list or a number) failed in every handler with `AttributeError` until `max_attempts` ran out. `claim()` now marks it dead at once.
- Tests: `tests/test_round77_boot_state_junk.py`, 8 tests. 7 fail on v0.69; the eighth is a guard that a locked file is never moved aside.

## v0.69: Round 76: fleet.json records go through register()'s checks at load. 5,502 tests + 19 integration

- `fleet.json` outlives versions and can be edited by hand, but its node records were loaded as is. Now `clean_saved_node()` rebuilds each record with register()'s checks and drops a record that fails them, logging a warning with the count.
  - **URLs:** a url pointing at a cloud metadata endpoint (169.254.169.254), carrying credentials, using a non-http scheme, or missing was dispatched to without `check_node_url`.
  - **Ids:** a key that fails `ID_RE` (`../x`, `UPPER`) or equals this node's own id became a dispatch target. A record's own `node_id` could differ from its key; the key now wins.
  - **Missing node_id:** a record without `node_id` raised `KeyError` in `pick()`'s error path.
  - **Types:** capabilities, children, load, presence, health and last_seen of the wrong type or unclamped reached `node_has`, `fleet_nodes` and `health_score`. Health goes through `sanitize_health`.
- Behavior change: `test_robust_v025_state` now uses a valid url, because a record with url `"u"` is dropped at load.
- Tests: `tests/test_round76_fleet_json_load.py`, 6 tests, all fail on v0.68.

## v0.68: Round 75: junk fleet records and peer presence can't break node selection. 5,496 tests + 19 integration

- **`health_score` is total.** It cast raw fields with `float()`. A record loaded from `fleet.json` (written by an older version, or edited by hand) with headroom `"abc"` raised `ValueError` out of `select_nodes`, so every auto dispatch failed, along with `health_table` and `fleet_nodes`. NaN poisoned the ranking, and headroom `1e308` scored `inf` and won every pick. Each field now goes through `_finite` and is clamped, so the score is always within 0..100. Scores for valid values are unchanged.
- **`fleet_nodes` is bounded.** It shows 16 children plus a count, clips `runner` and the state to one line (control characters can no longer forge rows), and caps output at 200,000 characters. A non-dict `capabilities` no longer raises `AttributeError`.
- **Directory rows keep only known fields.** `_clean_env` and `_clean_agents` passed unknown keys and nested blobs through whole into `/api/fleet/directory` and the `fleet_agents` filter. Now only known presence fields survive: strings are clipped to 300 characters (task 2,000), non-finite numbers are dropped, and lists are short. The local node's environment is unchanged.
- Tests: `tests/test_round75_fleet_health_junk.py`, 7 tests, 5 fail on v0.67.

## v0.67: Round 74: the fleet directory holds up at 2,000 nodes. 5,489 tests + 19 integration

- **`find_agent` is one pass.** It used to rescan the whole directory for every match. With 2,000 rows x 50 agents all named `Worker-1` (peers control their presence), each `fleet_tell` / `agent_message` held the event loop for 9.6 s. Now it takes well under a second. The ambiguity error lists 20 matches plus a count instead of all 100,000. Lookup results are unchanged: a live agent still wins, and route/name still resolves directly.
- **`presence.render` is bounded** at about `RENDER_MAX_CHARS` (200,000) with an `...[output capped: i of N nodes shown]` footer. That tree used to render 35 MB (2 s) into a model's context through `fleet_agents`. `fleet_agents` now filters and renders in a worker thread and refuses a filter longer than 500 chars.
- **`GET /api/fleet`** builds and encodes the node list once, in a worker thread, instead of running `jsonable` and FastAPI's encoder on the loop.
- **Telemetry** opens logs with `O_NOFOLLOW | O_NONBLOCK` and reads regular files only. A planted symlink fed an outside file to the HUD, and a FIFO named `*.jsonl` hung it. **OSINT metrics** skip symlinked logs.
- Tests: `tests/test_round74_directory_scale.py`, 8 tests. 6 fail or hang on v0.66 (5 fail, and the FIFO case hangs).

## v0.66: Round 73: every codex log reader sees rotated archives. 5,481 tests + 19 integration

- **Readers looked only at the active file.** After round 72's rotation, `codex_search` missed a run's older events. It also said "run not found" in the instant between the rename and the next write. `codex_stats` reported a run as nearly empty right after a rotation. The HUD telemetry window lost events that a rotation had moved, and the OSINT rollup undercounted. All of them now walk `log_chain()`: the active log, then `.jsonl.1..N`, newest first.
- **`codex_stats`** reads back across archives within the same `CODEX_STATS_MAX_BYTES` budget and opens each file with `O_NOFOLLOW`. **Telemetry** reads an archive only while its mtime is inside the window.
- **Listing race and planted links.** `codex_search` sorted the run listing with a bare `p.stat()`, so a log that rotated or vanished between the glob and the stat raised `FileNotFoundError`, and a dangling symlink did the same. It also opened symlinked logs found by the glob. Both are now skipped. Directories named `*.jsonl` no longer count as runs. Archive hits are labeled with the run id, not the archive file name.
- New helpers in `avrone.codex`: `archives_of(path)` and `log_chain(path)`. A symlink is never an archive, a gap ends the chain, and a missing or non-file active log is left out.
- Tests: `tests/test_round73_rotated_readers.py` (13 tests, 9 fail on v0.65). `test_cov8_b` now expects 1 run where it counted a directory as a second.

## v0.65: Round 72: codex event logs rotate by size. 5,468 tests + 19 integration

- **Unbounded logs.** `fleet.jsonl` gains an event for every heartbeat, join, dispatch and refusal over the node's whole life. A long or looping run grows its own log until the disk is full. Logs now rotate once a write would pass `AVRONE_CODEX_LOG_MAX_BYTES` (default 64 MB, floor 4 KB, `0` turns rotation off). On rotation `<stem>.jsonl` becomes `<stem>.jsonl.1` and older archives shift up. Up to `AVRONE_CODEX_LOG_KEEP` archives are kept (default 3; `0` drops the old log). Shrinking KEEP also prunes the extra archives.
- **Readers.** Archives don't match `*.jsonl`, so `runs()`, telemetry and OSINT still see one file per run. `replay()` reads back through the archives, newest first, only while the newer files hold fewer events than asked. Events come back in order. `runs()` takes a run's start event from its oldest archive.
- **Edge cases.** An event bigger than the cap is still written whole. If rotation fails (an archive slot taken by a directory, a read-only dir), the event goes to the active log, so nothing is dropped. A symlinked archive is never followed. A torn tail from a crash stays isolated. 8 concurrent writers lose no events across rotations.
- **Cost.** One extra `stat` per emit: 28 to 33 us.
- Tests: `tests/test_round72_codex_rotation.py` (13 tests, 6 fail on v0.64).

## v0.64: Round 71: junk floods no longer fill the fleet audit log. 5,455 tests + 19 integration

- **Unauthenticated audit flood.** Anyone who can reach the port can trigger a refusal before the HMAC check (no token, a malformed timestamp or nonce, a bad signature). Each one wrote a full `fleet.refused` event to the codex log, which never rotates, and sent it to every dashboard. 5,000 junk requests left 1.16 MB of identical lines, so 1M would leave about 230 MB. These refusals are now audited up to `AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN` (default 60) a minute. Past that they are counted (`refused_unauth`), and one `fleet.refused.suppressed` event per window gives the count per code. `stop()` writes any pending summary. Refusals after authentication (stale, replay, allowlist, hops, capability) are still audited every time. 5,000 junk requests: 0.78 s and 1.16 MB before, 0.07 s and 14 KB after.
- **`/api/fleet/register` now audits** its pre-HMAC refusals too, under the same limit. Before, it audited nothing.
- **Token rotation hashed the body twice.** With `AVRONE_FLEET_TOKEN_PREV` set, `verify()` hashed the body once per token. It is now hashed once (`_mac`). A 16 MB body dropped from 21 ms to 10 ms.
- **Big fleet bodies leave the event loop.** Bodies over 256 KB (`OFF_LOOP_BODY_BYTES`) are verified and parsed in a worker thread, both in the gateway and on the register path. Small bodies stay on the loop, so heartbeats don't hop threads.
- Tests: `tests/test_round71_gateway_audit_flood.py` (7 tests, 5 fail on v0.63).

## v0.63: Round 70: a fleet join storm shares one registry write. 5,448 tests + 19 integration

- **Join storm rewrote the whole registry once per peer.** Every new node saves `fleet.json` at once, and each save encoded every record with `indent=2` while holding the state lock. 256 concurrent joins of 60 KB records meant 256 full rewrites in a row, about 18 s.
- **Group commit.** `_save()` now elects one leader. Concurrent savers wait while the leader writes the newest snapshot, again and again, until every waiter is covered. A `register()` or `forget()` still returns only after `fleet.json` holds its change. `register()` and `forget()` now save outside the state lock. If the leader's write fails, only its own caller sees the error, and a waiter takes over and retries. A save made while the caller holds the lock still works, without coalescing.
- **Compact JSON.** `fleet.json` is now written without indentation, which encodes about 3x faster (16 MB: 142 ms down to 47 ms). Readers are unaffected.
- **Measured:** 256 concurrent joins went from about 18 s to 0.72 s, with 29 writes instead of 256.
- Tests: `tests/test_round70_fleet_group_commit.py` (5 tests, 3 fail on v0.62).

## v0.62: Round 69: fleet heartbeats no longer rewrite the registry on the event loop. 5,445 tests + 19 integration

- **Problem:** every child heartbeat (every 15 s by default) calls `FleetManager.register()`, which ended in `_save()`. That serialized every node record (`json.dumps`, `indent=2`), wrote the file and replaced it, all synchronously inside the async `/api/fleet/register` handler. At 256 peers × 60 KB records, which is inside `DEFAULT_MAX_NODES` and `MAX_NODE_RECORD_BYTES`, each heartbeat cost 145 ms. 256 peers kept the event loop busy about 2.5 s out of every second.
- **Fix:** a heartbeat refresh (a known node at the same url) now saves at most once per `FLEET_SAVE_DEBOUNCE_S` (5 s). New nodes, url changes and `forget()` still save immediately, so membership stays durable. `stop()` flushes anything pending, and the new `flush()` does the same on demand. The handler now runs `register()` with `asyncio.to_thread`. A heartbeat costs 0.5 ms at the same bounds.
- **Contract change:** a refresh can take up to 5 s to reach `fleet.json`. Peers resend it every heartbeat, so a crash inside that window loses nothing. `test_robust_v022_async` now calls `flush()` before it reads the file.
- **Tests:** `tests/test_round69_fleet_register_save.py`, 7 tests; 4 fail on v0.61.

## v0.61: Round 68: fleet replay cache O(1) per request (journal). 5,438 tests + 19 integration

- **Problem:** `FleetManager.verify()` runs synchronously inside the async gateway handlers. Every accepted request pruned the whole nonce dict and rewrote `fleet_nonces.json` in full, so its cost grew with the cache: 0.8 ms per request at 1k live nonces, 8 ms at 10k, 42 ms at 50k and 85 ms near the 100k cap. A busy or hostile signed peer could saturate the event loop at roughly 100 requests per second.
- **Fix:** once more than `NONCE_JOURNAL_MIN` (1024) nonces are live, an accepted request appends one `<nonce> <expiry>` line to `fleet_nonces.json.journal`. The journal is compacted into the snapshot when it grows past half the live set, which keeps the amortized cost O(1). Expired entries are pruned at most once a second, and an expired nonce that has not been pruned yet no longer blocks reuse. Measured: 0.04 ms per request at 10k, 50k and 99k live nonces. Small caches keep the old full-snapshot path.
- **Replay protection is unchanged:** instances that share a data dir read each other's journal tail under the same flock. A restart loads the snapshot plus the journal. Compaction loses no nonce. Torn, junk and NaN journal lines are ignored. A full cache still refuses new requests rather than evicting a live nonce.
- **Tests:** `tests/test_round68_nonce_journal.py`, 7 tests; 5 fail on v0.60.

## v0.60: Round 67: symlink loops in agent and workspace paths. 5,431 tests + 19 integration

- **Symlink loops leaked host paths:** on Python 3.12, `Path.resolve()` raises
  `RuntimeError("Symlink loop from '<absolute host path>'")` (3.13 raises `OSError` ELOOP).
  `agentfs.confine`, `is_agent_dir`, `agent_dir`, `agents_root` and `tools._safe_path` did not catch it.
- An agent that planted `l1 -> l2 -> l1` got `ERROR: RuntimeError: Symlink loop from '/srv/.../ws/agents/x/l1'`
  back from `read_file`, `write_file`, `edit_file` and `image_inspect`. That leaked the host's absolute
  workspace path, skipped the `agentfs.denied` audit, and `DockerRunner._agent_cwd` raised a raw
  `RuntimeError` out of `runner.run`.
- **Fix:** new `agentfs.resolve()` raises `AgentFSError("path has a symlink loop")`, and every
  confinement resolve goes through it. `_safe_path` raises the same plain `ValueError`, and
  `is_agent_dir` returns `False`.
- **Probed clean:** listing, search and grep tools on trees with `sub/up -> ..` cycles and links to `/etc`.
- **Tests:** `tests/test_round67_symlink_loops.py` (12 tests, 11 fail on v0.59).

## v0.59: Round 66: restart stampede on the shared container, evolve worktree race, .git aliases. 5,419 tests + 19 integration

- **Shared container restart stampede (`DockerRunner.run`):** when `avrone-env-*` died, every command
  that hit it at once ran its own `docker rm -f` + `docker run --name <same>`. A later remover deleted the
  container an earlier one had just started, and overlapping runs hit a name conflict. With 6 concurrent
  commands, 5 failed. A restart lock plus a generation counter now means one restart per death: the rest
  wait and retry. A later death still restarts, and a failed restart is reported, then retried by the
  next command.
- **Evolve worktree race (`_ensure_worktree`):** it checked `wt.exists()` and then cloned into it. Two
  concurrent calls for one proposal cloned into the same dir, and the loser's plain-copy fallback
  rmtree'd the winner's clone. A crash mid-build left a half-made `wt-<pid>` that later calls returned
  forever. The clone is now built in `.tmp-wt-<pid>-<hex>` and renamed into place under a per-proposal
  lock. A failed build leaves nothing behind.
- **`.git` aliases (`_in_repo`):** `.GIT/config`, `.git./config`, `.git /config` and the 8.3 short name
  `GIT~1/config` all open `.git` on a case-insensitive (macOS default) or Windows filesystem, and
  `.git/config` can make git run commands (`core.fsmonitor`). These are refused now; `.gitignore`,
  `.github/...` and similar names still pass.
- Tests: `tests/test_round66_docker_restart_race.py` (3; the stampede test fails on v0.58) and
  `tests/test_round66_evolve_worktree_race.py` (14; 10 fail on v0.58).

## v0.58: Round 65: job queue retention and backlog cap; queue_submit junk input; flaky stall tests made robust. 5,402 tests + 19 integration

- **No retention:** finished jobs (done/dead, each up to 100 KB of result) were never deleted, so `jobs.db`
  only grew. `JobQueue.prune()` drops finished jobs older than `AVRONE_QUEUE_RETAIN_S` (7 days), then all
  but the newest `AVRONE_QUEUE_KEEP_FINISHED` (5000). It runs every 100 enqueues and never touches
  queued or running jobs. New index `jobs_finished(status, updated)`.
- **No backlog cap:** an agent in a loop could queue without bound. `enqueue()` raises `QueueFull` at
  `AVRONE_QUEUE_MAX_PENDING` (10,000). The count happens inside the insert's `BEGIN IMMEDIATE`, so two
  queues on one file land exactly at the cap (60 concurrent enqueues, cap 20: 20 stored, 40 refused).
- **`queue_submit`:** `int(args["priority"])` in the tool raised on `"x"`, NaN and `"1e400"`, and a
  missing task raised `KeyError`. Numbers now go through `_num` (junk → default, clamped to ±2^62), an
  empty or non-string task is refused, a task over 1 MB is refused before it is stored, and a full
  queue returns `ERROR: queue full`.
- **Flaky tests:** the round 57 `plain_copy` and round 58 agent-pack loop-stall tests failed under
  full-suite load (GIL slices over 0.25 s while the off-loop call took seconds). They now pass when the
  gap is under 0.25 s or under half the call's own time. A blocking call still fails, since its gap is
  about its whole duration.
- Tests: `tests/test_round65_queue_retention.py`, 19 tests; 16 fail on v0.57.

## v0.57: Round 64: fleet memory merge into a full namespace was quadratic. 5,382 tests + 19 integration

- **`MemoryStore.merge_many` eviction:** each new key merged into a namespace at its cap scanned the whole
  namespace twice, once for the "older than everything kept" check and once to pick the eviction victim.
  With `AVRONE_MEMORY_MAX_ENTRIES=5000`, a 4000-entry fleet merge took 6.7 s while holding the store lock,
  so every save, recall and other merge on the node waited. A per-call min-heap (`_Oldest`, lazy
  deletion, ties broken by dict order as `min()` did) brings it to ~0.06 s. A seeded property test
  checks resulting stores and stats against the old scanning algorithm exactly.
- **ReDoS sweep, extended:** all 146 inline `re.*(literal)` calls were timed against 32 repeated units.
  No new backtracking issue was found. The three hits were `match`/`fullmatch` used only at a fixed position.
  Probe: `/workspace/work/regexscan2.py`.
- **Probed, no change:** `memory_recall`/`memory_list` (off loop, bounded by the entry cap), `fleet_status`
  and `router_status` JSON row-drop loops (50 and 20 rows max), bridge `export`.
- Tests: `tests/test_round64_memory_merge_evict.py`, 12 tests; 11 fail on v0.56.

## v0.56: Round 63: ReDoS in the secret redactor every CodeX event passes through. 5,370 tests + 19 integration

A sweep of all 68 module-level regexes against long repeated units (`a.`, `a-`, `token`, `http://`, ...)
found catastrophic backtracking in `ctxspawn.redact`, which `codex._scrub` runs on every string of every
event, on the event loop, before it is logged or sent to `/ws/events`.

- **Key=value rule:** `[\w.-]*SECRET[\w.-]*\s*[:=]...` backtracked from every word boundary of a
  `a.a.a...` run (minified JS, dotted versions, paths). 4 KB took 0.7 s, 16 KB 11 s, 50 KB 99 s. One
  fetched page or one `write_file` call could hold the node for minutes. Now `_redact_kv`: identifiers
  are tokenised once and only those holding a secret word and followed by `:=` are checked. 50 KB takes
  ~4 ms. A seeded property test (12,000 cases) checks it against the old regex output exactly. It keeps
  its slot in `_PATTERNS`, so `secret_scan` rule names still line up.
- **URL-credential rules** (`ctxspawn`, `doctor`, `tools_extra`, `tools_introspect`,
  `tools_researchnote`): `\b[a-z][a-z0-9+.\-]*://` was quadratic too (~0.36 s per 8 KB). The scheme is
  now `{0,31}` and unanchored, so `<40 letters>http://u:p@h` is still redacted.
- Tests: `tests/test_round63_redact_redos.py`, 25 tests; 18 fail on v0.55 (the slowest cases were
  deselected for that run). Probe: `/workspace/work/regexscan.py`.

## v0.55: Round 62: edit_file's loose match fixed (corrupting overlaps, quadratic scan). 5,345 tests + 19 integration

- **replace_all corrupted files.** The trailing-whitespace fallback found OVERLAPPING matches and spliced them in reverse. Example: `"a\na\na\n"` with old `"a\na  "` became `"B\n"`, losing a line. It is now non-overlapping, left to right, like `str.replace` (`"B\na\n"`), and the "matches N times" count agrees with it.
- **Quadratic scan on the event loop.** `_loose_span` compared line by line: 60k lines with a 3000-line `old` took 13.3 s. It is now one `str.find` over the right-stripped text, realigned on line starts: 0.02 s. A seeded property test checks it against the reference algorithm (2,400 random cases).
- **Miss hints:** `_nearest_hint` ran difflib over every full line (100k lines: 3.8 s; 2000 lines of 5000 chars: 4.3 s). Lines are now clipped to 200 chars, and only the first 20,000 distinct lines are compared.
- `edit_file` now runs `off_loop`, and refuses files over `EDIT_MAX_BYTES` (32 MB), pointing to shell instead.
- Tests: `tests/test_round62_edit_file_loose_match.py` (15 tests, 13 fail on v0.54).

## v0.54: Round 61: agent-dir walks off the event loop. 5,330 tests + 19 integration

- **Every write into an agent dir froze the node.** `quota_write` (behind `fs_write`, and behind `write_file`/`edit_file` inside an agent dir) walks the whole agent dir with lstat to check the quota. On a 100k-file dir (one npm or pip install gets there), that was ~0.26 s per write on the loop, under a `threading.Lock`. It now runs in a worker thread, and the per-dir lock still serialises check plus write (6 concurrent 300 KB writes under a 1 MB quota: exactly 3 land).
- `fs_list` (rglob sort plus usage walk, ~0.6 s at 100k), `fs_delete` (rmtree plus usage), `fs_read`, `list_files` (sorts each directory level before its 400 cap) and `evolve_list` (reads every proposal; 3000 take ~0.15 s) are now `off_loop`.
- Found with an AST sweep for blocking I/O (open, read_text, walk, rglob, rmtree, stat) in async tools outside `to_thread`.
- Tests: `tests/test_round61_agentfs_walks_off_loop.py` (10 tests, 9 fail on v0.53).

## v0.53: Round 60: checkpoint saves and admin CodeX views off the event loop. 5,320 tests + 19 integration

- **Checkpoint saves blocked the loop.** `Checkpoint.put` re-read, re-parsed and rewrote the whole file, with an fsync, for every finished worker, the plan, every deep round and the result. At 18 MB that was ~81 ms per put, and the cap is 64 MB. Trinity now saves through `asyncio.to_thread`. `_Store` also skips the re-read when the file still has the (inode, size, mtime_ns) of its own last write: ~48 ms, all off the loop. Another writer changes the signature, so cross-process merging still works.
- **priv_admin `codex_runs` / `codex_tail`** read every run log, or streamed a whole log, on the loop. They now use `to_thread`, like their `/api` twins.
- Found with an AST sweep for sync store calls in async functions outside `to_thread`.
- Tests: `tests/test_round60_checkpoint_admin_off_loop.py` (10 tests, 5 fail on v0.52).

## v0.52: Round 59: learning bridge off the event loop. 5,310 tests + 19 integration

- **Every squad and deep run froze the node for the memory parse.** With asyncio's slow-callback detector on and a 200k-line memory store, each squad and each deep wave logged a ~1.0 s blocked step. Profiling put all of it in `Trinity._lessons_block` (bridge `lessons_block` loads and folds the whole store to find the lessons). Lessons are on by default, so every run stalled the API, WebSockets and fleet heartbeats. The end-of-run `_distill` save ran on the loop too. Both now run via `asyncio.to_thread`; the same workload logs no slow callbacks at 0.1 s.
- Probed clean: all-tools fuzz seeds 20-23, governor parsers against junk cgroup, meminfo, vm_stat and pmset text.
- Tests: `tests/test_round59_lessons_off_loop.py` (4 tests, 3 fail on v0.51).

## v0.51: Round 58: admin endpoints and agent-pack tools off the event loop. 5,306 tests + 19 integration

- **Live-node stall probe** (uvicorn, `/api/status` pinged every 20 ms during each call):
  - `/api/capgate` walked and re-MACed the whole receipt chain inline. With 40k receipts, the node froze for 0.85 s, growing with the chain.
  - `/api/map` parsed every run log inline. With 360k lines (json or mermaid), the node froze for 1.3 s. `/api/telemetry` had already moved to a thread.
  - `/api/checkpoints` and `/api/evolve` read every file inline.
  - All four now run in `asyncio.to_thread`. Worst `/api/status` latency during each call: 0.01 to 0.04 s.
- **Agent packs:** `/api/fleet/agentpack` decoded, gunzipped, verified and installed a 50 MB pack inline (~0.7 s). It now runs in a thread; the install is still an atomic swap under the per-agent lock. The `agent_pack_export` and `agent_pack_import` tools (1.6 s and 0.6 s on the loop) are `off_loop`. Fleet migration keeps its synchronous export on purpose, so no await sits between the busy re-check and the snapshot.
- **Probed clean:** telemetry and map with `hours` set to nan, inf, -1, 1e308 or 0. All-tools fuzz seeds 16 to 19.
- Tests: `tests/test_round58_api_and_pack_loop_stalls.py` (12 tests).

## v0.50: Round 57: MCP HTTP credentials on cross-origin redirects, evolve plain-copy clones. 5,294 tests + 19 integration

- **MCP HTTP redirect leak:** `HTTPTransport` followed redirects with every configured header. httpx drops `Authorization` on a cross-origin hop, but an `X-Api-Key` (any custom credential header) and the `Mcp-Session-Id` went to whatever host the server's `Location` named. A request hook now strips configured headers, the session id, cookies and auth headers whenever a hop leaves the configured origin (scheme, host, port; an https to http downgrade counts). Same-origin redirects (`/mcp` to `/mcp/`) keep them.
- **Evolve plain-copy clone:** a node deployed from a release zip is not a git checkout, so every proposal clone uses the copytree fallback. With `data_dir` inside the repo under any name but `data`, the copy recursed into its own destination until ENAMETOOLONG and left the half-made tree behind. It also copied every agent's workspace and ran on the event loop. The copy now skips the data dir, evolve dir and agent workspace, skips FIFOs, sockets and devices, runs in a worker thread, and removes a failed clone.
- **Probed clean:** a raw-socket MCP server that drips one byte per 0.5 s, sends endless SSE keep-alives, a 2 GB gzip bomb (refused at the 32 MB cap), a malformed chunk header, a 60 MB SSE line with no newline, or never answers. Each ends within the tool timeout. All-tools fuzz seeds 14 and 15: clean.
- Tests: `tests/test_round57_mcp_http_redirect_edges.py` (11 tests).

## v0.49: Round 56: hostile stdio MCP servers, one-rewrite fleet memory merge, memory tools off the loop. 5,283 tests + 19 integration

10 new tests. 9 of them fail on v0.48. Two of those call the new `merge_many`, so 7 confirm 5 bugs on behavior alone. I wrote fake MCP servers that stop reading their input, flood stderr, or die with a 12 MB stderr line, and timed memory and fleet memory paths against stores of 120k to 200k lines.

| Lane | Finding | Mapping |
|---|---|---|
| MCP stdio send | `stdin.drain()` had no deadline. A server that stopped reading its input (2 MB tool args) hung the call **forever**, past the request timeout. It also held the write lock, so every later call to that server hung too | the send counts against the request timeout. A half-written line drops the connection (the server is killed), and a call queued behind a stuck write fails with nothing sent. `SEND_TIMEOUT_S` (30 s) covers notifications |
| MCP stdio stderr | stderr was read line by line. The first line over the stream limit raised inside a `suppress()`, and the drain stopped for good. The pipe then filled and the server blocked on its next stderr write | stderr is read in 64 KB chunks, and kept lines are clipped to `STDERR_LINE_CHARS` (2000) |
| MCP exit error | the "process exited" error quoted the last stderr line whole (12 MB) | clipped to 300 chars; a request after the server closed stdout fails at once |
| fleet memory merge | `receive_memory` merged one namespace at a time. Each pass re-read and rewrote the whole store: 32 namespaces over 200k lines took **24 s**, on the event loop | `MemoryStore.merge_many` does one read and one rewrite (0.76 s), and `/api/fleet/memory` runs it via `asyncio.to_thread` |
| memory tools | `memory_save` and `lesson_forget` read the whole store and took its file lock on the loop. `memory_sync` read it on the loop too (a 0.85 s stall) | the first two are `off_loop`; `memory_sync` reads through `to_thread` (0.13 s) |

Memory sync was also fuzzed with junk records (string, list, bool, inf and missing `ts`; NaN values; list keys; foreign namespaces; a 10 MB key). It handled every case cleanly.

## v0.48: Round 55: quadratic diff, osint rollup stall, one serialization per event. 5,273 tests + 19 integration

9 new tests. 7 of them fail on v0.47. Four of those call names added this round (`DIFF_INLINE_CHARS`, `codex.wire_text`), so 3 tests confirm 2 bugs on behavior alone. The third fix comes from a measurement: event fan-out across 32 subscribers dropped from 1.3 s to 0. A static AST scan listed every `async def` tool or endpoint that reads files without ever awaiting, and each one was timed against large state (20k-line diffs, 300k CodeX events, 3,600-file trees, 150k lesson lines, a 50 MB file, a 20 MB write).

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| diff_files | **difflib is quadratic** on many similar short lines: 20k lines took 5.4 s and 40k took 21 s. The 1 MB input cap allows about 200k lines, which means minutes. It ran on the event loop and could not be interrupted | inputs over `DIFF_INLINE_CHARS` (64k) are diffed in a child process (`searchproc.diff_main`) with a `DIFF_TIMEOUT_S` (20 s) deadline. On timeout it returns an ERROR that suggests diffing smaller pieces | the loop gap stays under 0.4 s on a 14k-line diff; the child's output is identical to the inline diff |
| osint_metrics | scanned every run log on the loop (300k events: a 0.72 s stall) | `off_loop` | |
| event fan-out | every WebSocket pump re-ran `asdict()` + `dumps` on its own copy of each event: a 4 MB event cost about 1.3 s of loop time across 32 HUD subscribers | `emit()` serializes once and caches the text; `codex.wire_text()` returns it (the stream.gap notice still serializes itself) | one `dumps_event` call for 20 subscribers; the wire text matches the log line |

Probed live under uvicorn with no change needed: 10 MB binary, 15 MB text and 100k-`{` frames, a 20k-frame flood, 80 connections against the 64-subscriber cap (17 refused), and never-reading clients during a run. There were no tracebacks, `/api/status` stayed at about 1 ms, and slots recovered. The all-tools fuzz ran on 2 more seeds and came back clean.

## v0.47: Round 54: event-loop stalls, docker bind paths with ':', and the tool wrapper. 5,264 tests + 19 integration

21 new tests, all of which failed on v0.46. Thirteen of those call a name added this round (`off_loop`, `bind_args`), so 8 tests confirm 6 distinct bugs. This round went looking for more bugs in the same class as round 53's ReDoS, where one call freezes the whole node. A stall probe ran every tool against 200k memory lines, 200k CodeX events and 3,000 workspace files while a 20 ms ticker measured the longest event-loop gap. A multi-process memory race (8 processes, saves, loads and trims on one store) found no torn lines and no lost keys.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| event loop | `memory_recall`, `memory_list`, `memory_peek`, `lesson_recall` and `codex_search` parsed whole logs on the event loop. With 200k memory lines, `memory_recall` held it for **1.09 s per call**, pausing every agent, the API, WebSockets and fleet heartbeats | `tools.off_loop` runs an await-free tool in a worker thread (it refuses one that awaits). The worst gap dropped to **~47 ms** | a ticker keeps running during a 200k-line recall |
| HTTP | `/api/telemetry`, `/api/runs` and `/api/runs/{id}/bridge` read run logs on the loop | `asyncio.to_thread` | each reader runs outside the loop |
| docker | `-v SRC:DST` splits on `:`. A workspace like `/data/proj:v2` became source `/data/proj` with an invalid mode, so the container never started | `agentfs.bind_args`: `--mount type=bind,"source=…",target=…` (CSV-quoted) for `:` or `,`; newline, NUL or `"` refused; relative paths made absolute; used by `DockerRunner.start`, `docker_run` and the per-agent `/agent` volume | parsed back with the csv reader docker uses |
| tool wrapper | a tool calling `sys.exit()` raised **SystemExit** through the agent loop, which can stop the node's event loop | `ToolRegistry.call` catches `SystemExit` with capgate both off and on | |
| plugins | a plugin's plain `def` tool failed every call ("can't be used in 'await' expression") | `_invoke` runs sync tools in a worker thread | |
| tool wrapper | an output or exception whose `__str__` raises escaped the wrapper | `_safe_str` gives `<TypeName>` | |

## v0.46: Round 53: catastrophic regexes, MCP wire encoding, fallback specs, python/evolve/git inputs. 5,243 tests + 19 integration

15 new tests. 13 failed on v0.45; 3 of those fail only on a name or limit added this round (`wire_safe`, the 2000-char pattern cap), so 10 failing tests confirm 6 distinct bugs. The all-tools fuzz from round 52 was rerun with nested (depth 150), wide (5000 keys, 50,000 items) and 1 MB values over every tool, fleet tools included, for 4 more seeds; the process-spawning tools were probed one hostile field at a time.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| search_files | **one regex froze the whole node.** Python `re` holds the GIL and cannot be interrupted: `(a+)+b` against a line of 28 a's blocked the event loop for ~17 s (32 a's: minutes), stalling every agent, the HTTP API and fleet heartbeats | the scan runs in a child process (`avrone/searchproc.py`) with a deadline (`SEARCH_TIMEOUT_S` 20); hits stream back, so partial results survive a timeout; the child is killed; patterns over 2000 chars are refused | a 50 ms ticker keeps ticking during a ReDoS search; timeout keeps earlier hits |
| MCP | **NaN, inf or a lone surrogate in a model's tool arguments** (Python's `json.loads` accepts all three) raised raw `ValueError`/`UnicodeEncodeError` from httpx over HTTP, and over stdio sent a bare `NaN` token, which is invalid JSON-RPC | `wire_safe()` on `tools/call` arguments: NaN/inf become null, surrogates become U+FFFD, tuples/sets become lists | strict stdio server (rejects NaN) echoes nulls |
| gateway | `AVRONE_LLM_FALLBACKS` turned model ids with NUL, newlines or 5000 chars into upstreams that failed every call, repeated a duplicated spec, and had no cap | model and lead ids must be 1 to 200 visible ASCII chars; duplicates dropped; at most `MAX_FALLBACKS` (16) | |
| python | a surrogate in `code` raised after the snippet file was created, outside the cleanup, so the file leaked; the snippet was written in the locale encoding | `write_content` + UTF-8 write | no `snippet_*.py` left; `café ✓` prints |
| evolve | `edits=[1]` or an edit with no path/content was found only after cloning the repo (~16 s, an `evolve/<pid>` branch left behind) | `_edits_shape_error` runs before the clone | the clone is never called |
| git | a 1 MB `args` string was split and handed to git (~10 s) | `MAX_GIT_ARGS_CHARS` 16,000 | |

Also checked, no change needed: `agentfs.agent_dir` with a file where the dir should be is caught by the caller, and MCP config with junk `args`/`env` fails only that server.

## v0.45: Round 52: every tool under hostile args, peer presence rendering, paths, timestamps. 5,228 tests + 19 integration

29 new tests. 24 failed on v0.44; 1 of those failed only because it uses a name added in this round (`check_path_text`), so 23 bugs are confirmed. A seeded fuzz now drives every registered tool that does not spawn a process or reach the network (`FUZZ_SEED`, `FUZZ_ITERS`), and none may answer with a raw Python exception. Separately, every HTTP endpoint was hit with junk query params and bodies, and none returned a 5xx; no code changed for that.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| presence | **peer names could forge directory rows.** `fleet_agents` printed peer-supplied agent names, roles, tasks, hostnames and service values raw, so a name with a newline added fake `[node]` rows the model reads as real, and a 5 MB `tools` value was rendered whole. `render()` also raised KeyError on rows missing `route`/`env` | each field is one line (control chars, U+2028/9 stripped) and clipped; partial rows render | forged-newline snapshot gives one `[`-row and output under 5 KB |
| presence | `find_agent("edge/")` and `"/"` resolved to an agent named `""` | an empty name is a `LookupError` | |
| file tools | a lone surrogate or a component over 255 bytes surfaced as raw `UnicodeEncodeError` / `OSError: [Errno 36]` from `read_file`, `write_file`, `edit_file`, `artifact_save` | `check_path_text()` in `_safe_path` and `fsunify.resolve` refuses with a plain message (`MAX_NAME_BYTES` 255) | |
| artifact_save | surrogate content raised after open(), leaving an empty artifact; dict content was saved as a Python repr; locale encoding | `write_content` + atomic UTF-8 write, like `write_file` | |
| bridge | `bridge_reply`/`bridge_post` with `refs` 1.5, 7 or true raised `TypeError` | non-list refs are treated as one ref (sets still work), so the reply gets "refs is required" | |
| research_note | a surrogate in the text raised in `lesson_key` *after* the post was made, so the agent saw ERROR, retried, and posted twice | surrogates become U+FFFD before posting; `lesson_key` hashes with surrogatepass | one post, lesson learned |
| memory / codex | `memory_peek` and `codex_stats` crashed on a record with `ts` 1e308 (year > 9999) or a 400-digit int; `memstore._ts` raised `OverflowError` on the int | read as `?` / 0 | |
| codex_search | log lines printed `kind`, `agent` and `tool` whole: a 1 MB tool name or a newline in an agent name came out raw | one line, clipped to 80/120 chars | |
| great_scale | `h` or `alpha` as a 400-digit int raised raw `OverflowError` | plain range error | |
| telemetry / fleet | **a 400-digit integer `ts` in a run log 500'd `/api/telemetry`** (`json.loads` accepts it; `float()` raises). The same `float()` sat on peer data: heartbeat health (`sanitize_health`, migration's `h()`), relayed bridge posts, `fleet_status`/`router_status`, and checkpoint `updated` | each reads as default/None (health clamps to its range) | a log with a 400-digit ts gives 200 |

Contract changes: an `artifact_save` dict/list `content` is now written as JSON (it was a Python repr). A path with a component over 255 bytes or a lone surrogate is refused before any filesystem call.

## v0.44: Round 51: agent home containers, osint_domain sections, MCP streams and config. 5,199 tests + 19 integration

31 new tests. 25 failed on v0.43; 4 of those failed only because they use names added in this round (`MAX_GUARD_SECS`, `parse_inspect`), so 21 bugs are confirmed. On v0.43 the 500-server config test really spawned 500 processes.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| agentcontainer | **another workspace's container could be adopted or deleted.** Two AVRONE instances on one host with the same node id and agent role compute the same name `avrone-agent-<node>-<role>`. A running container from the other workspace was adopted, so this agent ran with *their* `/agent` mounted. A stopped one was `rm -f`'d | `docker inspect` now also reports the host dir mounted at `/agent`; a different dir raises `ForeignContainer` (no adopt, no rm) and points at `AVRONE_NODE_ID`. Older output with no mount is accepted as before | running and stopped foreign containers: only `inspect` runs; own container (running and stopped) behaves as before |
| agentcontainer | `launcher(timeout=inf / -inf / 10**400)` raised `OverflowError` | guard capped at 7 days (`MAX_GUARD_SECS`) | 7 junk timeouts |
| agentcontainer | explicit mem/cpu values of NaN, 0, -1, inf or junk reached docker verbatim (`--cpus nan`; `--memory 0m` means unlimited); 10**400 raised | invalid values use the configured default; valid ones are clamped to MIN..MAX | 6 pairs |
| agentcontainer | `idle=NaN` never reaped (every comparison False); negative reaped on every call | junk falls back to `AVRONE_AGENT_IDLE_SECS` | |
| osint | **a raising sub-probe sank the whole `osint_domain` profile** (the docstring promised the opposite); a `None` CT result crashed on `.splitlines()`; a 5 MB section went out whole | each section is caught as `ERROR: ...`, coerced to text and clipped at 20,000 chars | raising, None and 5 MB probes give one bounded profile |
| osint | `osint_metrics` listed every distinct kind (5,000 junk kinds made a 5,000-line report) | top 30 kinds and refused tools, plus "... N more" | 5,000 kinds: under 60 lines |
| mcp_client | **SSE lines were buffered without a cap.** httpx `aiter_lines` waits for a newline, and only `data:` bytes counted toward `AVRONE_MCP_MAX_RESPONSE_BYTES`, so 2.5 MB with no newline, or a huge `event:` or comment line, was held whole | own LF/CRLF/CR line splitter capped at the response limit (a CR at a chunk edge waits for its LF) | 3 hostile streams refused; 3 line-ending styles split mid-CRLF still parse |
| mcp_client | `mcp.json` as a FIFO hung startup; a 50 MB or 100k-deep config was read and parsed whole; 10,000 servers were all spawned at once | regular file, at most 1 MB, deep nesting refused, at most 64 servers (`MAX_SERVERS`); the manager reports `_config` instead of spawning | FIFO returns in under 5 s; 500-server config spawns nothing |

## v0.43: Round 50: hostile model providers, codex listing/replay, CLI arguments. 5,168 tests + 19 integration

17 of the 28 new tests failed on v0.42 before the fixes. The 6,000-body seeded provider fuzz passed on v0.42 too: the gateway's ordinary junk handling held, and the failures were all in depth, size and argument edges.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| gateway | **a 200 body nested 200k deep (plain JSON, SSE, or inside tool-call `arguments`) escaped `chat()` as a raw `RecursionError`**, not `LLMError`, so callers expecting LLM failures crashed | caught as a permanent failure for that upstream (fails over) | 4 nested bodies, each an `LLMError` |
| gateway | **response bodies were buffered whole**: a 300 MB gzip stream (a few hundred KB on the wire) was inflated in memory, and a 200 MB `content` string became the agent's reply | streamed read capped at `MAX_RESPONSE_BYTES = 32 MB` (decoded); over the cap fails over; `Retry-After` and status still read | 3 MB body and 20 MB gzip bomb refused at a 1 MB cap; a small gzip reply still works; 429 then 200 still retries |
| gateway | `temperature=10**400` raised `OverflowError` before the request; `True` was sent as 1.0 | junk temperature uses the 0.7 default | 7 junk temperatures |
| gateway | `clean_text(None/int/list)` raised `TypeError` | non-text read as `str()` | |
| codex | **a FIFO named `<run>.jsonl` in `runs/` hung `runs()` and `replay()`** (and `/api/runs`) in `open()` | regular files only | both return within 5 s |
| codex | `runs()` returned 4 MB prompts (a 50-run listing was a 200 MB response) and non-string prompts; `runs("abc")`, `replay(limit=inf)` raised | prompts clipped to 2,000 chars and coerced to text; limits read as whole numbers 1..cap | 7 junk limits |
| cli | `--max-steps -5` gave every agent zero turns (silent empty answers); `10**9` meant no cap | clamped to 1..200 (`MAX_CLI_STEPS`) | |
| cli | `evolve --json PATH` treated PATH as a flag: printed JSON, never wrote the file | writes PATH like every other command | file written, plain listing printed |
| cli | `--first-answer-file` on a FIFO or `/dev/stdin` hung; a 2 GB file was read whole | regular file, at most 1 MB | FIFO, dir, oversize refused |
| evolve | accepted proposals with `files` as a string or holding `null` crashed the CLI listing; `passed: "yes"` counted as accepted | `files` coerced to strings; only `passed: true` counts | |

## v0.42: Round 49: image tools, skills loader, shell runner timeouts, evolve listings. 5,140 tests + 19 integration

54 of the 69 new tests failed on v0.41 before the fixes (52 failed outright, the 2 FIFO cases hung).

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| agentkit images | **`image_inspect` / `image_decode` on a FIFO (or `/dev/stdin`) hung the event loop inside `Image.open`**, stalling every agent and the API | regular files only, checked before opening | FIFO path returns an error in under 10 s |
| agentkit images | non-image, empty, truncated, directory, missing, escaping and NUL paths raised raw exceptions; a 48 KB PNG declaring 400 Mpx raised `DecompressionBombError`; one just under PIL's limit (~179 Mpx) was decoded at ~0.5 GB per copy, and the 2x decode pass at 4x that | `MAX_IMAGE_PIXELS = 40,000,000`, `MAX_IMAGE_BYTES = 64 MB`, the 2x pass only under 4 Mpx; every failure is an `ERROR:` result | 13 bad paths x 2 tools |
| agentkit images | decoding ran on the event loop; a one-colour image printed `entropy=-0.000` | work moved to `asyncio.to_thread`; entropy prints `0.000` | event loop keeps ticking during a slow inspect |
| skills | a skill file that grew after `stat()` was read whole; 100k skill files were read and listed on every call | bounded read (`SKILL_MAX_BYTES + 1`); `MAX_SKILLS = 500`; "Available:" list clipped to 50 names | grow-after-stat, 800 skills |
| runner | **`timeout=10**400` or `"5"` raised after the child was spawned, leaking the process and its pipe readers**; `inf` never timed out; NaN, negative and tiny values killed the command at once and called it a TIMEOUT | `_norm_timeout`: bad values fall back to the node default (or 120 s), capped at 24 h, integral values kept as ints | 11 values normalised, 7 junk timeouts still run `echo hi` |
| runner | `CommandResult.text(-10)` returned the whole text uncut; `exit_label(None)` raised | limit clamped at 0; a non-int exit code reads as -1 | |
| evolve | **one hostile file broke `evolve_list` / `evolve_status` for every agent**: deeply nested JSON (RecursionError), `iterations` as a dict or holding ints, a non-string `outcome`, `accepted` as a string, a str `ts` in the sort, a 10^30 `owner_pid` (OverflowError in `os.kill`), and an `UnboundLocalError` in the history writer's error path | loop files cleaned on read (`_clean_loop`); `loop_id` must be a string; `_pid_alive` rejects pids <= 0 and huge values | 8 hostile loop and meta files |
| evolve | `loop_limits(10**400)` raised; a proposal could carry unlimited edits | overflow clamps to the cap; `MAX_EDITS = 200`, `MAX_EDITS_BYTES = 16 MB`, checked before cloning | 3 oversized proposals refused without a clone |

## v0.41: Round 48: memory store, OSINT rollup, presence tree from peers, agent kit. 5,071 tests + 19 integration

25 of the 30 new tests failed on v0.40 before the fixes.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| agentkit | **`env_install` built its lock file name from `manager` before checking it**: `manager="x/../../victim"` opened `<data>/.install-x/../../victim.lock` with mode `w` and truncated a file outside the data dir (confirmed on v0.40); a missing parent dir crashed the tool instead | `manager` must be `pip` or `apt` before any file is opened | 7 hostile managers: victim intact, no runner command, no lock file |
| agentkit | **one torn or non-object line in `resources.jsonl` broke `env_install` and `resources` for good**; `resources` `limit` of `"abc"` raised, `0` listed every line, negatives sliced from the front | non-object lines skipped; limit clamped 1..500; missing/junk `ts`/`agent` rendered as `?` | junk ledger + 8 junk limits |
| agentkit | task board grew without limit (every op rewrites and lists all of it) | `MAX_BOARD_TASKS = 500` | 501st add refused |
| memstore | **a lone surrogate raised `UnicodeEncodeError` past every `except MemoryRefused`** (memory tools, research notes) | refused as `MemoryRefused` (odd keys like `k\x00ey` still round-trip, v0.20 contract) | 4 bad key/value pairs |
| memstore | hand-edited lines with dict/list/bool keys or null/object values were served as is (tools slicing values raised) | read with the same rules as `merge()` (number values kept as text) | junk lines skipped, the store still saves |
| memstore | `trim("abc" / NaN / inf / None)` raised a raw error | `MemoryRefused("keep must be a whole number")` | 6 junk keeps |
| osint | `osint_metrics` read every run log whole (a multi-GB log meant multi-GB RAM) and echoed 500 KB hosts into the report | logs streamed line by line, lines over 1 MB skipped, hosts clipped to 253 chars, tool names to 80 | hostile log (3 MB line, 500 KB host, deep brackets, torn tail, dir, dangling symlink): report under 4 KB |
| presence | **a peer's presence could break `/api/fleet/directory`, `fleet_agents` and agent lookup for the whole fleet**: `children` nested 500 deep (RecursionError), `children` as a list, `agents` without names (KeyError), junk `env` | rows cleaned; depth capped at `MAX_TREE_DEPTH = 16`, tree at `MAX_ROWS = 2000`; child ids with `/` or repeating the route skipped | 5 hostile trees: render, lookup and JSON all work |
| fuzz | — | 4 forked processes x 60 memory saves keep all 240 keys; 450 seeded merge rounds with junk keys/values/ts and journal garbage keep every value a string and every namespace under its cap | `tests/test_round48_mem_osint_presence_kit_edges.py` |

## v0.40: Round 47: recon target smuggling, hostile MCP servers, governor/telemetry and fleet strict JSON. 5,041 tests + 5 integration

29 of the 54 new tests failed on v0.39 before the fixes.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| recon | **private addresses hidden in names passed the up-front guard**: compressed IPv6 on sslip.io/nip.io (`0--1` = `::1`, `fe80--1`, `fc00--1`), dotted quads glued to text (`customer-10.0.0.1.nip.io`, `make-127.0.0.1-rr.1u.ms`, `a.10.0.0.1-x.example.com`), and the DNS-rebinding services `rbndr.us` / `1u.ms`. (Connecting tools still re-check the resolved address; this closes it before any DNS) | `_GLUED_QUAD` scan on every name; IPv6 dash labels parsed on IP-in-name services; `rbndr.us` and `1u.ms` added | 16 smuggled names x 5 forms (case, trailing dot, scheme, path, port) refused; 11 ordinary public names (`2a01-4f8-c17-b8f--2.sslip.io`, `dead--beef.example.com`, IDN) still pass; 2,400 seeded encodings (full-width, ideographic dots, zero-width, soft hyphen) of 22 private bases never pass; 3,000 junk strings never raise |
| mcp | **a server paging tools without end put up to 100 x 32 MB of tools into the registry** (and every agent's tool docs) | `MAX_TOOLS_PER_SERVER = 512`, pagination stops there | endless-pages fake server: 512 tools, 2 requests |
| mcp | **a schema with an unhashable `type` (`{"x":1}`, `[{"x":1}]`) raised and dropped the tool**; 100k properties were all rendered before the doc was cut to 300 chars | non-string types read as `any`; rendering stops at the limit; keys capped at 80 chars | 13 hostile schemas: every doc is at most 300 chars, every tool still registers |
| governor | **a +-inf sensor reading made `/api/telemetry` answer 500** (strict JSON has no Infinity); `Governor(thermal_max_c=10**400)` raised OverflowError | `NodeHealth.as_dict` shows non-finite readings as null (the throttle still treats inf as the extreme); `_finite` catches OverflowError; telemetry passed through `jsonable` | inf/-inf/1e308/NaN sensors x 5 `hours` values: 200 and strict JSON; 900 seeded junk governors never raise |
| fleet | **a peer registering `Infinity`/`NaN`/bytes/sets in capabilities or presence was stored as is, and every later `GET /api/fleet` answered 500** | node record passed through `jsonable` before the size check and save (unencodable keys are still refused, v0.32 contract); `/api/fleet` also sanitizes records saved by older versions | registry reloads as strict JSON |

## v0.39: Round 46: live WebSocket feed, bridge journal across processes, hostile plugins, non-ASCII tokens. 4,987 tests + 5 integration

Every fix below was confirmed by running the new tests against v0.38 first, then fixing.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| server | **a non-ASCII admin token (latin-1 header byte, `%ff`, `é`) answered 500, not 401, on every admin route, and crashed the `/ws/events` handler**: `hmac.compare_digest` raises TypeError on non-ASCII str | `token_eq()` compares UTF-8 bytes, constant time, never raises | 401 for 3 non-ASCII tokens; WS refused before accept |
| capgate | the same TypeError on a non-ASCII `sig` in a hand-edited policy or receipt file | byte comparison in `check_sig` and `verify_chain` | refused as "bad signature" |
| bridge | **a reply to a post made by ANOTHER process was refused** ("unknown post id"): each process's ring only learned sibling posts at compaction | on unknown refs, re-read the journal under the file lock and recheck; a truly unknown id is still refused | cross-process reply test |
| bridge | compaction renamed an un-fsynced temp file over the journal (power cut could leave it empty) | `fsync` before `os.replace` | 4 forked processes x 150 posts through compaction: all 600 kept |
| plugins | **`sys.exit()` at import or inside `register()` killed the node** (`SystemExit` is not an `Exception`) | caught and reported like any other plugin failure | exit-at-import and exit-in-register reports; good plugins still load |
| plugins | **a plugin could silently replace an existing tool** (`shell`, `read_file`, another plugin's): the replacement kept `source="builtin"`, so `/api/tools`, capgate receipts and the codex never showed third-party code ran | originals restored, report says `refused to replace existing tool(s): ...`; new tools still added and tagged `plugin:<name>`; partial tools before a `register()` error are still kept (existing contract) | hijack and plugin-vs-plugin tests |
| fuzz | — | WS: binary, 500 KB, 50 junk frames per client x 5 clients, the stream still delivers and every subscriber slot is freed; hostile event payloads arrive as strict JSON. Bridge: torn/junk journal lines, 600 seeded hostile remote posts (NaN/inf ts, 50 KB text, cycles, overwrite attempts). Plugins: binary, syntax error, no/uncallable `register`, infinite recursion, package raising on import | `tests/test_round46_ws_bridge_plugin_edges.py` |

## v0.38: Round 45: job queue, CodeX log and HTTP API under crashes, races and junk. 4,971 tests + 5 integration

Every fix below was confirmed by running the new tests against v0.37 first (22 failed), then fixing.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| codex | **a crash mid-write cost the NEXT event too**: the following emit appended straight onto the torn bytes, making one corrupt line out of two | emit opens `ab+`, checks the last byte with `pread`, and starts on a fresh line when it is torn; one `write()` per event | torn-tail restart keeps the next events; 4 processes x 200 KB events never interleave |
| jobqueue | **`enqueue(priority=10**30)` raised OverflowError** (SQLite INTEGER is 64-bit) | priority clamped to ±2^62 | 54 hostile priority x max_attempts combinations |
| jobqueue | **`extend(seconds=inf / "1e400" / 1e300)` wrote an infinite lease**: if that worker then died, the job could never be reaped | non-finite or junk means one normal lease; capped at 7 days (`EXTEND_MAX_S`) | 8 hostile values |
| server | **a malformed body answered 500, not 422**: FastAPI echoes the input back, and `1e999` (inf), `NaN` or a lone surrogate made that echo fail encoding on `/api/chat`, `/api/fleet/tell`, `/api/terminal` | `RequestValidationError` handler: strict, ASCII-escaped JSON via `codex.jsonable` | 19 hostile bodies x 3 header sets x 8 POST endpoints: never 500 |
| server | **`/api/terminal` with a lone surrogate in `cmd` answered 500** (the runner could not build the command line) | 422 before the runner is touched (a NUL keeps the v0.26 contract: exit -1 result; v0.39 restored it) | strict-JSON 422 tests |
| fuzz | — | 4 processes drain 150 jobs from one SQLite file with no job run twice; expired-lease owner fenced from complete/fail/extend/release; poison job goes dead; corrupt rows never kill `claim()`; `replay()`/`runs()` survive binary, 3 MB, deeply nested, directory and symlink logs; `emit()` with NaN, cycles, a `repr` bomb, surrogates always writes one JSON line | `tests/test_round45_queue_codex_edges.py` (86 tests) |

## v0.37: Round 44: filesystem escapes, hostile agent packs, several processes on one data dir. 4,885 tests + 5 integration

All six process bugs below were confirmed by running the new tests against v0.36 first (6 failed), then fixed.

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| fleet | **a captured signed request replayed to a SECOND worker process ran twice**: each process kept its own replay cache, and each save overwrote the other's nonces | nonce check + insert under `flock` on `fleet_nonces.json.lock`, folding in nonces other processes saved (re-read skipped while the file's mtime/size is unchanged) | worker A accepts, worker B refuses; 6 forked processes race one request x5 rounds: accepted exactly once each round; a third process started later refuses all 40 |
| checkpoint | **two processes writing one checkpoint erased each other's steps** (whole-file last-writer-wins) | save re-reads the file under `flock` and applies only this process's written/deleted keys; a cleared scope is never resurrected | 3 forked writers x 40 steps: all 120 kept |
| checkpoint | **`claim()` only saw jobs in its own process**, so two processes could resume one checkpoint at once | non-blocking `flock` on `<ckpt>.claim`, held for the run; the kernel frees it if the process dies | conflict across processes; free after release and after a hard crash (`os._exit`) |
| paths | — | `agentfs.confine` and `tools._safe_path` vs seeded hostile paths (`..` variants, `~`, drive letters, UNC, NUL, full-width dots, RTL marks, lone surrogates) and real symlinks (dir link out, link to `/`, relative file link, self loop) | never resolves outside the root |
| agentpack | — | 26 booby-trapped bundles (symlink, hardlink, device, FIFO, dir members, traversal, absolute, backslash, drive, control chars, case and NFC collisions, file-vs-dir, duplicate manifest, unlisted file, wrong hash, setuid mode, bad agent id/origin, memory tamper, not gzip, empty, truncated) + a 64 MB gzip bomb (refused in under 5 s, never inflated) + random byte flips | only `PackError`, never anything else |

## v0.36: Round 43: crash, multi-process and hostile-peer edges. 4,843 tests + 5 integration

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| capgate | **two processes sharing a data dir forked the receipt chain** (thread lock only), which `verify_chain` then reported as tampering | `flock` on `receipts.jsonl.lock` across threads and processes | 4 forked processes x 60 receipts: one linked chain of 240 |
| capgate | **a crash mid-append broke the chain forever**: the next receipt glued onto the torn bytes | torn tail moved to `receipts.jsonl.torn` (kept as evidence) and cut before the next append | torn-tail and torn-only-file tests |
| capgate | deleting the LAST receipts was invisible (what remains still links) | signed head pointer `receipts.jsonl.head` (count + newest hash); verify reports `truncated`, `head mismatch (chain replaced)`, or an unsigned/forged head; a lost head is recounted | 5 tests |
| gateway | **a `tool_call_id` with a lone surrogate crashed the request locally**; one like `"\x00\n"` passed the empty check and went out as `"\n"` | id cleaned (surrogates, NUL), capped at 256 and stripped before the emptiness check | seeded `sanitize_messages` contract fuzz (found by the 6,000-iteration soak) |
| fuzz | — | `test_round43_crash_peer_edges.py`: memstore merge vs hostile peer payloads (stats add up, caps hold, old never beats newest), `extract_choice` raises only what `Gateway.chat` catches, Retry-After/temperature never NaN or negative, 300-line junk stream into the MCP server (strict JSON-RPC replies, still answers the last line) | 20 tests; `FUZZ_ITERS=6000` soak green |

## v0.35: Round 41-42: seeded fuzz + soak, capgate (capability-scoped tools + receipt chain), Avrone bridge merge. 4,823 tests + 5 integration

| Lane | Finding | Mapping | Verification |
|---|---|---|---|
| merge | daemon lacked 9 Avrone commits (Grok Bot bridge contract, bridge chaos + multiprocess tests, cross-process journal compaction lock) | merged `--no-ff`, no conflicts, nothing overwritten | `test_bridge_chaos.py`, `scripts/verify-grok-bridge.sh` |
| fuzz | example tests can't show the space between examples | `test_round41_fuzz_soak.py`: seeded hostile generators (lone surrogates, NUL, think/harmony wrappers, truncated JSON, 5k-deep nesting, 2 MB finals) against parse/normalize/reply_text/subtasks/clip/compact, fleet HMAC (valid-once, any single tamper refused, junk headers only ever `GatewayRefused`), 4-thread memstore soak + torn tail, checkpoint reload soak. `FUZZ_ITERS` (default 400) scales it | 6,000-iteration soak: 26/26 green after one test-side fix (1-char clip limit keeps tail only) |
| capgate | every agent held ambient power over every tool | `avrone/capgate.py` (`AVRONE_CAPGATE=off|audit|enforce`, default off): signed expiring capabilities from `AVRONE_CAPGATE_POLICY`, ctxspawn children get an **attenuation** (no amplification, depth cap 16), hash-chained HMAC-signed receipts (digests only), `GET /api/capgate` verifies the chain | `test_capgate.py` (24): forged/edited/expired/wrong-audience caps, malformed policy fails closed, chain detects edit/delete/reorder/forge/torn line, 6-thread writers stay linked |
| docs | systems evolved separately | `docs/EVOLUTION.md`: what each repo is, what is already merged, ranked merge plan | — |

Full suite also passes under `AVRONE_CAPGATE=audit` except one expected difference: `self_scan`'s
"read-only" check sees the audit receipt file the gate writes.

## v0.34: Squad round 20: run-log reads streamed and bounded. 2908 tests, coverage 99%

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | **one run view could take the node down**: `Codex.replay` read and parsed the whole log on the event loop and returned every event | streamed replay keeping the newest `AVRONE_REPLAY_MAX_EVENTS` (2000, hard 50k); lines over 4 MB skipped unread (`iter_lines`); `/api/runs/{id}` runs off the loop and sets `X-Avrone-Truncated` | `test_robust_v034_replay.py`; live 200 MB log: **7.6 s, every other request stalled 7.1 s, 212 MB body, 1.37 GB RSS** before; **1.2 s, 0.01 s, 1 MB, 46 MB** now |
| robust | `codex_search` loaded newline-less logs whole and let a deeply nested line raise | line-bounded reads, RecursionError skipped | `test_robust_v034_search.py`; 60 MB newline-less log: **120 MB -> 16 MB** peak |
| tools | caps and log damage invisible | `log_health` (corrupt / over-long / torn lines, logs bigger than a view, 256 MB read budget, no content); `limits_status` adds the replay cap; priv_admin `codex_tail` streams only the tail | `test_tool_loghealth.py`, `test_tool_limits.py` |
| skills | (none) | `log-integrity`; `log-retention` starts with `log_health` | `test_skill_r20.py` |
| coverage | (none) | `cov20_a`, `cov20_b` | 6 statements missed of 11,388 |

## v0.33: Squad round 19: thread-side events wake the stream, capped stream subscribers. 2887 tests, coverage 99%

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | **events emitted from worker threads stalled the live stream**: `Codex.emit` called `asyncio.Queue.put_nowait` from runner/fleet threads; the queue is not thread-safe and the put never woke the loop | each subscriber queue remembers its loop; off-loop emits go through `call_soon_threadsafe`; subscribers of a closed loop are dropped | `test_robust_v033_threademit.py`; thread emit at 0.1 s: arrived **3.0 s** (only when a timer fired) before, **0.10 s** now |
| robust | **unbounded `/ws/events` subscribers**, each a 1000-event queue every emit fans out to | `AVRONE_MAX_WS_SUBSCRIBERS` (64, hard 1024): past it the socket closes 1013; slot released if accept fails | `test_robust_v033_wscap.py`; live 200 connects: **200 held** before, **64** now |
| tools | caps invisible | `stream_status` shows cap + connections refused; `limits_status` shows the cap + live subscribers; `load-limits` maps the 1013 | `test_tool_gatewayview.py`, `test_tool_limits.py` |
| skills | (none) | `release-verification` (probe old vs new on a real server); `event-stream-health` covers the cap and thread-side events | `test_skill_r19.py` |
| coverage | (none) | `cov19_a`, `cov19_b` | 9 statements missed of 11,258 |

## v0.32: Squad round 18: bodies capped before buffering, bounded fleet registry. 2866 tests, coverage 99%

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | **an unauthenticated caller could make the server hold any amount of RAM**: every route read the whole body before auth (fleet HMAC needs it, `/api/chat` parses it) | `BodyLimit` ASGI guard: 413 on a declared Content-Length over the cap, chunked bodies counted and cut off; `AVRONE_MAX_BODY_BYTES` (16 MB, hard 512 MB), never below what the message cap needs, agent-pack route fits a quota-sized bundle | `test_robust_v032_body.py`; live 400 MB chunked unauth POST: v0.31 **401 after RSS 48 -> 415 MB**, v0.32 **413, RSS flat** |
| robust | **fleet registry unbounded**: any allowed id (allow-all by default) could register, each join rewrote fleet.json, megabytes per record | `AVRONE_FLEET_MAX_NODES` (256, hard 4096): offline peers evicted oldest-first, live never, then "registry full"; node record <= 64 KB | `test_robust_v032_registry.py` |
| tools | no way to remove a peer | `fleet_forget` (acting, audited): refuses an online still-allowed peer (it would rejoin), final once revoked | `test_tool_packpeer.py` |
| tools | new caps invisible | `limits_status` adds body cap, agent-pack body cap, peers vs registry cap; `load-limits` skill maps both new refusals | `test_tool_limits.py`, `test_skill_r15.py` |
| skills | (none) | `fleet-offboarding`, `flood-response` (every refusal message it cites is asserted to exist in code) | `test_skill_r18.py` |
| coverage | (none) | `cov18_a`, `cov18_b` | 14 statements missed of 11,201 |

## v0.31: Squad round 17: bomb-proof agent packs, validated peer urls. 2838 tests, coverage 99%

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | **a 290 KB agent pack drove the importer to ~1.2 GB RAM**: quota checks used header-declared sizes, but tarfile buffers PAX/GNU extended headers first; a 50 MB bundle (under the cap) would ask for hundreds of GB | decompressed stream itself is capped (`_CappedStream`, tar read in stream mode); PackError "expands past the quota" | `test_robust_v031_packbomb.py` (tracemalloc peak < 8 MB); live RSS 1160 MB -> 87 MB |
| robust | **a token holder could register `http://169.254.169.254` as its url** and this node would POST to it and echo the reply into dispatch errors; credentials/CRLF urls stored too | `check_node_url`: http(s), host, no userinfo/query/fragment/control chars/port 0, never metadata/link-local/multicast/unspecified (incl. v4-mapped); LAN + loopback still fine; every outgoing `_post` re-checks saved urls | 33 cases; live register: v0.30 metadata url **200**, v0.31 **400**, LAN url 200 |
| tools | (none) | `pack_list`: verifies every pack in memory, OK or the exact import refusal, symlinks flagged | `test_tool_packpeer.py` |
| tools | old fleet.json may hold bad peer urls | `peer_audit`: each peer vs current url rules + status, never prints urls | same |
| skills | (none) | `agent-pack-hygiene`, `peer-onboarding` | grounding + claim tests |
| coverage | (none) | `cov17_a`, `cov17_b` | 18 statements missed of 11,086 |

## v0.30: Squad round 16: replay-safe restarts, leak-free event stream. 2780 tests, coverage 99%

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | **a captured signed fleet request was accepted again after the node restarted** (nonce cache was memory-only; anything still inside the 60 s window replayed, including `/api/fleet/run`) | replay cache persisted atomically to `data_dir/fleet_nonces.json`, expired/junk entries dropped on load; if it is unreadable, a boot guard refuses anything signed before this boot for 2 windows; a failed write never refuses a valid request | `test_robust_v030_replay.py`; live: v0.29 replay after restart **200**, v0.30 **401 replayed** |
| robust | **every dropped `/ws/events` client leaked its 1000-slot queue and handler on a quiet node** (only noticed on the next failed send); a non-`WebSocketDisconnect` send error escaped | anyio task group: event pump + disconnect watcher, first to finish cancels the other, subscriber always released | real-uvicorn integration test fails on v0.29 and passes now |
| tools | (none) | `replay_guard`: live nonces vs cap, persisted + readable, boot-guard time left; OK/DEGRADED/OFF, never prints nonces | `test_tool_gatewayview.py` |
| tools | slow dashboards lost events silently | codex counts `emitted`/`dropped`; `stream_status`: subscribers, queue depth vs limit, FULL/backed-up flags | same |
| skills | (none) | `fleet-restart-safety` (data dir must persist, boot guard, rejoin check), `event-stream-health` (FULL subscribers, history via `codex_search`) | grounding + tool-output match tests |
| coverage | (none) | `cov16_a` (stream transport errors, fleet.json save cleanup), `cov16_b` (checkpoint claim released on stamp failure, nonce tmp cleanup) | 21 statements missed of 10,956 |

## v0.29: Squad round 15: load caps, run-log retention. 2750 tests, coverage 99%

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | **`/api/chat` accepted unlimited background runs** (each holds a task, run state and a log) and messages of any size went into every agent prompt | 429 past `AVRONE_MAX_BACKGROUND_RUNS` (default 32, hard 1024) in flight; 413 past `AVRONE_MAX_MESSAGE_CHARS` (default 200k, hard 5M), also on `first_answer` | `test_robust_v029_limits.py`; live server with cap 2: burst of 4 got `200, 200, 429, 429`; 1,001 chars got 413 |
| robust | one MCP batch of N `avrone_squad` calls started N squads at once | `AVRONE_MCP_MAX_INFLIGHT` (default 4, hard 64), then an `isError` busy result; counter released on failure; task-size cap applies | `test_robust_v029_mcp_limits.py` (peak concurrency = cap) |
| tools | no single place showing which cap refused you | `limits_status`: every cap, value, env/default, and live usage (model calls in use, background runs, agents working, agents per run) | `test_tool_limits.py` |
| tools | run logs are append-only and never rotated | `run_logs`: usage report; `prune` deletes logs older than N days, always keeping the newest `keep` (min 10) and active runs (acting) | prune test keeps current + active runs |
| skills | (none) | `load-limits` (429/413/busy to its cap; size `AVRONE_MAX_PARALLEL` for CPU vs GPU), `log-retention` (measure, `secret_scan`, prune, verify) | grounding + defaults-match-code tests |
| coverage | (none) | `cov15_a` (run_logs stat error, evolve symlink loop, privileged allow-list), `cov15_b` (agent out-of-turns, Lead retry on thin merge) | 26 statements missed of 10,841 |

## v0.28: Squad round 14 — MCP JSON-RPC hardening, secrets out of run logs — 2712 tests, coverage 99%

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | MCP: non-object params/arguments and a non-string method leaked `AttributeError` as -32603; a dict task ran as its repr; `[]` got no answer; **one line over the 16 MB reader limit killed the server** | -32602/-32600 per JSON-RPC, `'task' must be a string`, empty batch -32600, oversized line answered -32600 and the server keeps serving | `test_robust_v028_mcp.py`; live `python -m avrone.mcp_server` with a 17 MB line: error, then `ping` still answers, exit 0 |
| robust | **secrets reached the run log and `/ws/events` verbatim** (an agent running `env`, a key in a clone URL) | codex scrubs every event string with the ctxspawn redactor (env secret values, bearer/API keys, private keys, JWTs, URL credentials) before writing or broadcasting; `AVRONE_CODEX_REDACT=off` to disable | `test_robust_v028_redact.py` |
| tools | old run logs may still hold secrets | `secret_scan` (runs/data/workspace; path:line + kind, never values; `fix` redacts run logs atomically; acting); redaction made idempotent (`[REDACTED]` no longer re-redacted into `[REDACTED]]`) | `test_tool_secrets.py` |
| tools | no way to check the MCP layer from inside | `mcp_selftest`: in-process handshake, ping, tools/list and three malformed requests | PASS on a fresh node |
| skills | — | `secret-hygiene` (scan → fix → rotate), `mcp-client-check` (stdio pitfalls, tool names) | grounding + end-to-end hygiene test |
| coverage | — | `cov14_a` (secret_scan internals), `cov14_b` (fleet branches) | 32 statements missed of 10,693 |

## v0.27: Squad round 13 — agent and run routes are local-only by default — 2676 tests, coverage 99%

**Breaking (safe direction):** `POST /api/chat`, `/api/runs*`, `/api/telemetry`, `/api/map`, `/api/evolve`,
`/api/osint/metrics` and `/ws/events` now answer only loopback callers or callers with `X-Admin-Token`
(the events socket also takes `?token=`). `AVRONE_PUBLIC_ACCESS`:

| value | who gets in |
|---|---|
| `loopback` (default; any unknown value) | 127.0.0.1 / ::1, or anyone with the admin token |
| `admin` | the admin token only, even from loopback |
| `all` | anyone who reaches the port (pre-v0.27). Compose uses this because it publishes on 127.0.0.1 and every in-container caller comes from the bridge network; set `admin` when `AVRONE_PUBLISH_ADDR` is not 127.0.0.1 |

Live probe (server bound 0.0.0.0): `/api/runs` loopback 200, remote 403, remote+token 200; remote `POST /api/chat` 403; `/api/status` stays 200.

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | anyone reaching the port could start agents (shell) and stream every run | loopback/admin guard on 9 routes; admin token compared in constant time | `test_robust_v027_access.py` |
| robust | doctor warned on every exposed bind; UI couldn't reach guarded routes remotely | doctor warns only for `all` on an exposed bind (info otherwise); UI sends the token; compose documents its default | `test_doctor.py` |
| tools | api_surface had no notion of guarded routes | `[local]` class + policy; new `exposure_check` (bind, policy, token strength never values, remote-open routes; LOCAL/GUARDED/AT RISK) | `test_tool_exposure.py` |
| skills | — | `exposure-hardening`, `deploy-readiness` (go/no-go for VPS/Fly/Railway) | AT RISK → GUARDED test |
| coverage | — | `cov13_a` (evolve), `cov13_b` (policy, bridge, checkpoint) | 37 statements missed of 10,521 |

## v0.26: Squad round 12 — HTTP surface fuzz, no orphaned children, host view tools — 2641 tests, coverage 99%

Great Scale form (heat → mapping → verification; Ψ = CONE).

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | Fuzzing all 21 GET + 8 POST routes (11 junk bodies × unsigned / fleet-signed / admin): a **signed** peer's malformed `/api/fleet/message` leaked `KeyError` as a 502 | `handle_message` validates agent/message/route types; server maps `ValueError` → 400 with a reason | `test_robust_v026_api.py` |
| robust | Admin `/api/terminal` with a NUL byte → 500; a **cancelled** run (evolve_cancel, client gone) left its child process running | runner refuses NUL bytes, returns a result for unstartable commands, and kills + reaps the child on cancellation | `test_robust_v026_runner.py` (live: cancelled `sleep` is gone); 8/16 fail before |
| tools | Agents couldn't see their own processes or front door | `proc_status` (own children from /proc: age, state, RSS, cmd; long-running + zombies; never env) and `api_surface` (every route + auth class; admin/fleet enabled) | 7 tests |
| skills | — | `surface-audit` (read-only own-node audit → Great Scale report), `runaway-process` (proc_status → codex_search → evolve_cancel) | grounding + live cancel-kills-child |
| coverage | disk reader raised on an unreadable path | returns None | `test_cov12_a.py`, `test_cov12_b.py`; 47 statements missed of 10,450 |

api_surface on a default node: 27 routes — 15 public, 8 admin, 4 fleet-signed; admin token unset (admin routes 403), fleet off.

## v0.25: Squad round 11 — whole-registry fuzz, corrupt state can't stop a node, schemas everywhere — 2593 tests, coverage 99%

Great Scale form (heat → mapping → verification; Ψ = CONE).

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | Fuzzing all 77 tools with 8 junk arg shapes leaked **141** internal `AttributeError`/`KeyError`s to the model (list/str/None args, missing `path`, `task_board` op=add with no title) | `ToolRegistry.call` refuses non-object args and coerces non-str returns; kit tool schemas; task_board validates ops | `test_robust_v025_args.py`: 141 → 0 |
| robust | A torn/hand-edited `fleet.json` made `FleetManager()` raise → **node could not boot**; task_board/evolve files likewise | corrupt file moved aside as `fleet.json.corrupt-<ts>`, junk fields dropped; task_board + `_finish_abnormally` atomic, `list_accepted` tolerant | `test_robust_v025_state.py` 8/10 fail → 0 |
| tools | No way to see durable-state damage | `state_health` (per-area unreadable/torn/set-aside counts, paths only, never contents) | 7 tests |
| tools | 48 builtins had no arg schema (no pre-call ARG ERROR) | `infer_schema` from args_doc (required scalars only; optionals stay lenient) + `registry_audit` (by source, acting vs read-only, schema-less list) | 48 → 1 (great_scale, alternative shapes) |
| skills | — | `state-recovery`, `toolset-hardening` (registry_audit → failing test → gated evolve proposal) | grounding + end-to-end recovery test |
| coverage | — | `test_cov11_a.py`, `test_cov11_b.py` (+ `_resume_start` non-object loop file fix) | 66 statements missed of 10,311 |

What an agent sees on a fresh node: 79 tools (31 acting, 48 read-only), 1 without a schema, state HEALTHY.

## v0.24: Squad round 10 — evolve traversal closed, local models reachable behind proxies, lesson pruning — 2536 tests, coverage 99%

Great Scale form (heat → mapping → verification; Ψ = CONE: every residual closed).

| Lane | Finding (heat) | Mapping | Verification |
|---|---|---|---|
| robust | `evolve_reset {"pid": "../x"}` read any `x.json` and **rmtree'd the dir it named**; `evolve_diff` ran git there; corrupt/partial proposal files crashed reset/diff/list | `_proposal()`: pid must be a plain id, worktree must be `data_dir/evolve/wt-*`, corrupt meta handled | `test_robust_v024_evolve.py` 9/10 fail → 0/10 |
| robust | Found live: with an env `HTTP(S)_PROXY`, calls to the default Ollama on 127.0.0.1 went through the proxy (ConnectError / bench saw 404 "unhealthy") | `is_loopback_url` → `trust_env=False` for local upstreams only (gateway, legacy llm, model_bench) | `test_robust_v024_proxy.py` 12/13 fail → 0 |
| tools | Lessons could be learned but never retracted | `lesson_forget` (key + reason; reason posted as a bridge `correction`, audited; `MemoryStore.delete`), `bridge_status` (posts by kind/agent, relayed, unanswered questions) | 12 tests |
| tools | Router view hid how calls leave the node | `router_status` per-upstream `route=direct\|env-proxy` (proxy value never shown) | 2 tests |
| skills | — | `local-model-ops` (route, circuits, bench-driven model choice, fallback) and `lesson-hygiene` (recall → verify → forget → relearn → close open questions) | grounding + full-cycle tests |
| coverage | trinity/fleet 96% | `test_cov10_a.py` (fleet fallbacks in trinity), `test_cov10_b.py` (fleet thermal/inbound/privileged gate) | trinity 98%, fleet 98%, total 99% |
| lead | `AVRONE_PROVIDER=ollama` with no model set picked `llama3.1` (8B, never benchmarked) while docs said qwen2.5:3b | preset default → `qwen2.5:3b` | 3 preset tests updated |

**Live model bench re-run (2 vCPU, ~8 GB RAM, no GPU):** llama3.2:3b scored **69/100**: valid JSON 4 of 5 turns but it *printed a tool result without calling the tool* (guessed 52277; real 53527), then finished squad (35) and deep (10). qwen3.5:4b and gemma3:4b could not load on this host (llama-server killed while loading; model files live in RAM-backed storage here), so they stay unscored. qwen2.5:3b (100/100) remains the default.

## v0.23: Squad round 9 — conclude-bridge hardening, long-horizon learning loop, router bridge view — 2449 tests (2468 after merging origin), coverage 99%

Great Scale form (each finding: heat → mapping → verification; Ψ = CONE when every residual closes).

**Robust lane: the agent ↔ sub-agent conclude Bridge** (`tests/test_robust_v023_bridge.py`, 15 tests). Seven suspected bugs, six reproduced, all six fixed:

| Finding | Before | After |
|---|---|---|
| Torn last line (crash mid-write) swallowed the next post on reload | 5/5 fail | 0/20 |
| `merge_remote` raised on a circular relayed post instead of rejecting it | 5/5 | 0/20 |
| Evicted posts resurrected by a two-node relay cycle, churning the ring | 5/5 | 0/20 |
| Eviction memory lost across restart (now a `{"tombstones": [...]}` header, ignored by old readers) | 5/5 | 0/20 |
| NaN / ±inf / bool timestamps accepted from remote nodes | 5/5 | 0/20 |
| Two Bridge instances on one file dropped each other's posts at compaction (compaction now re-reads the file under the shared lock) | 1/1 | 0/20 |
| `distill_lessons` not atomic: concurrent runs both reported the same lesson as new (per-data_dir `lessons_lock`) | 1/5 | 0/20 |

Spoofed-local-origin rejection (7th) left out: no failing case, would be a policy change.

**Tools lane: recursive learning loop, long-horizon.** `research_note` posts a finding/conclusion/question to the run bridge and, for findings and conclusions, stores a deduped lesson under the same key `distill_lessons` uses (says *newly learned* vs *already known*; secrets masked). `lesson_recall` is the read half: topic-ranked recall of what earlier runs concluded, redacted and bounded.

**Skills lane: router bridge + comms docs.** `router_status` shows the model router (primary + `AVRONE_LLM_FALLBACKS` chain, each upstream's circuit closed/open/half-open with cooldown); API keys *and* base URLs are masked (a URL leak via `last_route` was caught by its test and fixed). Skill `agent-comms-and-conclude` documents bridge posting, replies with refs, spawn, conclusion → lessons → next run, and cross-node relay; its test drives a real `run_agent` where a Researcher posts a finding, spawns a Verifier that replies with a conclusion referencing it, and the conclusion distills into a recallable lesson.

**Coverage lane.** `tools_agenthome`, `recon`, `doctor`, `jobqueue`, `agentcontainer`, `research_note`, `lesson_recall`, `router_status` 100%; `bridge`, `presence` 99%. Total 98% → 99%.

## v0.22: Squad round 8 — root-gateway linking pipeline proven end to end, crossed-order run-scope fix, resumable evolve loops, agent homes — 2366 tests, coverage 98%

- **Root-enabled gateway pipeline** (`tests/test_robust_v022_pipeline.py`, 50 tests): a real signed request goes gateway → `handle_run` → `TrinityCore.chat` → tool call → `priv_shell`/`priv_admin` → runner, offline. With every condition met the allow-listed command runs once and is audited; removing any single condition refuses with its own code and nothing executes.
- **Fixed: crossed-order run scope** (`FleetManager._run_scope`). Overlapping runs ending A-start, B-start, A-end, B-end left `fleet_privileged` set after all runs ended, let an unprivileged run wipe a live grant, and let a shallow run lower a deep run's hop count. Live runs are now tracked under the state lock (5/5 → 0/5). Overlapping `priv:shell` + `priv:admin` runs now block both tools until one ends.
- **Fixed: netsquad route/budget** restored in crossed order (same mechanism, 5/5 → 0/5) and **`thermal_migrate`** exporting an agent that became busy mid-loop (50/50 → 0/50).
- **Asyncio hammer tests** (`tests/test_robust_v022_async.py`): same-checkpoint chats, `AVRONE_EVOLVE_MAX_BG`, same-node register/heartbeat, memory_sync merges — all held.
- **New tools:** `fleet_status` (this node's fleet view, on/off settings only, no tokens or URLs), `flake_probe` (runs one read-only perception tool 1–50× and reports nondeterminism as a Great Scale snapshot), `agent_home` (the calling agent's own home: quota, counts, container hosting, home node, last pack).
- **Evolve reach:** loops checkpoint each iteration; `evolve_start(resume_loop_id=...)` resumes an interrupted loop from its last completed iteration. Resumed work counts toward the same caps; current env/hard caps are re-applied.
- **Skill:** `skills/agent-home-resilience` (quota, symlinks, pack export/import, thermal migration). `evolve-loop-operations` documents resume.
- **Coverage:** tools_codexstats, tools_mempeek, agentfs, ctxspawn, tools_extra, agentpack → 100%; server 99%. Total 98%.
- **Test fix:** the v0.21 single-flight migration test counted successes and flaked (~1/3 alone) when a late thread migrated after the first had finished; it now measures true overlap (max concurrent = 1).

## v0.21: Squad round 7 — TOCTOU sweep, Great Scale bridge, recon default-deny, long evolve loops — 2150 tests, coverage 97%

- **Robust lane — 10 check-then-act races fixed** (Barrier hammer tests, before → after): agentpack shared index 170/200 → 0, container reaper re-adopting a container mid-`rm` 200/200 → 0, evolve history 148/200 → 0, fleet nonce double-accept 125/200 → 0 and dropped nonces 126/200 → 0, fleet registry readers 199/200 → 0, jobqueue double-claim across processes 69/100 → 0 (`BEGIN IMMEDIATE`), checkpoint lost steps 25/200 → 0, register two-URL 3/200 → 0, migrate guard 1/200 → 0. `fleet.json` now written atomically. Privileged tier unchanged; a signed concurrent burst is all refused `privileged_disabled`.
- **Coverage lane**: config, agentfs_tools, codex, agentpack_tools, osint → 100%.
- **New tools**: `codex_stats` (payload-free aggregate of the agent's own run log) and `memory_peek` (bounded, redacted view of the agent's own memory namespace).
- **Evolve**: `evolve_history` is now an agent tool; new `evolve-loop-operations` skill; opt-in long loops `AVRONE_EVOLVE_LONG=allow` (hard ceiling 50 iterations / 7200s, default off); background loops left "running" by a restart are marked `interrupted` with a resume hint and recorded.
- **Great Scale bridge** (`avrone/greatscale.py`, tool `great_scale`): exact port of great-scale `public/residual.cjs` (node parity test). Evidence channel: `S = heat(p) = p(1−p)` is the Bernoulli variance of a check's failure rate, `Ψ = φ⁻² + S`, runs to detect `N ≥ ln α / ln(1−p)`, plus a signed `orientation` channel because heat is orientation-blind (an always-failing check also sits on the cone floor). A mathematical embedding of measured evidence, not a physical claim.
- **Recon is default-deny**: unset/blank `AVRONE_RECON_ALLOWLIST` refuses every target; list your own/authorized domains, or set `*` to allow any public host explicitly. Private/metadata refusal still always applies.
- **Lead fixes**: env_report redacts tokens glued to other text.

## v0.20: Squad round 6 — coverage, introspection tools, edge skills + evolve history, gateway/home hardening — 1948 tests, coverage 96%

- **Coverage lane**: mcp_server.py 82% → 99%, plugins.py 81% → 100%, evolve.py 88% → 97%, gateway.py 91% → 100% (49 new offline tests).
- **New tools** (`avrone/tools_introspect.py`): `inspect_tool` (one tool's description, args, schema and source, or a bounded search; unknown names get close matches) and `env_report` (every Settings value + `AVRONE_*` env, with secret names shown only as set/unset and token-shaped values masked whatever the variable is called).
- **New skills**: `edge-node-health` (governor health, healthy-node picks, when to fall back locally) and `checkpoint-resume` (long squad/deep runs by checkpoint id). Tests check every env var and tool they name exists.
- **Evolve history**: finished, cancelled and crashed loops are summarised to `data/evolve/loops/history.jsonl` and readable after restarts. `AVRONE_EVOLVE_HISTORY` (default 50, can only lower; hard cap 200). Loop caps unchanged.
- **Gateway hardening** (stricter only; privileged tier unchanged and deny-by-default): strict ASCII timestamps, nonce format/length with a new `bad_nonce` code, non-ASCII signatures → 401 not 500, nonces kept until `max(ts+60s, now+120s)` (closed a replay at the window edge and on clock step-back), replay cache bounded at 100,000, deep-nested JSON → `bad_body`, strict `hops` parsing.
- **Thermal + homes**: thermal migration never auto-targets a hot/throttled peer, single-flight `migrate_agent` and `import_pack`, NaN/inf thermal limits fall back to 85°C, stream-checked pack size limits, duplicate pack members refused, `O_NOFOLLOW` appends + random exclusive temp files in agent-fs quota writes, memstore tolerates bad timestamps and caps far-future peer clocks at +1h.
- **Lead fixes**: removed a stray download-wrapper first line from pyproject.toml, README.md and avrone/__init__.py (broke `pip install` outside CI); agent-pack import now keeps its memory merge + home record under the per-agent lock (concurrent imports refused ~6% of the time as "belongs to another agent").

## v0.19: Full-enablement release — upgraded stack + `deploy/full.env` final-product profile

- **Software stack upgraded to current:** FastAPI 0.162, uvicorn 0.54, httpx 0.28.1 (floors raised in `pyproject.toml`); vision extras now `zxing-cpp>=2.3`. Python >= 3.11 unchanged. The offline suite (1680 tests) still gates every push in CI on 3.11 + 3.12.
- **`deploy/full.env` — the fully-enabled configuration:** every shipped feature on in one file: deep squad of 6, model fallback chain + circuit breaker, Docker-backed agent sandbox with network, admin terminal, plugins + MCP + search, fleet node (agents spawn across nodes), self-changes + skills, self-evolution gated by tests, authorized recon. `cp deploy/full.env .env`, fill the `TODO` placeholders, run `avrone-cli doctor`, and boot. `.env.example` stays lean; this is the maximal profile.
- Model default remains **qwen2.5:3b** via Ollama (benchmarked 100/100 — see "Free open-source models"); on a GPU host set `AVRONE_LEAD_MODEL=qwen3.5:9b` for stronger merges.

## v0.18: Squad round 5 — full-context sub-agents, recursive learning bridge, network-dispatched squads, edge-case hardening — 1680 tests

Four lanes in parallel, Lead merge + fixes: the bridge now relays across nodes per run (`net-<sender>-<origin_run>`; inbound posts readable by the remote squad, only locally written posts return), and fleet audits that carried `kind=`/`run_id=` were silently dropped by Codex.emit (now `req_*`). Note: `AVRONE_BRIDGE_ROUNDS` defaults to 1 (one extra model call per worker) and lessons are on; set `AVRONE_BRIDGE_ROUNDS=0` / `AVRONE_LESSONS=off` for v0.17 behaviour. Known: agent packs carry memory under the dir id, which differs from the name-derived namespace for roles with spaces or punctuation.

- **edgecases** — agent spawning / per-agent env / fleet edge cases (tests/test_edgecases_v018.py, 27 tests): concurrent live agents of one run whose roles sanitize alike ("Builder!" / "Builder?", or "Fact-Checker" / "FactChecker" for memory) now get distinct homes and memory namespaces (`-2`, `-3` suffix; the first keeps the plain id, so persistence across runs and sequential re-spawns is unchanged); `core.active` records each agent's `agent_id`, so thermal migration never moves the home of a working agent whose `agent_id` differs from its role; messages that land during an agent's last turn are audited as `agent.message_dropped` instead of vanishing; a deep checkpoint resumed after `squad_size` changed no longer mixes cached reviews of the old first-wave answer with the new one; memstore appends after a torn (crash mid-append) last line no longer lose the new record; `remote_broadcast` normalizes routes before de-duplicating (`good/`, `/good`, ` good `) and drops empty targets instead of turning them into an `auto` pick. Pinned as safe: batched spawns with `max_parallel=1` at depth 2 (no deadlock), squad cancellation (no ghosts, inbox cleared, no failed step checkpointed), quota races, self/A→B→A routes and hops at the limit, multi-process memstore writers, empty/zero-memory/at-quota agent packs, unicode/huge checkpoint tasks.
- **ctxspawn** — `spawn` now hands the child a ContextPack: `context` = `full` (default: the run's original task, a lineage chain root -> ... -> parent, the parent's role/focus, squad sibling roles/focuses, the parent's last 8 tool calls/results, a read-only snapshot of its memory namespace and the file names in its agent dir) | `summary` (task + lineage + a compact digest) | `none` (old behaviour); optional `focus` and `share_dir` (child may read, never write, the parent's agent dir under a confined `parent/` prefix in read_file/list_files/search_files/diff_files). Packs are capped by `AVRONE_SPAWN_CONTEXT_CHARS` (default 12000, max 64000; deterministic truncation keeps task + lineage first) and secret-redacted (tokens, keys, Authorization headers, `*_TOKEN/*_KEY/*_SECRET` values). Children get their own dir `<parent id>-<role>`; lineage lives on AgentContext and in the `agent.spawn` CodeX event (`context_mode`, `context_chars`, `lineage`, `share_dir`); `AVRONE_MAX_AGENTS_PER_RUN` (default 64) caps agents per run alongside the unchanged depth limit.
- **netsquad** — squad mode dispatched into the node network: `AVRONE_SQUAD_DISPATCH=local|network|hybrid` (default local = unchanged) places each squad subtask on a healthy fleet node (round-robin by health score), where it runs as a sub-squad of `AVRONE_NET_SUBSQUAD` agents (default 2, max 4; `AVRONE_NET_WORKER_MODE=chat` for one) and may place further down the tree hop by hop (cycle refusal, `AVRONE_FLEET_MAX_HOPS`); capped by `AVRONE_NET_MAX_NODES` (8) and `AVRONE_NET_MAX_REMOTE_AGENTS` (32); remote failure runs the subtask locally; privileged runs never leave the node; receivers need capability `squad:net`; optional bridge-post relay; per-request `"dispatch"` on `/api/chat` and `avrone-cli squad --dispatch network`; doctor warns on network/hybrid with an open allowlist.
- **bridge** — a recursive learning bridge so agents among agents conclude with each other's information: a run-scoped blackboard (`avrone/bridge.py`; posts `finding|question|answer|correction|conclusion` with refs, bounded by `AVRONE_BRIDGE_MAX_POSTS` (500) and 4000 chars/post, persisted to `data_dir/bridge/<run_id>.jsonl` with corrupt-line-tolerant replay) shared by every squad worker, sub-agent at any depth and deep reviewer via `bridge_post` / `bridge_read` / `bridge_reply` (codex `bridge.post`); a squad cross-examination round before the Lead merge (`AVRONE_BRIDGE_ROUNDS`, default 1, max 3, 0 = classic squad; stops early when a round adds no correction) whose digest of joint conclusions and unresolved disagreements goes to the Lead; run-end distillation of up to 5 conclusions into a `lessons` memory namespace (hash-deduped, `AVRONE_LESSONS_MAX` 200, oldest evicted) that is keyword-matched back into new runs' worker prompts (`AVRONE_LESSONS=off` disables); `Bridge.export(since)` / `merge_remote(posts, origin_node)` hooks for fleet relay; `GET /api/runs/{id}/bridge`.

## v0.17: Squad round 4 + edgepack — unified agent fs, batched + schema-checked tool calls, health-routed edge offload, API/fleet checkpoints, container-hosted agent homes with thermal migration — 1586 tests

Five lanes in parallel (git worktrees, two green commits each), Lead merge + fixes (collision-free fleet checkpoint keys; one shared node-health block for routing and migration). Everything new is off by default except fsunify and toolcall.

- **fsunify** — the generic file tools (`read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `diff_files`) now work inside the agent's own persistent dir (`workspace/agents/<id>/`, confined and symlink-proof), with the per-file cap and dir quota applied to `write_file`/`edit_file`; a `shared/` prefix reaches the old shared workspace (its `agents/` subtree is off limits), and `python` runs with cwd = the agent dir on the local and Docker runners. `AVRONE_AGENT_FS=0` (or no agent dir) keeps the exact old behaviour.
- **toolcall** — agents can send a batch `{"tools": [...]}` of independent calls per turn (run concurrently, results fed back in order; cap `AVRONE_MAX_CALLS_PER_TURN`, default 4, max 8; one crashing call never sinks the others); core tools declare arg schemas that are checked before the call, so a bad call returns an `ARG ERROR` the model repairs instead of running (unknown tools suggest close names; checks step aside after 3 consecutive errors); new CodeX events `tool.batch` and `tool.arg_error`.
- **edgeroute** — governor-aware fleet routing: heartbeats/registration carry a compact governor health block (load ratio, mem %, disk free, CPU temp, headroom, ok/throttled/hot, active agents), stale with node status; `FleetManager.select_nodes(k, need)` ranks online, allowlisted children cool/idle first and skips hot/throttled ones (`remote_spawn node=auto` uses it); opt-in `AVRONE_FLEET_OFFLOAD=off|spill|spread` (default off) sends a governor-capped squad's overflow workers to healthy edge nodes over the signed dispatch path (mode chat, `AVRONE_FLEET_OFFLOAD_TIMEOUT` 300 s) and runs any failed one locally; privileged runs never offload; audited as `fleet.offload` / `fleet.offload_fallback`; node health in `fleet_nodes`, telemetry and the map; doctor warns on offload with an open `AVRONE_FLEET_ALLOW`.
- **ckpt** — server-managed checkpoint/resume over the API, MCP, CLI and fleet: `POST /api/chat` takes `checkpoint_id` (squad/deep, `[A-Za-z0-9_-]{1,64}`, stored only under `data_dir/checkpoints/<id>.json`); re-posting the same id + task resumes (also a background run that died), a different task is 409; admin `GET /api/checkpoints`, `GET`/`DELETE /api/checkpoints/{id}`; `avrone_squad`/`avrone_deep` and `avrone-cli --checkpoint-id` take the same id; remote squad/deep jobs carry it in the signed body and the running node keys it `fleet-<sender>-<id>` (owner-stamped), so a retried job resumes and senders never collide.
- **edgepack** — portable, container-hosted agent homes for edge nodes. `AVRONE_AGENT_CONTAINER=persistent` (default `off`) gives each agent one long-lived, lazily created Docker container `avrone-agent-<node>-<id>` with only its dir mounted at `/agent` (`--cap-drop ALL`, `no-new-privileges`, pids cap, `AVRONE_AGENT_MEM_MB` 512, `AVRONE_AGENT_CPUS` 1.0, `--network none` unless `AVRONE_ALLOW_NETWORK`; the isolate path now shares these flags), reaped after `AVRONE_AGENT_IDLE_SECS` (600) idle. `avrone/agentpack.py` exports an agent's dir + memory namespace as a deterministic tar.gz with a sha256 manifest (no symlinks/devices, quota-bound); import verifies every hash, confines paths and only replaces the same agent's home (same id and birth node) with a newer copy. `AVRONE_AGENT_MIGRATE=on` (default off) adds signed `POST /api/fleet/agentpack` (capability `agent:migrate`, sender must be in a non-empty `AVRONE_FLEET_ALLOW`, never privileged), `FleetManager.migrate_agent` to the coolest / least-loaded peer, and a governor hook that moves idle agents off a node hotter than `AVRONE_THERMAL_MAX_C` (audited `agent.migrate` / `agent.migrate_refused` / `agent.migrate_failed`; a failed agent stays put). Tools `agent_pack_export`, `agent_pack_import`, `agent_migrate`; doctor warns on migrate + open allowlist.

## v0.16: Squad round 3 — privileged execution, per-agent filesystem + memory, edge resilience — 1372 tests, coverage 95%

- **priv_shell / priv_admin** — the v0.15 privileged tier now does something, and only when granted. Requires BOTH `core.fleet_privileged` and a run-scoped grant set by the signed `handle_run` (a concurrent unrelated run never inherits it). `priv_shell`: `AVRONE_PRIV_SHELL_ALLOW` bare-name allowlist (empty = refuse all), no metacharacters/pipelines/redirects, args re-quoted, `AVRONE_PRIV_SHELL_TIMEOUT` (30 s, max 300). `priv_admin`: fixed read-only actions (queue_stats, node_list, fleet_info, codex_runs, codex_tail). Audited as `priv.shell` / `priv.refused` / `admin.action` / `admin.refused`.
- **Per-agent filesystem** — each agent role gets `workspace/agents/<id>/` (0700, symlink-proof, confined); its `shell` commands start there. Docker: shared container uses `-w /workspace/agents/<id>`; `AVRONE_AGENT_DOCKER_ISOLATE=1` runs each command in a throwaway container with only that dir mounted at `/agent`. Tools `fs_write/fs_read/fs_list/fs_delete`, quota `AVRONE_AGENT_FS_QUOTA_MB` (50, max 1024), per-file `AVRONE_AGENT_FS_MAX_FILE_MB` (5, max 64). `AVRONE_AGENT_FS=0` disables.
- **Durable agent memory + edge sync** — per-agent namespaces in memory.jsonl (survive restarts, flock + atomic rewrite, caps `AVRONE_MEMORY_MAX_ENTRIES/VALUE/NAMESPACES`), `memory_list`. `memory_sync` pushes to the parent over a new signed `/api/fleet/memory` (full dispatch_gateway, capability `memory:sync`); receiver merges into `fleet:<sender>/<ns>`, never its own namespaces, newest wins.
- **Resilience governor** — reads load/CPU, memory, disk free and CPU temperature (psutil, `/proc`, `/sys/class/thermal`) and throttles squad/deep parallelism on hot or starved nodes (`AVRONE_THERMAL_MAX_C` 85, `AVRONE_MIN_PARALLEL` 1, `AVRONE_GOVERNOR`). Never 0, never deadlocks, falls back to full parallelism if sensors fail. Checkpoint/resume for long squad and deep runs (`--checkpoint PATH`). Node health + throttle/resume counts in telemetry.
- **Timeouts** — `AVRONE_AGENT_TIMEOUT` stays off by default (decision 2026-10-28).

## v0.15: Squad round 2 — secure node dispatch gateway, privileged tier, background evolve, doctor — 1129 tests, coverage 95%

- **Dispatch gateway** — every inbound `/api/fleet/run` and `/api/fleet/message` goes through `FleetManager.dispatch_gateway`: token -> HMAC -> 60 s window -> nonce replay -> sender in `AVRONE_FLEET_ALLOW` -> hops -> capability. Distinct refusal codes (401 auth / 403 policy), `fleet.dispatch` / `fleet.refused` audit. `AVRONE_FLEET_REQUIRE_ALLOW=1` makes an empty allowlist refuse everyone. Outbound calls pre-check the same rules.
- **Privileged tier (deny by default)** — `priv:shell` / `priv:admin` requests only pass when the target has `AVRONE_FLEET_PRIVILEGED=allow` (exact) AND lists the sender in `AVRONE_PRIVILEGED_NODES`, within `AVRONE_FLEET_PRIVILEGED_MAX_HOPS` (default 1, direct only). The flag lives inside the HMAC-signed body; an unsigned header or in-transit edit is refused. Today it marks the run (`core.fleet_privileged`); no tool consumes it yet.
- **Evolve** — `evolve_start` / `evolve_cancel` background loops (same 10-iteration / 1800 s caps, max 2 concurrent, cancel-file survives restarts, deny re-checked between iterations, orphans show `interrupted`). `evolve_skill_propose` writes a new `skills/<name>/SKILL.md` only through the throwaway-clone + full-suite gate, confined to `skills/`, 16 KB cap, no overwrites.
- **Tooling** — deep `rounds` (clamped 1..5) on `/api/chat`, MCP `avrone_deep`, CLI and fleet remote deep. mcp_client, netdiag, agentkit to 100% coverage.
- **Hosting** — `avrone-cli doctor [--json PATH] [--strict]`: offline preflight with insecure-combo warnings (exposed shell, weak/placeholder/reused tokens, open fleet allowlist, privileged tier, cleartext fleet links). Dockerfile runs as uid 10001 with healthcheck; compose adds `cap_drop: ALL`, `no-new-privileges`, read-only rootfs, sandbox network off, and **publishes on 127.0.0.1 by default** (`AVRONE_PUBLISH_ADDR=0.0.0.0` for LAN). Fixed compose `.env` host/data_dir bugs. bootstrap.py 100%.

## v0.14: Squad Recon round — self-scan, hardened recon, evolve loop, multi-round deep — 767 tests, coverage 88%

Four lanes in parallel (git worktrees, two commits each, full suite green per commit), Lead merge + fix:

- **Introspect** — `self_scan` (read-only inventory of the agent's OWN host: OS, paths, runner, toggles, tools, interfaces; secret-shaped env values shown only as `***set***`/`unset`) and `capabilities_report` (plain-English CAN/CANNOT ledger).
- **Recon hardening** — one shared gate for all six recon/OSINT tools: kill switch -> target syntax -> `_reject_private` -> allowlist. Refuses IP literals (all encodings), CIDR/lists/wildcards, loopback/private/link-local/CGNAT/reserved, cloud-metadata hosts, internal TLDs, nip.io-style embedded private IPs; resolves first and connects only to the vetted public address; re-checks every redirect. `AVRONE_RECON` parsed strictly (unknown = off). Fixed cert_transparency suffix match (evilexample.com). Every refusal audited as `recon.refused`.
- **Self-evolve loop** — `evolve_loop` / `evolve_status`: up to N propose->full-suite iterations, stops on first pass, hard caps 10 iterations / 1800 s (`AVRONE_EVOLVE_MAX_ITERS`, `AVRONE_EVOLVE_TIME_BUDGET` can only lower). Still throwaway worktrees only, never touches main, never pushes.
- **Deep search** — multi-round `deep()` (`rounds=`, `AVRONE_DEEP_ROUNDS`, `avrone-cli deep --rounds`, cap 5), early stop on convergence (no new corrections), reviewer crash/junk/zero handling, `## Deep rounds` footer with confidence.
- **Lead fix** — `JobQueue`: closing while a cancelled claim was still inside SQLite could segfault the test run (~15% of runs). Thread lock + closed flag; regression tests.

## v0.13: Squad round (4 parallel lanes, Lead merge) — coverage 78% -> 87%, 379 tests

Four agents worked in separate branches; each landed two commits that passed the full suite, then the Lead merged and re-ran everything.

- **Coverage lane:** `test_fleet_edges.py` (fleet.py 48% -> 100%: HMAC failures, replayed nonce, stale timestamps, hop limits, allowlist, node-id collision, status ageing, routing errors) and `test_server_edges.py` (server.py 61% -> 99%, tools.py and sovereign.py -> 100%). Fixes: signed fleet requests with a malformed or non-object JSON body now return 400 (they returned 500), and `GET /api/runs/{id}` for an unknown run now returns 404 (it returned 200 `[]`).
- **Tools lane:** new `diff_files` (unified diff of two workspace files or a file vs text; path-confined, binary and size checks, capped output) and `codex_search` (read-only search of the Eternal CodeX logs by kind, agent, tool, substring or run id).
- **Skills lane:** hardened skill loader (one empty SKILL.md no longer hides every skill; real frontmatter parser; path-traversal, symlink and bad-name refusal; folder skills win over flat duplicates; sorted, stable `skill_list`). New playbooks `test-coverage-sweep` and `incident-triage`, plus a catalog test that every skill parses and names only real tools.
- **Robustness lane:** a crashing or hung worker no longer sinks a squad (failures become notes, the Lead merges the rest; optional `AVRONE_AGENT_TIMEOUT`, default off); junk planner output is normalised. Codex skips truncated or corrupt JSONL lines instead of 500ing a run log, closes file handles, and locks concurrent writes.
- Lead: FastAPI app version now tracks the package version.

## v0.12: Hardening + coverage round (66% -> 78%)

- **Security fix:** `AVRONE_RECON_ALLOWLIST` matched bare suffixes, so `evilmysite.com` passed an allowlist of `mysite.com`. Now only the exact host or a true subdomain (`*.mysite.com`) passes; a leading-dot entry (`.partner.org`) works too.
- `port_scan` validates ports (1-65535) and de-duplicates them before probing.
- MCP server clamps `squad_size` to its advertised 1-8 schema and reports the real package version.
- New offline suites: `test_cli.py` (0% -> 99%), `test_mcp_server.py` (0% -> 80%, full stdio JSON-RPC loop), `test_llm_legacy.py` (45% -> 100%), `test_recon_offline.py` (44% -> 97%). The old allowlist test no longer touches the network.

## v0.11: OSINT aggregation & metrics

- `osint_domain` — aggregate one domain's public footprint (DNS + TLS + HTTP + certificate-transparency subdomains) into a single structured profile. Public data, one domain per call, no private-individual targeting.
- `osint_metrics` / `GET /api/osint/metrics` — roll up recorded recon/OSINT activity from the action logs: counts per tool and top targets. Data-driven view of what the fleet has probed.
- Same guardrails as recon: AVRONE_RECON / AVRONE_RECON_ALLOWLIST, every call logged.

## v0.10: Advanced reconnaissance (bounded, authorized)

Deeper recon on hosts/domains you name, one target per call, public data or a direct connection only:
- `tls_inspect` — a host's TLS cert: issuer, validity, every SAN hostname.
- `http_fingerprint` — HTTP response headers (server/tech hints) and which security headers are missing.
- `cert_transparency` — a domain's subdomains from public certificate-transparency logs (crt.sh).
- `dns_enum` — A/AAAA/MX/NS/TXT/CNAME/SOA via DNS-over-HTTPS.
- `port_scan` — TCP-connect scan of a small port set on ONE host, and **only** with `authorized=true`; scanning third-party hosts is refused.

Guardrails: one target per call, no sweeps or ranges, `AVRONE_RECON=allow|deny`, optional `AVRONE_RECON_ALLOWLIST`, every call logged to Codex. No access to private infrastructure, satellites, towers, or any capability AVRONE does not actually have.

## v0.9: Self-evolution (AVRONE edits its own code, safely)

- Agents can propose changes to AVRONE's **own** codebase with `evolve_propose` ({goal, edits:[{path, content}]}).
- Each proposal is applied in a **throwaway clone** of the repo and the **full test suite is run**. It is kept only if every test passes; a failing proposal is discarded with its test output so the agent can retry.
- Nothing is pushed and **nothing touches `main`** or the live tree. An accepted change lands on an `evolve/<id>` branch in the sandbox and is surfaced as a diff (`evolve_diff`) for a human to review and merge.
- Paths that escape the repo are rejected. Tools: `evolve_propose`, `evolve_diff`, `evolve_list`, `evolve_reset`. `GET /api/evolve` and `avrone-cli evolve` list accepted proposals.
- Config: `AVRONE_SELF_EVOLVE=allow`(default)`|deny`, `AVRONE_REPO_DIR`, `AVRONE_EVOLVE_TEST_CMD` (default `python -m pytest -q`).

## v0.8: Agent kit (tooling, skills, resources, tasks)

- **Vision tools:** `image_inspect` (size, entropy, colors, estimated barcode grid) and `image_decode` (QR, DataMatrix, Aztec, PDF417, 1D at several scales). `pip install -e ".[vision]"`.
- **Skills:** markdown playbooks in `skills/` (`skill_list`, `skill_load`). Ships with barcode-forensics, network-mapping and self-heal-environment. Add your own as `skills/<name>/SKILL.md`.
- **Self-changes, logged:** `env_install` (pip, or apt with sudo fallback). Every install, including ones run through `shell`, goes to the resource ledger (`resources` tool, `env.change` events). Policy `AVRONE_SELF_CHANGES=allow` (default) | `log` | `deny`.
- **Task board:** `task_board` lets a squad split work into subtasks with owners and status, shared across the run.
- Agents are told to load fitting skills first, track multi-part work on the board, and self-heal missing tools.

## v0.7: Gateway and model fallback

AVRONE routes model calls across several providers so agents keep working when one throttles or fails.

- Set `AVRONE_LLM_FALLBACKS=openrouter:x-ai/grok-4,ollama:llama3.1` (each entry is `provider[:model][|lead_model]`). Each provider's key comes from its usual env var.
- On a 429, 5xx or network error the gateway retries the primary, then moves to the next healthy provider; results note the route taken.
- A circuit breaker opens a provider after repeated failures (`AVRONE_BREAKER_THRESHOLD`, default 3) for a cooldown (`AVRONE_BREAKER_COOLDOWN`, default 30s), so a throttled provider is skipped instead of hammered.
- Lead and worker models are chosen per provider.
- `GET /api/gateway` shows each upstream's state, calls, errors and the last route.

## v0.6: Fleet directory, agent messaging, durable queue

- **Where every agent is:** each node reports its device (hostname, OS, arch), runner and container, network interfaces and addresses, and services (URL, tools, MCP servers, model), plus every agent with its role, status and task. Routers fold in their children, so Main AVRONE sees the whole tree. `GET /api/fleet/directory` (admin), the Map tab, and the `fleet_agents` tool.
- **Tell any agent to build:** `agent_message` tool or `POST /api/fleet/tell` with `{"agent": "router/edge/Builder-3", "message": "..."}`. A live agent gets it in its inbox and acts on its next step inside its own container; a finished one is respawned on that node. Short names work when unique.
- **Durable job queue:** SQLite (WAL) per node, atomic claims, retries with backoff, dead-letter after max attempts, crash recovery via leases. Tools `queue_submit` / `queue_status`, `GET /api/fleet/jobs`.

## v0.5: Fleet (remote routers and servers)

Link AVRONE servers into a tree. Any server can be a node, a router (a node with
children) or both, and agents on every node can spawn agents on the others.

- **Join:** set `AVRONE_FLEET_TOKEN` (same on all nodes), `AVRONE_NODE_ID`, `AVRONE_PUBLIC_URL`, and `AVRONE_FLEET_PARENT` to the router or root it reports to. It registers and heartbeats; status shows online, degraded or offline.
- **Agent tools:** `fleet_nodes`, `remote_spawn` (target a node, a route like `router1/edge7`, or `auto` for least loaded) and `remote_broadcast` (same task on up to 8 nodes in parallel). Remote agents get the same tools, so they spawn further down the tree.
- **Routing:** jobs are forwarded hop by hop through routers; results return the full path.
- **Security:** every fleet request is HMAC-SHA256 signed (method, path, timestamp, nonce, body hash), 60 s window, replayed nonces rejected, hop limit (`AVRONE_FLEET_MAX_HOPS`, default 3), optional node allowlist (`AVRONE_FLEET_ALLOW`), node-id collision checks. Use HTTPS or a private mesh for confidentiality.
- **Visibility:** `GET /api/fleet` (admin) and the Map tab show the node tree.
- Example: `examples/fleet-compose.yml` (root, router, edge).

## v0.4: Network diagnostics tools

Real, bounded networking from AVRONE's own host (one host and port per call, no port sweeping):

- `dns_lookup`: A/AAAA/MX/TXT/NS/CNAME/SOA/PTR/CAA/SRV via the system resolver plus DNS-over-HTTPS
- `tcp_ping`: TCP connect latency to host:port, no root or ICMP needed
- `traceroute`: hop-by-hop route via mtr, traceroute or tracepath (bundled in the Docker image)
- `rdap_whois`: registrar, dates, nameservers or IP network owner via RDAP
- `ip_geo`: approximate city, ASN and ISP for a public IP or hostname (blank = AVRONE's own)

Ask a squad things like "trace the route to github.com and tell me who runs each network".

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env          # Ollama + qwen2.5:3b by default, or set your own provider
avrone                        # http://127.0.0.1:8710
```

### Free open-source models (Ollama)

AVRONE is provider-agnostic (OpenAI-compatible API), so the free route is one
env change — no code changes:

```bash
curl -fsSL https://ollama.com/install.sh | sh   # or brew install ollama
ollama pull qwen2.5:3b
cp .env.example .env                            # AVRONE_PROVIDER=ollama is already set
avrone
```

**Why qwen2.5:3b is the default:** it was picked by a live, no-mock benchmark
(`tools/model_bench.py`), not by guesswork. The bench scores each candidate on
exactly what AVRONE needs, against a real Ollama server with the `shell` tool
really executing:

| Stage | Points | What it proves |
| --- | --- | --- |
| JSON actions | 30 | a valid tool/final action every turn (worker protocol) |
| Tool use | 25 | really calls the tool instead of guessing (15) + correct computed value (10) |
| Squad | 35 | planner JSON (10) + both workers finish (10) + Lead merge non-weak (15) |
| Deep wave | 10 | recursive review: 2 reviewers + Deep Lead report with all sections |

Results (2 vCPU, no GPU): **qwen2.5:3b scored 100/100** — every turn valid,
correct tool calls, full squad with a merged answer. The bench survives server
drops mid-run (health-wait + retries), isolates models so one failure never
kills the run, and writes results incrementally.

Run it yourself, or let CI run it — the `model-bench` workflow (weekly, or on
demand from the Actions tab) installs Ollama on the runner, pulls the
candidates, and uploads `bench_results.json`:

```bash
python3 tools/model_bench.py --base http://127.0.0.1:11434
```

On a GPU host, upgrade in one env line (`AVRONE_LLM_MODEL=qwen3.5:9b`): the
Qwen family leads open-weight tool-calling benchmarks (BFCL v4 / TAU2-bench),
so bigger Qwen models only get better at orchestration.

### Full configuration (the final product profile)

`deploy/full.env` turns on everything AVRONE ships — the maximal, most
configured profile:

```bash
cp deploy/full.env .env    # then fill the TODO placeholders (admin/fleet tokens)
avrone-cli doctor          # preflight: warns on insecure combos before you boot
avrone                     # squad + deep waves + fleet + self-evolve + recon
```

On in the full profile: deep squad of 6, fallback chain with circuit breaker,
Docker runner with network, admin terminal, plugins + MCP + search, fleet node,
skills, self-changes, self-evolution (test-gated), authorized recon. Review
`deploy/full.env` line by line — every switch is documented inline.

### Connect OpenClaw

1. Install and run OpenClaw (the macOS app, or `npm i -g openclaw` on Linux).
2. Turn on its OpenAI-compatible endpoint (off by default). Merge
   `deploy/openclaw.json5` into `~/.openclaw/openclaw.json`, set a token, restart the gateway.
3. Put that token in `AVRONE_LLM_API_KEY`. Default URL is `http://127.0.0.1:18789/v1`.

Each AVRONE agent gets its own OpenClaw session (`x-openclaw-session-key`), so
squad members don't share context.

### Run in Docker

```bash
cp .env.example .env
docker compose up --build
```

AVRONE launches its agent environment as a sibling container through the host
Docker socket, with the workspace bind-mounted.

## API

```bash
# Squad (Grok Heavy style)
curl -s localhost:8710/api/chat -H 'content-type: application/json' \
  -d '{"message":"Build a FastAPI todo app in the workspace and test it","mode":"squad","squad_size":4}'

# Long-running job: returns immediately, watch it on the HUD
curl -s localhost:8710/api/chat -H 'content-type: application/json' \
  -d '{"message":"...","background":true}'

curl -s localhost:8710/api/runs              # past runs
curl -s localhost:8710/api/runs/<run_id>     # full action log
curl -s localhost:8710/api/terminal -H "X-Admin-Token: $AVRONE_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"cmd":"ls -la"}'
# Live events: ws://localhost:8710/ws/events
```

## Safety

Agents run real commands. Prefer the Docker runner, keep `AVRONE_HOST=127.0.0.1`
unless you put auth in front of it, set `AVRONE_ALLOW_NETWORK=false` or
`AVRONE_AGENTS_CAN_SHELL=false` to tighten things, and leave `AVRONE_ADMIN_TOKEN`
empty to turn the admin terminal off.

## Tests

```bash
pytest -q     # runs fully offline with the mock model and local runner
```

## System bridge (Grok Bot)

AVRONE concludes into one system with its Node twin, [Grok Bot](https://github.com/doitupsolutions810-crow/grok-bot-):

- **Inbound:** Grok Bot agents call this AVRONE's plain `/api/chat` through their `avrone.squad` tool (`AVRONE_URL`, e.g. `http://host.docker.internal:8710`), running full squad/deep modes here.
- **Outbound:** AVRONE agents reach Grok Bot's terminal, browser and team via the built-in `http_request` tool — e.g. `http_request POST http://localhost:3000/api/chat {"message": "...", "mode": "squad"}`.
- **MCP, both ways:** `avrone-mcp` serves this AVRONE to any MCP client (Grok Bot handshakes MCP servers at boot); `mcp.json` loads external tools into AVRONE.

Grok Bot's `launch.sh` boots both systems in sequence and runs their live tests; `./verify.sh` proves the combined boot locally (verdict in Grok Bot's `data/VERIFY.md`).


## Roadmap

- Native iOS/Android shell on the same API
- Self-hosted runners pool (multiple environments, one per run)
- Model hosting environment (Ollama/vLLM container managed by Sovereign OS)
- Persistent agent memory in Eternal CodeX

