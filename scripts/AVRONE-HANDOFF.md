# AVRONE — Operator Handoff

You are taking over an autonomous, self-evolving AI system called **AVRONE**. You may have
zero prior knowledge of this project. This document plus `mac-avrone-autopilot.sh` is
everything you need to find the system, configure it and its bridges, install the right
model, and continue the work **exactly where it was left off**. Read this top to bottom
before doing anything.

---

## 1. What AVRONE is

AVRONE is a Python (FastAPI) multi-agent platform that can **edit its own source code**
safely. An agent proposes code changes; a sandboxed harness (`avrone/evolve.py`) applies
them in a throwaway git clone, runs the full test suite, and **keeps the change only if
every test passes** — otherwise it hands back the failing output to try again. Nothing ever
touches `main` or the live tree automatically; accepted work lands on `evolve/<id>`
branches for a human to review and merge.

Architecture (the three layers from the original design):

- **Eternal CodeX** — `avrone/codex.py`: an event bus + append-only JSONL run logs. Every
  tool call, agent step and decision is recorded here. This is AVRONE's memory of what it did.
- **Trinity Core** — `avrone/trinity.py`: the kernel. Planner → parallel **squad** of worker
  agents → a **Lead** that cross-checks and merges. Runs the JSON tool loop and spawns
  sub-agents.
- **Ghost Shell** — `avrone/server.py` + `static/index.html`: REST API, a WebSocket HUD, and
  an admin terminal.

Supporting modules you will touch: `sovereign.py` (Docker/local runner), `gateway.py`
(multi-provider LLM failover + circuit breaker), `fleet.py` / `presence.py` (multi-node
mesh), and the **bridges** (see §3).

The full, authoritative module-by-module history is in `README.md` and in the test suite
under `tests/` (~2,300+ tests). When in doubt, read the code and the tests — never guess a
path or an API.

## 2. The mission (what "continue the work" means)

Two tracks are in flight. Your job is to keep both moving and leave reviewable results.

**Track A — tool reintroduction.** AVRONE has **75 tools**. On weak/local models the full
registry overwhelms a small context, so we trim it with `AVRONE_TOOLS_ONLY` and re-add tools
in **9 staged batches**, smoke-testing each batch before admitting it. The plan is
`scripts/avrone-state/reintro-plan.json`; the current stage is in
`scripts/avrone-state/reintro-stage.txt`.

> **Left off at: stage 7 of 9 — 58 of 75 tools active.** Stages 0–6 passed. Stage 7 (the
> `spawn / fleet_status / bridge_post / bridge_read / bridge_reply / codex_search /
> codex_stats` batch) kept **holding** on the small 7B model because it emitted malformed or
> empty calls. A capable model (see §4) should clear stages 7, 8 and 9 and reach the full
> 75-tool registry.

**Track B — self-evolution squad.** Run parallel evolve lanes. Each lane reads a real
module, finds a genuinely uncovered branch or a small useful new tool, and submits
`evolve_propose {goal, edits:[{path, content}]}`. The harness gate-tests it in isolation and
accepts only all-green changes onto an `evolve/` branch.

> **Left off at: 0 accepted proposals.** The 7B model made many calls but landed **nothing**
> — it is too weak. This is the single biggest reason to move to a bigger model. The gate
> itself is healthy: it has correctly accepted good changes and rejected broken ones in past
> rounds on stronger models.

**Definition of done for a cycle:** reintro stage reaches 9/9 (75 tools), and the evolve
lanes land at least a few **accepted** proposals on `evolve/` branches, summarized in
`MERGE_ME.md` for the human to review. **Never merge to `main` yourself** and **never push
without the human's go-ahead.**

## 3. The bridges — find and configure them coherently

"Bridges" in AVRONE means the connectors that let parts of the system talk. Configure all of
them before running; the autopilot script does this and self-checks it:

1. **LLM/provider bridge** (`avrone/gateway.py` + `avrone/llm.py`): AVRONE → the model. Here
   it points at **local Ollama** (`AVRONE_PROVIDER=ollama`,
   `AVRONE_LLM_BASE_URL=http://127.0.0.1:11434/v1`). No cloud, no API key, no cost. Verify
   Ollama answers `/api/version` and that the model is pulled.
2. **Learning bridge** (`avrone/bridge.py`): a run-scoped blackboard where every agent in a
   run (squad workers, their sub-agents, deep reviewers) posts findings/questions/answers/
   conclusions and reads each other's. Persisted as JSONL under `<data_dir>/bridge/<run_id>`.
   Tools: `bridge_post`, `bridge_read`, `bridge_reply`. These are part of stage-7 reintro.
3. **Fleet bridge** (`avrone/fleet.py` + `presence.py`): HMAC-signed node-to-node mesh for
   spawning/relaying across machines. Single-machine autopilot keeps this local
   (`AVRONE_RUNNER=local`); no remote nodes are required.
4. **CodeX bridge** (`avrone/codex.py`): the event/log bus every run writes to; `codex_search`
   / `codex_stats` read it (also stage-7 reintro).

Coherent config = one env block (the autopilot writes it to `avrone-state/env.sh`) with the
provider pointed at local Ollama, data dir on persistent storage, repo/workspace/skills/
plugins dirs all pointing at this checkout, self-change and self-evolve **allowed**, and
recon kept **default-deny** except `127.0.0.1` (guardrail: `port_scan` on localhost only).
Run `python -m avrone.cli doctor` to self-check the wiring before the loop starts.

## 4. The model — install the bigger one

The small `qwen2.5-coder:7b` is the proven bottleneck. On this Mac (Apple Silicon, ~128 GB)
install a frontier-class **free, local** model via Ollama. Default and recommendation:

- **`gpt-oss:120b`** — OpenAI's open-weight 120B (MXFP4, ~65 GB), strong tool-calling and
  reasoning, fits comfortably in 128 GB. This is the default the autopilot pulls.
- Alternative if you prefer a coder-specialist: `qwen3-coder-next:q4_K_M` (~52 GB).

Always run Ollama with a large context so the 75-tool prompt is never truncated
(`OLLAMA_CONTEXT_LENGTH=32768`). Everything stays on-device; there is no paid cloud.

## 5. Guardrails (do not cross)

- Self-change/self-evolve are allowed **because the owner (Crow) approved agents changing
  their own sandbox**, but accepted code lands only on `evolve/` branches, never `main`.
- Recon/OSINT is **default-deny**; the only allowlisted target is `127.0.0.1`, and
  `port_scan` requires `authorized=true` with ≤5 ports on localhost. Do not scan anything
  else.
- Do not delete committed source. `agents/` in the repo is AVRONE's runtime per-agent home
  (recreated every run). The autopilot keeps it and hides it from git via `.git/info/exclude`;
  never delete it.
- Everything is logged (CodeX + resources ledger). Keep it that way.

## 6. How to run

```bash
# one command, from the repo root, with your external drive mounted:
DRIVE=/Volumes/AI ./scripts/mac-avrone-autopilot.sh
```

It will: locate/validate the checkout → apply the two carried patches if the code lacks them
(see §7) → install/start Ollama with big context → pull the model → install AVRONE and run
the **baseline gate** (must be green before anything evolves) → write and self-check the
coherent env + bridges → then **loop**: advance the reintro stage and run the evolve squad
lanes, writing results to `$DRIVE/avrone-runs/` and accepted proposals to `MERGE_ME.md`.

Exit codes: 0 done, 1 config/precondition error (message says what), 2 another autopilot already
running on that drive, 130/143 interrupted (lock released, child processes stopped). It is safe to
rerun at any time: state resumes from the drive, corrupt state files are reseeded, a stale lock is
recovered, and Ollama is restarted if it dies (gives up after 3 restarts).

More resilience: state is re-validated before **every** cycle (a stage file deleted or corrupted
mid-run is restored from `reintro-stage.txt.bak`, written on each advance). A hanging lane is killed
at `LANE_TIMEOUT` and a crashing lane is logged; the loop continues either way. It refuses to apply
patches over uncommitted edits to tracked files (your work is never touched). The Mac is kept awake
with `caffeinate`, closing the terminal (SIGHUP) does not stop it, and it warns when RAM is too small
for the chosen model. The stage-advance rule lives in `scripts/avrone-state/smoke_judge.py`
(>=60% of the batch called, <=3 malformed/unknown calls).

Hostile-environment guards: it stops cleanly (exit 1, state intact) if the drive is unplugged or
swapped mid-run and never recreates the mount point; refuses `DRIVE=/`, your home folder, or a path
inside the repo; refuses to run as root (`ALLOW_ROOT=1` to force); rejects shell metacharacters in
`MODEL`, `LANES`, `OLLAMA_HOST` (they are written into env.sh); restarts its own hung Ollama but
refuses to fight a foreign hung listener on the port; times out a frozen warm-up (`WARM_TIMEOUT`,
default 900s); and re-pulls the model if it is deleted mid-run.

Long-run hygiene: a second autopilot on the same repo (even from another drive) exits 2; a stale
git `index.lock` from a crashed git is cleared (refused if git is actually running); timeouts and
Ctrl-C/kill take down the whole process tree (no orphaned pytest clones eating RAM), and TERM/INT
act immediately even mid-gate or mid-smoke; old run folders are pruned (`KEEP_RUNS`, default 30,
0 = keep all); it stops with state saved when the drive drops under `MIN_RUN_FREE_GB` (default 5);
env.sh is shell-escaped so odd folder names ($, quotes, backticks) source safely.

Useful switches: `MODEL=gpt-oss:120b` (default), `ONCE=1` (one cycle then stop),
`CYCLES=N` (stop after N cycles; default: loop forever), `SKIP_GATE=1` (not recommended),
`LANES="deepcov newmods tools robust"`.

## 7. Carried patches (why they're here)

Two improvements were made off-cloud and are **not yet on GitHub**:

- `0001 — AVRONE_TOOLS_ONLY`: the registry-trimming used by reintro.
- `0002 — trinity.coerce_args`: repairs flattened/aliased tool args from small models
  (`command→cmd`, `file_path→path`, `text→content`, …).

They live in `scripts/avrone-state/patches/`. The autopilot applies them only if the running
code doesn't already contain them (idempotent; it checks for the symbols first). If you
reconnect GitHub and push `main`, a fresh clone will already have them and the patch step
becomes a no-op.

## 8. When you're done

Leave the system with: the highest reintro stage you reached, the baseline + lane logs under
`$DRIVE/avrone-runs/<timestamp>/`, and `MERGE_ME.md` listing each accepted proposal id with
its goal and branch. Report that summary back to Crow for review and merge. Do not push or
merge without his word.

## Round 5 hardening (macOS bash 3.2, launch quirks, fuzzing)
- Verified on GNU bash 3.2.57 (the /bin/bash every Mac ships) as well as bash 5: the full 202-test edge suite passes on both.
- `sh mac-avrone-autopilot.sh` re-execs under bash; running it through a symlink (e.g. in /usr/local/bin) still finds the real repo.
- Closed stdin/stdout/stderr (launchd, cron, `>&-`) are pointed at /dev/null first; previously a closed stdout made the model look missing and re-pulled it.
- SIGHUP or SIGUSR1 during a lane pair no longer cuts the wait short (bash `wait` returns early on any trapped signal). `kill -USR1 <pid>` logs a status line (cycle, phase, stage, lanes). SIGQUIT cleans up like SIGTERM (exit 131).
- Symlinked state files on the drive are removed, never written through. A multi-line stage file ("6\n7") is treated as corrupt instead of read as 67; leading zeros are normalized.
- Fuzzed: 300 random smoke logs, unicode tool names, a 20 MB stage file, a JSON recursion bomb, random binary plan files, corrupt patch files, 500+ character drive paths.

## Round 6 hardening (developer Mac, exFAT drive, DST, bad settings)
- Global git settings no longer break unattended commits: commit/tag signing, a global hooksPath with failing hooks, and autocrlf are overridden via GIT_CONFIG_COUNT for the autopilot and every child git, including AVRONE's evolve commits. Stray GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE are ignored.
- Hostile Python/pytest env is scrubbed: PYTHONHOME, PYTHONPATH (including a shadow `avrone` package), PYTHONWARNINGS=error, PYTHONOPTIMIZE, PYTEST_ADDOPTS=--pdb, PIP_USER. Conda/venv leftovers and a bogus TZ are harmless.
- macOS AppleDouble `._*` files (from exFAT/FAT drives) are never fed to `git am`. Spotlight, Trashes, and fseventsd folders on the drive are harmless.
- Old runs are pruned by age, not name, so the repeated hour at the DST fall-back can't delete the newest run.
- LANE_TIMEOUT, SMOKE_TIMEOUT, CYCLE_SLEEP, MIN_FREE_GB, MIN_RUN_FREE_GB, and KEEP_RUNS must be whole numbers; anything else fails before Ollama starts, instead of hours in.
- MODEL validation uses explicit ASCII so old bash in a UTF-8 locale can't let accented letters through.

## Round 7 hardening (port owners, model tags, unfinished git work, launchd env)
- A different program on the Ollama port (a dev server, an AirPlay-style 403, an empty 500) is detected as "not Ollama" and the run stops with advice to set OLLAMA_HOST. Before this fix, any HTTP reply counted as Ollama.
- MODEL=gpt-oss now matches `gpt-oss:latest` in `ollama list`, so it no longer re-pulls a model that's already there. Other tags (`:7b`, `:120b-q8`) still don't count as a match.
- If you're in the middle of a merge, cherry-pick, revert, rebase, or bisect, the run refuses to start and changes nothing.
- On a detached HEAD, the carried patches go onto a new `avrone-autopilot` branch instead of a commit that would get lost. Shallow clones and `git worktree` checkouts work.
- Login/cron style starts: Homebrew bin folders are added to a stripped PATH. A missing HOME falls back to `$DRIVE/avrone-home`. A dead or read-only TMPDIR is dropped. A umask that blocks the owner is reset to 022.

## Round 8 hardening (when Ollama itself misbehaves)
- **If the Ollama app is already running:** the autopilot uses it but never downloads into it, since the app keeps models in ~/.ollama on the internal disk, not on $DRIVE. If the model is missing there, the run stops and tells you to quit the app. `REUSE_OLLAMA=1` overrides that.
- **Context:** an Ollama the autopilot didn't start never got OLLAMA_CONTEXT_LENGTH. So the context is baked into a derived model, `avrone-<model>:ctx<CTX>` (FROM model + PARAMETER num_ctx), and AVRONE uses that. If that fails, the run warns and uses the base model.
- **Startup race:** if the Ollama app grabs the port while our `ollama serve` is starting, ours exits. The autopilot detects this and treats the winner as not ours, instead of assuming it started it.
- **Hung `ollama list`:** it now has a deadline (LIST_TIMEOUT, default 60s).
- **Pull failures are classified.** "Needs newer Ollama", "model not found" and "disk full" stop right away with the fix. Network errors retry 3 times, then say the network looks down. A pull that exits 0 without the model showing up is counted as a failure. Pull progress goes to `$OUT/pull.log`.

## Round 9 hardening (listings that lie, offline reruns)
- **A failed `ollama list` no longer means "model missing".** It retries 3 times, then stops, so it never starts a 65 GB re-download because of a server hiccup.
- **The listing header is skipped by its content, not its position.** A listing with no header can't hide the first model.
- **LIST_TIMEOUT and WARM_TIMEOUT are validated** as whole numbers ≥ 1, the same as the other settings.
- **Offline reruns:** if pip fails but AVRONE, pytest and fastapi are already installed, the run warns and continues. On a first run with nothing installed, it stops and says to connect to the internet.
- **venv creation:** if the first python ≥ 3.11 can't create a venv (a stripped build), the others are tried. A .venv whose python link was broken by `brew upgrade` gets rebuilt.

## Round 10 hardening (honest numbers, a report that can't hang, a tidy drive)
- **Lane numbers are real now.** `proposals=` and `accepted=` come from the proposal records each lane wrote this run, not from grepping its log, which also held the task text ("Land TWO accepted"), files the agent read, and its narration. Old records and broken files aren't counted.
- **The report can't freeze the loop.** Each lane's listing has a REPORT_TIMEOUT (default 120s); a stuck one is noted in MERGE_ME.md and the loop moves on.
- **MERGE_ME.md is written to the run folder first.** If the repo copy is blocked (a folder with that name, a read-only repo), you get a warning and the loop keeps going.
- **Deleting the run folder mid-run is survivable.** It's recreated, but only while the drive is still mounted.
- **ollama.log is rotated** once it passes OLLAMA_LOG_MAX_MB (default 50) when the autopilot starts Ollama, so weeks of request logs can't fill the drive. One old copy is kept as ollama.log.1.
- **REPORT_TIMEOUT and OLLAMA_LOG_MAX_MB are validated** before they ever reach shell arithmetic, where text like `a[$(cmd)]` would otherwise run.

## Round 11 hardening (crash loops, the repo changing mid-run)
- **The run stops if AVRONE crashes on every start.** When every agent run in a cycle exits with an error (timeouts don't count), CRASH_LIMIT cycles in a row (default 3), the autopilot stops. It prints the last error line, for example `ModuleNotFoundError: No module named 'x'` after a `git pull` added a dependency. Rerun to reinstall. Partial failures, such as a lane crashing while the smoke works, never trip it.
- **Pulling or checking out in the repo mid-run is noticed.** The autopilot logs that HEAD moved and lanes test the new code. If the carried patches are gone, it stops instead of quietly giving every lane all tools. Rerun to re-apply them.
- **CRASH_LIMIT is validated** like every other numeric setting.

## Round 12 hardening (smoke judge honesty, stalled reintro)
- **The judge reads only real event lines.** A stage now advances only on genuine `[run] tool.call agent tool` lines. Before, text like "tool.call x port_scan" inside a tool's output (an agent cat-ing a log or test file, or echoing it) or inside the agent's own narration counted as a call. Lane `calls=` uses the same rule.
- **Unknown-tool and malformed counts ignore narration.** An agent saying "no unknown tool errors" no longer counts against the batch. Real `ERROR: unknown tool` results and `action.malformed` events still count.
- **The judge flags model server errors.** When a batch HOLDs and the log shows `Model backend error 5xx` (for example Ollama failing to parse a gpt-oss tool call), the judge prints a HINT that the model or server is failing, not the tools.
- **A stalled reintro is reported.** If the same stage HOLDs for STALL_CYCLES cycles (default 5), the log warns with the batch and the smoke log to read, and MERGE_ME.md shows `STUCK for N cycles`. Any progress resets the count. STALL_CYCLES is validated.

## Round 13 hardening (gpt-oss tool-call 500s)
- **AVRONE gateway, `avrone/gateway.py`:** gpt-oss on Ollama sometimes answers HTTP 500 "error parsing tool call", when the model writes into its native tool channel. AVRONE never uses native tools, so it now detects that error and sends the retry with a plain-text instruction folded into the system prompt. It never mutates the caller's messages and adds the instruction only once. Other 500s retry exactly as before. Tests: tests/test_gateway_toolparse.py.
- **Smoke judge:** the 5xx HINT now also reads the gateway's error spelling (`ollama 500: ...`), not just the legacy `Model backend error 500`. Round 12's hint would have missed real gateway errors.

## Round 14 hardening (gpt-oss reply shapes)
- **Native tool calls with empty content:** Ollama sometimes parses gpt-oss's action into `message.tool_calls` and leaves `content` empty. The gateway now rewrites those calls into AVRONE's JSON action (`{"tool","args"}`, or `{"tools":[...]}` for several). It strips gpt-oss namespaces (`functions.shell`, `repo_browser.shell`, `... to=functions.shell`) and accepts arguments as a string or an object. Unparseable arguments are kept as `{"_raw": ...}`, never dropped. When the reply has text, the text wins.
- **Truncated empty reply:** empty content with `finish_reason=length` means every token went to reasoning. It now fails right away with a plain reason instead of silently returning "" (retrying would truncate the same way). Partial text with `length` is still returned.
- Hardened against a non-dict `message` and non-string `content`.
- **Smoke judge:** a held batch whose log shows `finish_reason=length` gets a HINT to raise CTX. It no longer reads as a tool failure.
- Tests: tests/test_gateway_toolcalls.py (18), plus 2 judge tests. Mutation check: 10 of the 20 fail on round 13. The others are guards.

## Round 15 hardening (action parsing)
- **`parse_action` (avrone/trinity.py) rewritten.** The old parser used one greedy `\{.*\}` regex, so a reply broke whenever its prose had a brace (`run {x} then {"tool":...}`), the model repeated its action, or it showed example args before the action. Each of those counted as `action.malformed`, which HOLDs the smoke and burns evolve cycles. The new parser decodes with `raw_decode` at each `{` and takes the first object that is an action. It accepts any fence tag (`jsonc`, `JSON`, `python` blocks are skipped), and it finds a complete action inside a truncated outer object.
- **Reasoning text:** JSON inside `<think>`/`<thinking>`/`<reasoning>` blocks, and in a leaked gpt-oss harmony `analysis` channel, loses to the answer's own action. It's used only when the answer has none, as before. Harmony control tokens are stripped.
- **Bounded:** at most 4000 decode attempts and 0.3 s, so a pathological reply can't stall an agent. Non-text input returns None instead of raising.
- Tests: tests/test_parse_action_robust.py (23). The existing fuzz, core, and toolcall parser tests still pass. Mutation check: 15 of the 23 fail on round 14.

## Round 16 hardening (action normalizing)
- **`normalize_action` / `normalize_call` (avrone/trinity.py)** run on every parsed action, batch entries included, before the arg checks. They fix JSON that parses but misses AVRONE's format the way gpt-oss and qwen do:
  - `"tool": "functions.shell"` (also `tools.`, `function.`, `repo_browser.` prefixes and stray spaces) becomes `shell`, but only when the bare name is a registered tool.
  - Args under `arguments`, `parameters`, `params`, or `input` move to `args`. A real `args` key still wins.
  - Args sent as a JSON-encoded string are decoded when they decode to an object. Anything else is left for the normal ARG ERROR.
  - A `"final": null` or empty final beside a tool call is dropped, so the tool runs. Before, the run ended with the literal result "None".
  - A lone `{"final": null}` now yields "" rather than "None". A structured final (object, list, or number) keeps its JSON form instead of a Python repr.
  - The caller's dict is never mutated.
- Tests: tests/test_action_normalize.py (26), including real agent-loop runs where the shell tool executes. Mutation check with identity stubs on round 15: 21 of 26 fail, including all 4 loop tests.

## Round 17 hardening (agent-loop guards)
- **Empty turns:** an empty or whitespace reply used to end the agent at once with no result. gpt-oss does this after long tool output, or when all its tokens go to reasoning. Now it gets a nudge, sharing the 2-nudge budget with malformed actions, and logs `action.empty`. Endless empty turns still stop after 3 calls.
- **Non-text replies:** a client returning None, bytes, or a message dict crashed the agent with a TypeError. `reply_text` now turns the reply into text first.
- **Identical-call loops:** the same tool, args, and output 3 times in a row (`REPEAT_CALL_LIMIT`) adds a note to the tool result telling the agent to change approach or finish, and logs `tool.repeat`. Nothing is blocked. The counter resets on any different call.
- **Tool output cap:** `AVRONE_TOOL_OUTPUT_MAX` (default 20000 chars, clamped to 1000–500000) is a central safety net for tools that don't cap their own output, such as MCP and plugins. It keeps the head and the tail, where errors usually are.
- Tests: tests/test_agent_loop_guards.py (22). Mutation on round 16 with stubs: 18 of 22 fail, including every loop-behavior test.

## Round 18 hardening (out of turns, context budget)
- **Out of turns:** the last "give your final result" reply went back raw. A model answering `{"final": "..."}` returned literal JSON, a tool call returned the call itself, and None or empty returned nothing. `out_of_turns_result` unwraps a final (structured finals become JSON), never returns a tool call, and falls back to a clear "[agent ran out of turns (N steps) without a final answer]" message. Logs `agent.out_of_turns`.
- **Context budget:** Ollama silently cuts an over-long prompt from the front, which drops the system prompt with its tool docs and JSON format. `AVRONE_CONTEXT_CHARS` (default 120000, clamped to 8000–2000000) now elides the oldest tool-result bodies before each turn, logging `context.compact`. The system prompt, the first user turn, every assistant turn, operator notes, and the newest 4 messages are never touched.
- Inbox notes that aren't strings no longer crash the loop.
- Tests: tests/test_agent_loop_context.py (22). Mutation on round 17 with stubs: 16 of 22 fail, including all 3 out-of-turns loop runs and the context-budget loop run.

## Round 19 hardening (native tool-call dispatch guard)
- **Cut-off calls:** a tool call whose arguments were cut off by `finish_reason=length` is never run, so a half-written shell command can't execute. Complete calls in the same message still run. If nothing complete is left, the gateway fails fast with "tool call cut off mid-arguments ... nothing ran" and doesn't retry.
- **Junk around the arguments:** leaked harmony tokens (`<|call|>`, `<|end|>`, `<|constrain|>`), trailing text, or an extra brace around otherwise complete arguments are stripped. The first complete JSON object is used.
- **Nameless calls:** a call with no name, whose arguments are themselves `{"tool": ...}` or `{"final": ...}`, is used as that action. Other nameless calls are dropped.
- **`final` as a tool name:** `final`, `final_answer` (any namespace, any case) becomes `{"final": ...}`, taking final/answer/text/content/response/result, or a lone key. When it comes beside a real tool call, the tools run first.
- **Duplicates:** exact duplicate calls in one message run once.
- Unparseable args without truncation still reach the ARG ERROR path (round 14 behavior).
- Tests: tests/test_gateway_toolcall_guard.py (26). Mutation on round 18: 24 of 26 fail.

## Round 20 hardening (reply content shapes)
- **Content parts:** `content_text()` joins OpenAI-style content-part lists (`[{"type":"text","text":...}]`, `output_text`, `{"text":{"value":...}}`, plain strings, a single part dict). Image and audio parts are skipped, and bytes are decoded. Before this, a parts list was `str()`-ed into a Python repr and became the agent's final answer.
- **Prose preamble beside a tool call:** gpt-oss commentary such as "Let me list the files." next to a native tool call no longer ends the agent as a prose final. The tool call wins unless the text itself holds a JSON action (`{`). If the tool calls turn out unusable or cut off, the text is returned as before. **Behavior change:** round 14's `test_text_content_wins_over_tool_calls` now uses JSON text.
- **Refusal:** with empty content and no usable tool call, a non-blank `message.refusal` is returned instead of "".
- **Double-encoded arguments:** tool-call arguments JSON-encoded two or three times are unwrapped. A plain JSON string stays `_raw`.
- Tests: tests/test_gateway_content_shapes.py (33). Mutation on round 19: 21 of 33 fail. The rest are pass-through guards.

## Round 21 hardening (response body shapes and Retry-After)
- **`extract_choice()`** reads a 200 body even when it isn't the plain OpenAI chat shape:
  - legacy completions (`choices[0].text`)
  - a lone streamed chunk (`choices[0].delta`)
  - Ollama's native `/api/chat` body (`{"message": ...}`, where `done_reason` maps to finish_reason, so length still fails fast)
  - a BOM or surrounding whitespace
  - an SSE stream from a proxy that forces streaming. Content deltas and tool-call argument fragments are joined by index, and a cut-off streamed call still never runs.
- **200 with `{"error": ...}`:** the message is surfaced instead of "bad response". Model not found, does not exist, invalid API key, and unauthorized are permanent (no retry, fail over). Anything else is retried with its message. SSE error chunks are handled the same way.
- **Retry-After:** accepts an HTTP-date as well as seconds. A past date gives 0, NaN or junk is ignored, and the delay is still capped at 30 s.
- Unusable bodies (empty choices, HTML, arrays) still give "bad response" and retry, as before.
- Tests: tests/test_gateway_response_shapes.py (36). Mutation on round 20: 19 of 36 fail. The rest are pass-through guards.

## Round 22 hardening (outbound request body). Branch: avrone-a12lr-daemon
- From round 22 on, work goes to the `avrone-a12lr-daemon` branch (Crow, 2026-11-02). `main` stays at round 21 (ae0c2ee).
- **`sanitize_messages()`** runs on every request body (gateway and the legacy `llm.LLM` path):
  - Lone surrogates (bytes decoded with surrogateescape, or a cut emoji) become U+FFFD. Before, httpx raised UnicodeEncodeError locally, the same way on every upstream.
  - NUL bytes are dropped.
  - Content that is None, a number, bytes, a part list, or a dict always becomes a string.
  - Roles are normalised: developer→system, human→user, ai/model/bot→assistant, function→tool, anything unknown→user.
  - A tool-role message with no `tool_call_id` becomes a user "TOOL RESULT:" message, so strict servers don't return 400.
  - Extra keys are dropped. A valid `name` is kept. Junk entries are skipped, and the caller's list is never mutated.
- **`safe_temperature()`**: NaN or inf (which made the JSON invalid) and non-numbers fall back to 0.7. Other values are clamped to 0–2.
- Tests: tests/test_gateway_outbound.py (43). Mutation on round 21: 35 of 43 fail.

## Round 23: endpoint and config values (branch avrone-a12lr-daemon)
- **`normalize_base_url()`** in config.resolve_provider covers both the gateway and the legacy LLM:
  - Strips quotes, whitespace, trailing slashes, and a pasted full `/chat/completions` or `/completions` endpoint. That used to double into a 404.
  - Adds a missing scheme: http:// for local or LAN hosts and :11434, https:// otherwise.
  - For Ollama, a bare `host:11434`, or `/api`, `/api/chat`, or `/api/generate`, becomes `/v1`.
- **`clean_api_key()`**: removes quotes, whitespace, and newlines, plus a leading `Bearer ` (that used to send "Bearer Bearer", a 401). Model names are unquoted.
- **Numeric env vars** (`_int`/`_float`): blank, junk ("30s"), NaN, or inf now fall back to the default with a stderr note. Before, this raised ValueError at import (`settings = Settings()`), so nothing started. Underscores, quotes, and "1e1" are accepted.
- **.env parser** (`parse_dotenv_line`): handles `export KEY=`, a BOM, CRLF, and a quoted value that contains " #". Keys with spaces are rejected. A non-UTF-8 file or a directory named .env no longer crashes.
- **Malformed URL at request time**: httpx.InvalidURL (not an HTTPError, so it used to escape chat() raw) and UnsupportedProtocol now fail over at once with no retry. The legacy client raises LLMError.
- Tests: tests/test_config_endpoint.py (70). Mutation on round 22: 49 of 70 fail.

## Round 24: core read_file / write_file / fetch tools (branch avrone-a12lr-daemon)
- **read_file**:
  - Reads are bounded. It never reads a huge log whole.
  - Truncation is announced with `offset=` paging. Optional `offset` accepts an int or a numeric string. Before, it cut silently at 12000 chars and the model thought the file ended there.
  - Binary files (a NUL in the first 8 KB) get a refusal with a shell hint, instead of NUL soup.
  - A directory returns its entries instead of `IsADirectoryError`. An empty path or a junk offset gets a clear message.
  - A multi-byte character cut at the window edge isn't mangled.
- **write_file**:
  - Dict or list content becomes pretty JSON. Before, the model's .json files were Python reprs like `{'a': 1}`, and the schema rejected them.
  - Lone surrogates become U+FFFD instead of aborting the write.
  - An empty path, a trailing-slash path, or an existing directory gets a clear error.
  - Writes outside agent dirs are atomic (tmp then os.replace) and use explicit UTF-8. A full disk leaves the old file intact.
- **fetch**:
  - The body is streamed and capped at 2 MB.
  - Binary types are summarised, not decoded.
  - URLs wrapped in <>, quotes, or backticks, and uppercase schemes, are cleaned.
  - HTML entities are unescaped.
  - Timeouts, connect errors, bad URLs, and an unknown charset each get a clear message.
- Tests: tests/test_core_file_tools.py (51). Mutation on round 23: 38 of 51 fail.

## Round 25: edit_file and file modes (branch avrone-a12lr-daemon)
- **Data-loss fix:** edit_file used to read with errors="replace" and write the whole file back, so one edit turned every non-UTF-8 byte (in latin-1 or mixed files) into U+FFFD. It now round-trips with surrogateescape. Bytes outside the edit, and any BOM, survive exactly.
- **CRLF files:** an LF `old` or `new` matches and is written back as CRLF. A CRLF `new` isn't doubled.
- **Trailing-whitespace drift:** a line-aligned match that ignores trailing whitespace, used only when the exact match fails. It never splices mid-line, and an ambiguous match is refused.
- **Misses:** a miss names the closest line (difflib) so the model can copy the exact text.
- **Refusals:** binary files, directories, empty paths, and an identical `old`/`new` are refused. `new=None` deletes the text. A lone surrogate becomes U+FFFD.
- **replace_all:** opt-in. Accepts "true", "yes", "1", or "all" as strings.
- **File modes:** writes are atomic, and an overwrite keeps the existing file's mode. Round 24's mkstemp made shared files 0600 and stripped exec bits. agentfs quota_write had the same problem; it now keeps the old mode on overwrite. New shared files get 0666 & ~umask.
- Tests: tests/test_edit_file_robust.py (55, plus 1 skip, run on both legacy and agent-dir contexts). Mutation on round 24: 41 of 55 fail.

## Round 26: shell runner and output view (branch avrone-a12lr-daemon)
- **Background stall:** `server &` (any background child holding stdout) used to block every shell call until the timeout, because `proc.wait()`/`communicate()` wait for the pipes to close. The runner now waits on the process itself, gives the pipes a grace period (`AVRONE_SHELL_PIPE_GRACE`, default 0.5 s, clamped to 0–10), then returns with a note telling the agent to redirect the child's output.
- **Timeout:** output printed before a timeout is kept (it used to be thrown away), and the message says "command timed out after Ns". Each command runs in its own session, so a timeout or cancel kills the whole process group, `cmd &` grandchildren included.
- **stdin:** the server's stdin is no longer inherited. It's /dev/null, so `cat`, `read`, and prompts return immediately instead of hanging.
- **Bounded capture:** each stream keeps the first 1 MB and the last 256 KB, and counts the middle. `yes` can't grow server memory anymore.
- **Exit labels:** `[exit -9 SIGKILL]`; `[exit 137 = killed by SIGKILL, often out of memory]`; 139 shows SIGSEGV.
- **text() view:** keeps the head and the tail of each stream, so the traceback at the end survives. stderr gets a guaranteed third of the budget. The command echo is clipped at 400 chars, so a heredoc can't eat the budget. ANSI codes are stripped, and CR progress bars collapse to their final state. Tiny limits keep the old head-cut shape.
- **shell tool:** an empty or whitespace `cmd` gets an error.
- Tests: tests/test_shell_runner_robust.py (38). Mutation on round 25: 22 of 38 fail. The remainder are unit checks against the stubs.

## Round 27: Docker runner (branch avrone-a12lr-daemon)
- **In-container deadline:** killing a timed-out `docker exec`/`docker run` client never killed the command inside the container, so runaway loops kept burning the container's CPU. Docker commands now launch through `agentcontainer.GUARD` (`sh -c GUARD <deadline> <cmd>`). It applies `timeout -s KILL <t+2>` inside the container when the image's `timeout` accepts that syntax. The syntax is probed, so on an old busybox the command still runs, just without the deadline.
- **No-bash images:** alpine and busybox images used to fail with exit 127 on `bash -lc`. GUARD falls back to `sh -c`.
- **Dead containers:** when the shared env container or an agent home dies (daemon restart, OOM, `docker rm`), docker answers "No such container" or "is not running/paused/restarting". The runner recreates the container once and retries. The retry only happens when docker refused before the command started (empty stdout, exit 1/125/126, daemon error text), so a command can't run twice. A failed restart is reported in stderr.
- **Leaked --rm containers:** isolate-mode runs (`avrone-run-*`) and the docker_run tool (`avrone-dr-*`) are now named, and they're `docker rm -f`'d on timeout or cancel.
- **Auto mode:** if the docker health check fails (for example, an image with no shell), auto mode stops docker and falls back to local. An explicit `runner=docker` still raises.
- **Exit 125:** labeled "docker failed before the command ran" when the stderr is docker's.
- `exec_argv` without a timeout keeps the old `bash -lc` shape. `run()` always passes the timeout.
- Tests: tests/test_docker_runner_robust.py (36), run against a fake `docker` shim. Mutation on round 26: 25 of 36 fail. Not verified against a real Docker daemon.

## Round 28: evolve proposal pipeline (branch avrone-a12lr-daemon)
- **Spaces in paths:** `cd`, `git clone`, `git checkout`, and `PYTHONPATH` used unquoted paths. A repo or data dir on a drive like `/Volumes/My Passport` broke every evolve lane. All of them are now `shlex.quote`d.
- **Commit-message injection:** the goal went into the commit as `json.dumps(goal)[:200]`, which is double-quoted, so `$(...)` and backticks in an agent-written goal ran as shell commands. Cutting at 200 chars could also drop the closing quote, and then the commit silently never happened. `commit_message()` now collapses whitespace, caps at 200 chars with an ellipsis, and `shlex.quote`s the result. The commit runs with `--no-verify`, `core.hooksPath=/dev/null`, and `--allow-empty`.
- **Commit checked:** if tests pass but the commit fails, the proposal is now marked REJECTED with `commit_error` in its meta. Before, it showed ACCEPTED with nothing on the branch.
- **Containment (`_in_repo`):** the string-prefix check let `../wt-<pid>0/x` write into a sibling clone. It's now a parents check. Writes into `.git` are refused (`.git/config` can set `core.fsmonitor`, which makes git run commands). Also refused: the repo root, directories, empty or whitespace paths, and NUL bytes.
- **Edits:** non-object edits are refused. `content: null` is refused (it used to write the literal text "None"). dict/list content is written as JSON. Files are written as UTF-8 bytes with no newline translation, and lone surrogates become `?`. Write errors (OSError) come back as a result, and the clone is removed.
- **Clone fallback:** a half-made dir from a failed clone used to crash `copytree` with FileExistsError. It's removed first now. The copy skips agents/, node_modules, venvs, and caches, and keeps symlinks as links.
- **evolve_diff:** each proposal now records its `base` sha, and the diff runs against that base (new files included). A rejected, uncommitted proposal used to show the repo's previous commit instead of its own edits. A bad base value falls back to the old diff and never reaches the shell.
- Tests: tests/test_evolve_propose_robust.py (45). Every test runs from a base path that contains a space. Mutation on round 27: 39 of 45 fail.

## Round 29: durable job queue (avrone/jobqueue.py)
- **Lease fencing:** each `claim()` mints a `token` (the column is added to old DB files in place). `complete`/`fail` with that token only touch the job while this worker still owns it. Before, a worker whose lease ran out could mark a job done, or requeue it, while a second worker was running it. `complete` now returns a bool. `fail` can now return 'stale' or 'missing' (an unknown id used to report "requeued").
- **Heartbeat:** the Worker extends the lease every lease/3 while the handler runs. A job longer than the lease (900 s default) used to be re-claimed mid-run and run twice.
- **Release on cancel:** stopping a Worker mid-job puts the job back to queued at once, and the attempt doesn't count. Before, it sat "running" for the whole lease.
- **Poison jobs:** a job whose lease expired on its last attempt (it crashed or hung every worker) goes dead. Before, it was re-claimed forever.
- **Corrupt payload:** the job is marked dead in the claim transaction and the next job is claimed. Before, `json.loads` raised out of `claim()` and killed the Worker loop for good.
- **Results:** `_encode_result` always stores valid JSON. It uses `default=str`, maps NaN to a string, and replaces anything over 100 KB with `{"truncated", "bytes", "head"}`. Before, an unserializable result raised, the finished job was failed and re-run, and slicing could store invalid JSON that made `get()` raise forever. `get()` now decodes corrupt rows as `{"corrupt": true, "raw"}`.
- **Worker loop:** it survives claim errors ("database is locked", I/O) with capped backoff, and survives `fail()` errors. It exits quietly once the queue is closed.
- **Inputs:** a bad `lease_s` (0, negative, NaN, inf, non-number) falls back to 900. `max_attempts` is clamped to 1..1000. A bad priority becomes 0. `kind` becomes a str. An unserializable payload raises at enqueue time.
- Tests: tests/test_jobqueue_robust.py (41). Mutation on round 28: 38 of 41 fail.

## Round 30: fleet routing and failover (avrone/fleet.py)
- **Peer load values:** `node_load()` clamps them to an int 0..10000. A string load from a heartbeat used to make `select_nodes`' sort raise TypeError, so every auto dispatch failed. A negative load made a peer look idle forever. `register()`, saved fleet.json records, `health_table`, and the dispatch counters all go through it.
- **Heartbeat setting:** `heartbeat_env()` clamps AVRONE_FLEET_HEARTBEAT to 1..3600, default 15. "abc" used to crash FleetManager at boot, and 0 made every peer read as offline.
- **Clock skew:** a `last_seen` more than 3 heartbeats in the future now reads as offline. After the clock stepped back, a dead peer used to stay "online" until the clock caught up. Junk `last_seen` values read as offline.
- **Failover:** auto dispatch (no route) now tries up to `FAILOVER_MAX`=3 peers, best health first. It fails over only on ConnectError, ConnectTimeout, or PoolTimeout, where the request never reached the peer. A read timeout, a dropped connection, or a 5xx may mean the job already started, so those never fail over. Explicit routes never fail over. When only one peer was tried, the original exception type is kept. Otherwise it raises `RuntimeError("peer unreachable (a, b, c): …")`.
- **Cooldown:** an unreachable peer gets `cooldown_until = now + 2 heartbeats`, which `select_nodes` skips. Its next `register()` builds a fresh record without the cooldown. Each one is audited as `fleet.unreachable`.
- **Load counter:** each attempt's increment and decrement is paired on that attempt's node, so failover can't leak or steal load.
- **Peer replies:** `_post` raises a clear error for a 200 that isn't JSON (captive portal, proxy) and names the content-type and a snippet. Non-object JSON is refused, because every caller calls `.get()` on the reply.
- **register():** non-dict capabilities become {}. Children are filtered to strings and capped at 1000. `select_nodes(k)` coerces k.
- Tests: tests/test_fleet_routing_robust.py (59). Mutation on round 29: 40 of 59 fail.

## Round 31: fleet request signing and peer auth (avrone/fleet.py)
- **Token rotation:** `AVRONE_FLEET_TOKEN_PREV` is also accepted on inbound requests, so peers can be moved to a new token one at a time. Outbound requests always sign with the current token. A request signed with the previous token is audited as `fleet.token.prev`. Replay, tamper, and window checks are unchanged for both tokens. `info()` reports `token_rotating`.
- **Quoted tokens:** `clean_token()` trims whitespace and strips one matching pair of quotes. Docker compose keeps the quotes from a .env file and a shell export drops them, so two nodes with the same .env line used to compute different signatures. A token of `""` alone means the fleet is disabled.
- **Proxy prefix:** `canonical_path()` signs from `/api/fleet/` onward. A receiver behind a reverse proxy or tunnel prefix (`/avrone/api/fleet/run`), or a peer url with a path, used to fail every request as "bad signature". The endpoint and the method are still bound into the signature.
- **Signature format:** the signature must be 64 hex chars and is compared case-insensitively. An upper-cased sig used to be refused.
- **Clock skew:** a stale-request refusal now says how many seconds the sender is ahead or behind, and points to NTP. This shows only after the HMAC passes, so it leaks nothing to someone without the token.
- **Weak token:** a token under 16 chars logs a warning with a generator command, and `info()` reports `token_weak`. It is still accepted.
- Tests: tests/test_fleet_auth_robust.py (48). Mutation on round 30: 23 of 48 fail. The other 25 are regression guards (replay, tamper, wrong token, method) and pass on both versions by design.

## Round 32: memory sync and merge between nodes (avrone/memstore.py, fleet.py, tools_extra.py)
- **Peer namespace lockout fixed:** a child could push 200 one-entry namespaces and fill the parent's 256-namespace cap. After that, every local agent's first `memory_save` on the parent was refused. `receive_memory` now limits each sender to `AVRONE_MEMORY_MAX_REMOTE_NS` new namespaces (default 32, clamped 1..4096), and all peers together to half of `AVRONE_MEMORY_MAX_NAMESPACES`. Existing namespaces still update. Refused entries are counted as `over_quota` in the reply (with a note) and in the `fleet.memory.merge` audit.
- **Typed merge:** `merge_value()` refuses null, bool, object and list values. A null used to be stored as the text "None", and a dict as its Python repr. Numbers are kept as text. `merge_key()` refuses non-string and non-integer keys. A non-dict entry is counted as rejected instead of crashing the merge (this also covers agentpack imports).
- **Full namespace:** an incoming NEW key whose ts is not newer than the oldest kept key is skipped. Before, it evicted a newer memory, was evicted itself right after, and was still reported as "merged".
- **memory_sync `keys`:** a bare string (`"db"`) is now one key. It used to be iterated into `{"d","b"}` and synced the wrong memories. A number counts as one key, and junk returns an error.
- Tests: tests/test_memory_sync_robust.py (67). Mutation on round 31: 43 of 67 fail. The other 24 are guards that pass on both versions.

## Round 33: agent migration packs (avrone/agentpack.py)
- **Mac case collisions:** `fold_path()` (NFC + casefold) catches pack paths that are the same file on case-insensitive APFS or exFAT. Example: `Notes.md` and `notes.md`, or NFD and NFC `café`. Before, the second write silently replaced the first, and the import reported success with a file missing. The file-vs-directory conflict check also ignores case now (`Tools` vs `tools/run.sh`).
- **Path hygiene:** control characters (newline, ESC, DEL, CR) in pack paths are refused. A component over 255 bytes, or a whole path over 1000 bytes, is refused up front with a clear message, instead of failing mid-install with an OSError.
- **Executable bits:** export records `mode: 0o755` for any file with an exec bit, and import restores it. Before, migrated scripts and tools landed as 0644 and stopped running. Only 0644 or 0755 is accepted (setuid, setgid and world-writable are refused). Old packs without `mode` still import.
- **mtimes:** clamped to 1970..2242 on install, and a `utime` failure (exFAT's 1980 floor, OverflowError) is ignored as cosmetic. Before, an OverflowError escaped as a 500.
- **Export under churn:** a file deleted during the walk is skipped. A file that vanishes or can't be read before packing raises a PackError naming it, instead of an uncaught FileNotFoundError or PermissionError.
- Tests: tests/test_agentpack_robust.py (45). Mutation on round 32: 28 of 45 fail. The other 17 are guards that pass on both versions.
- Updated tests/test_robust_v020_homes.py::test_overlong_member_name: a 300-byte name is now refused up front ("too long"), not mid-install. It still raises a PackError and leaves no residue.
- Gate: 4072 passed (4071 plus the updated test, rerun alone), 2 skipped. Edge suite: bash 3.2 ran shards 1 and 3 (89+89), bash 5 ran shards 0 and 2 (90+89), 357 total.

## Round 34: CodeX event log and the HUD live stream (avrone/codex.py, /ws/events in server.py)
- **Strict JSON events:** `jsonable()` turns event data into values `json.dumps(allow_nan=False)` and a browser's `JSON.parse` accept. Before:
  - a tuple dict key raised TypeError out of `emit()`, killing the agent step that logged it;
  - with redaction off, a cyclic structure raised "Circular reference";
  - NaN and Infinity were written as bare `NaN`, so the HUD's JSON.parse threw and `/api/runs/{id}` returned 500 ("Out of range float");
  - bytes or sets sent live made `send_json` raise and dropped the dashboard.

  Non-finite floats become null, sets become sorted lists, bytes are decoded as UTF-8 with replacement, nesting is capped at 32 levels, and a hostile `__str__` becomes `<Type>`. Replay converts old NaN lines when it reads them. Redaction still runs first.
- **Run ids:** `log_name()` keeps a plain id (`[A-Za-z0-9_.-]{1,128}`, no leading dot) as the file name. Any other id maps to a stable `x-<sha256[:24]>` name. Before, `emit("../escape")` wrote outside `runs/`. Emit and replay use the same mapping.
- **Slow dashboard:** events dropped because a subscriber's queue was full are counted per subscriber. When the queue has room, that subscriber gets one `stream.gap` event (`missed: N`) ahead of the next event.
- **Stalled dashboard:** each WebSocket send is bounded by `AVRONE_WS_SEND_TIMEOUT` (default 30 s, clamped 1..600). A client that stops reading is closed with code 1008 and its subscriber slot is freed. Before, 64 stalled clients locked everyone out of the HUD.
- Tests: tests/test_codex_stream_robust.py (67). Mutation on round 33: 48 of 67 fail.

## Round 35: MCP client against misbehaving servers (avrone/mcp_client.py)
- **HTTP total deadline:** `request()` now has a wall-clock limit of the server's timeout. httpx's timeout only applies per read, so an SSE stream sending keep-alives or a trickle of progress events held a tool call open forever.
- **Size caps:** `AVRONE_MCP_MAX_RESPONSE_BYTES` (default 32 MiB, clamped 64 KiB..1 GiB) caps one HTTP body, one SSE event, and one stdio line. Before, an endless `data:` stream grew memory without limit. Error bodies are read at most 4 KiB.
- **SSE parsing per spec:** exactly one leading space is stripped. A bare `data` line counts as data. Lookalike fields (`database:`) and `:` comment lines are ignored.
- **Server-to-client requests inside the stream:** a ping gets `{}`, and any other method gets -32601, sent as a separate POST. Before, they were ignored, and a server waiting on its ping stalled the call. A failure sending that reply doesn't break the call.
- **Session ids:** an `Mcp-Session-Id` that isn't visible ASCII, or is over 1024 characters, is ignored with a warning. Before, it was echoed back: httpx refused the header on every later call, and it was a header-injection path.
- **Session expiry:** after a 404 on a live session, `MCPClient` starts a new session once and retries, as the spec says. Before, every tool on that server failed until AVRONE restarted. A plain 404 is not retried.
- **stdio:**
  - Lines over the cap are discarded up to their newline. The requests waiting at that moment fail with the reason, and the connection keeps going. Before, a ValueError killed the reader and later calls hung until their timeout.
  - Deep-nesting and 5000-digit-integer lines are skipped.
  - list, dict, or bool ids no longer kill the reader.
  - A BrokenPipe or ConnectionReset during the write/exit race is an MCPError, not a raw exception past the tool wrapper.
- **Config:**
  - String `args` are split shell-style. Before, `"-y srv"` became one argument per character.
  - `timeout` "abc", NaN, 0, or negative falls back to the default; other values are clamped 1..3600.
  - Non-object `headers`, `env`, or `mcpServers` give a clear error.
- **Results:** non-object initialize/list/call results are tolerated.
  - A bad `protocolVersion` (list, number, CR/LF) falls back to the default, so it never becomes a header.
  - Non-string tool names are skipped. Before, they aborted the whole manager start for every server.
  - A repeated or cyclic `nextCursor` stops paging (a repeated cursor used to be requested 100 times), and duplicate tools are listed once.
  - A string `content` is no longer printed one character per line.
  - A string `required` in a schema is no longer split into characters.
  - Error codes that aren't integers become None.
- **Tool name collisions:** `a b`, `a_b`, `a?b`, and servers whose names sanitize alike get `_2`/`_3` suffixes. Before, `register()` silently replaced the earlier tool. Name parts are trimmed to 64 characters.
- Tests: tests/test_mcp_client_robust.py (117), including a real subprocess stdio server. One older test was updated: `test_list_tools_pagination_guard_and_empty_call` now expects a repeated cursor to stop after 2 calls, not 100.

## Round 36: recon guardrails, next layer (avrone/recon.py)
- **DNS-rebinding pin (security):**
  - Before: `http_fingerprint` vetted the name's address, then httpx resolved the name again on its own. A rebinding name (public on the first lookup, 127.0.0.1 or 169.254.169.254 on the next) reached a private address.
  - Now: `_PinnedTransport` connects every request and redirect hop to the exact address the guard vetted for that host. The Host header, the TLS SNI and the certificate check still use the name; this was proven with a real local TLS server. An unvetted host is refused before any connection.
- **port_scan `authorized`:** before, `bool("false")` was True, so `"false"`, `"no"` and `"0"` all authorized a scan. Now only true, "true", "yes", "1" or 1 count.
- **Strict ports (`_strict_int`):** before, `True` became port 1 and 443.9 became 443. Now bools, non-integer floats, "443.5", NaN and lists are refused.
- **Allowlist entries:** these used to be kept verbatim and never matched anything, so they silently allowed nothing. Now each entry is reduced to its bare host:
  - URLs (`https://mysite.com/`) and host:port (`mysite.com:443`).
  - userinfo (`user@mysite.com`) and Unicode names (bücher.de becomes xn--bcher-kva.de).
  - An IP-literal entry never matches.
- **IPs embedded in names:**
  - The dash form (`10-0-0-1.sslip.io`, `app-192-168-1-5.nip.io`) is refused.
  - The 8-hex form (`7f000001.nip.io`) is refused on the wildcard-DNS services (nip.io, sslip.io, xip.io, traefik.me, nip.direct).
  - Loopback wildcard domains (localtest.me, lvh.me, vcap.me, lacolhost.com) are refused.
  - The connecting tools still re-check resolution; this refuses these names up front.
- **IPv6:** site-local fec0::/10 now counts as non-public.
- **tls_inspect:** tries each vetted address (up to 4) instead of failing when the first one, often IPv6, is unreachable.
- Tests: tests/test_recon_pinning_robust.py (124), including a real 127.0.0.1 HTTP server with a hostname that has no DNS at all, and real TLS SNI and certificate verification. Mutation: 71 of 124 fail on round 35's code.

## Round 37: checkpoint/resume vs crashes and odd files (avrone/checkpoint.py)
- **Durable saves:** the temp file is fsynced before `os.replace`, and the directory after it, best effort. A power cut on a delayed-allocation filesystem could otherwise leave a zero-length checkpoint.
- **Poison values:**
  - Before: a value JSON can't encode (a cycle, tuple dict keys, very deep nesting) went into the shared dict anyway, so every later save failed and the run silently stopped checkpointing.
  - Now: `put` refuses that one value (returns False, `write_errors += 1`) and stores the JSON round trip of good values. `put` now returns a bool.
- **Unusable files are renamed aside, not overwritten:**
  - Corrupt JSON, the wrong shape or bad UTF-8 → `<name>.corrupt-<ts>`.
  - Another format version → `.v<N>-<ts>`.
  - Over `AVRONE_CHECKPOINT_MAX_BYTES` (default 64 MiB, clamped 64 KiB..1 GiB) → `.toobig-<ts>`.
  - A zero-length file is simply a fresh start. A directory or an unreadable file is never touched.
- **Stale temp files:** `<name>.*.tmp` older than 1 h (left by a killed writer) are removed on load. The glob is escaped, so names containing `[` work.
- **Resume matching:** compared after a JSON round trip. A tuple extra used to be stored as a list, so it never matched again and every resume wiped the scope.
- **Case-insensitive filesystems (macOS APFS):** the busy set and the file locks are keyed by the case-folded path. Before, "Job" and "job" were the same file there but could be claimed by two runs at once.
- **list/describe:**
  - Symlinks, directories and oversize files are skipped, so a symlink can't leak another JSON file through the API.
  - Odd `updated` values (a string, NaN, Infinity) no longer crash sorting. Ties sort by id.
  - Non-list `history`, bool or string `n`, and a non-string `mode` read as empty.
- Tests: tests/test_checkpoint_robust.py (63), including a real SIGKILL of a writer process mid-write: the file stays loadable and every stored step is complete. Mutation: 47 of 63 fail on round 36's code.

## Round 38: AVRONE's own MCP server vs hostile clients (avrone/mcp_server.py)
- **Two server-killing parse bugs fixed.** A line holding an integer over Python's 4300-digit limit raises `ValueError` (not `JSONDecodeError`). Very deep nesting raises `RecursionError`. Either one used to escape the read loop and kill the server. Both now return -32700 and serving continues. A UTF-8 BOM at the start of a line is tolerated.
- **Envelope checks:**
  - An id that is a bool, a float, an object or an array → -32600 with id null.
  - `jsonrpc` present but not "2.0" → -32600. A missing `jsonrpc` is still tolerated.
  - 1 and "1" are distinct ids.
- **Cancellation:** `notifications/cancelled` cancels the in-flight request it names and sends no reply, per the spec. Its in-flight slot and checkpoint claim are freed. Before, a cancelled squad or deep run kept running.
- **Duplicate ids:** a second request reusing an id that is still in flight → -32600. Null ids are never tracked.
- **Batches** are capped by `AVRONE_MCP_MAX_BATCH` (default 64, clamped 1..1024). Before, a 100k-element line spawned 100k tasks.
- **stdin closed** (the MCP stdio shutdown signal): in-flight work gets `AVRONE_MCP_SHUTDOWN_GRACE` seconds (default 30, clamped 0..3600). After that it is cancelled and each open request gets a -32603 "server shutting down" error. Before, the server stayed alive for hours for a client that was gone.
- **Output:**
  - A result JSON can't carry (NaN, objects) → -32603 for that id. Before, NaN went out as invalid JSON.
  - A lone surrogate is backslash-escaped instead of raising.
  - A broken or closed stdout stops writes and ends the serve loop.
- **Arguments:** `squad_size` 1e999 (inf) falls back to the default instead of an OverflowError tool error. A non-string `first_answer` is refused.
- Tests: tests/test_mcp_server_robust.py (88), including a real `python -m avrone.mcp_server` subprocess fed hostile lines over stdio.

## Round 39: Governor on macOS, in containers, and with junk inputs (avrone/governor.py)
- **Mac memory without psutil.** Before, a Mac without psutil had no memory reading at all, because `/proc/meminfo` doesn't exist there. That mattered on an M4 holding gpt-oss:120b (~65 GB). Now:
  - `sysctl -n hw.memsize` plus `vm_stat` give the reading. The page size comes from the header (16 KiB on Apple Silicon). Available counts free, inactive, speculative and purgeable pages.
  - `kern.memorystatus_vm_pressure_level` raises the reading: warn (2) to at least 90%, critical (4) to at least 96%.
- **Mac thermal signal.** macOS has no readable temperature without root. A new sensor, `thermal_limit_pct`, reads the CPU speed limit from `pmset -g therm` (the smaller of CPU_Speed_Limit and CPU_Scheduler_Limit).
  - It ramps parallelism from 100% down to 50%.
  - `is_hot()` fires at or under `AVRONE_THERMAL_LIMIT_HOT_PCT` (default 80). Before, edgepack thermal migration could never fire on a Mac.
- **Probes are bounded.** Every probe command has a 2 s timeout (`CMD_TIMEOUT_S`), stdin is DEVNULL, and any failure gives None. The governor runs on the event loop's thread.
- **Containers.**
  - Memory is the worse of the host reading and the cgroup limit: v2 `memory.max`/`current`, or v1 `limit_in_bytes`/`usage_in_bytes`, minus `inactive_file`, the way `docker stats` does it. Unlimited or junk limits are ignored. Before, a container at 96% of its own limit read as a healthy host.
  - Load per CPU divides by the usable CPUs: the smallest of `os.cpu_count()`, the affinity mask and the cgroup CPU quota. Before, a saturated 2-CPU container on a 16-core host read as 0.25 load.
- **Fallbacks.**
  - If psutil raises or returns NaN, the fallback readers are used.
  - `/proc/meminfo` parsing skips odd lines; one bad line used to lose the whole reading.
  - A negative millidegree sysfs value (a powered-off sensor at -274000) is dropped.
- **Junk inputs never raise.** `target(want)` accepts NaN, inf, strings and bools. `max_parallel` and `min_parallel` accept inf, "2.0" and junk (`max_parallel` is clamped to 4096). A NaN `ttl_s` falls back to 2 s.
- Tests: tests/test_governor_platform_robust.py (127). macOS command output is simulated from the documented formats and **not checked on a real Mac**.

## Round 40: telemetry, system map and Mermaid against broken and hostile run logs (avrone/telemetry.py)
- **One bad line no longer 500s the HUD.** `_iter_events` skips:
  - lines that aren't JSON objects (lists, strings, numbers)
  - huge-int and deep-nesting lines (which raise `ValueError` or `RecursionError`)
  - bad UTF-8 (decoded with replacement)
  - logs deleted between glob and stat, and unreadable files
  - a missing log dir, or one that is a file
- **Fields are normalised.** `ts` must be a finite number, otherwise it reads as 0 and falls outside the window. Run ids must be a string or int (a list id raised `TypeError`). `data`, `agent` and `kind` must have the right types. An unhashable `mode` reads as None. A non-string spawn `parent` reads as no parent.
- **Durations.** `run.done` earlier than `run.start` (a clock step) clamps to 0. Completion is tested with `is not None`.
- **Bounded reads.** Each log is read from its tail, up to `AVRONE_TELEMETRY_MAX_FILE_BYTES` (default 8 MiB, clamped 64 KiB..256 MiB), and the partial first line is dropped. Before, a multi-GB log was read whole on every request.
- **Window.** `clamp_window`: NaN, inf, zero, negative or junk become 24 h, capped at 366 days. Before, `?hours=nan` showed an empty HUD.
- `active_now` tolerates odd shapes for `core.active`.
- **Mermaid.**
  - Labels are entity-escaped (`" [ ] | ` < > { } &`), whitespace is collapsed and the length is capped at 120. Before, a model-written tool name could close the node text and inject a `click` statement.
  - Ids are ASCII-only and unique: "a-b" and "a_b" no longer merge.
  - Junk nodes and edges are skipped, and odd weights and `min_weight` values are tolerated.
- Tests: tests/test_telemetry_robust.py (91).
- **Also fixed in round 40: an MCP server hang found by the gate.** Round 38's real-subprocess test timed out after 60 s in 2 of 3 runs under load. Two causes:
  - `shutdown()` ignored a core that was still building at stdin EOF. That core then finished, and its runner and MCP children were never stopped.
  - Children inherited the real stdout, so a lingering child held the client's pipe open.
  - Fixes: `shutdown()` waits up to the grace period (at least 1 s), then cancels the core build, and stops the runner and MCP of a core that finished during shutdown. `protocol_stdout()` gives JSON-RPC a private, non-inheritable dup of fd 1 and points fd 1 at stderr, so a stray child print can't corrupt the protocol either.
  - Six new tests in tests/test_mcp_server_robust.py. The subprocess test then passed 6 of 6 runs.

## Hardening round 41 (Hark edge-case series; not the v0.x "round 1xx" series): fsunify on case-insensitive filesystems
- **Agent-isolation bypass on macOS and Windows.** `shared/Agents/<other>/...`, `shared/AGENTS/...`, `shared/agents./...` and `shared/agents::$INDEX_ALLOCATION/...` resolved to paths that `is_relative_to(ws/agents)` called outside. On APFS, HFS+ and NTFS they are the same directory, so one agent could read or overwrite another agent's dir and dodge its own quota through `shared/`. A symlink whose target names the agents root in another case slipped past the same way.
  - `_touches_agents()` now also compares the first component casefolded, with NTFS `:stream` syntax and trailing dots and spaces removed.
  - Names that only look similar stay allowed: `agents-notes`, `my-agents`, `x/agents`, and an `Agents/` folder inside your own dir.
  - On Linux this refuses `shared/Agents/` even though it would be a separate dir. That is deliberate: the same tree has to behave the same on a Mac.
- **Unusable agent dir.**
  - `agent_root()` used to leak a raw `OSError` with the host's absolute path when mkdir failed (read-only disk, permissions). It now raises an audited `AgentFSError`.
  - A file sitting where the dir should be is refused.
  - A symlink loop gives a plain refusal.
- **`display()`** no longer raises on a symlink loop or a broken agent dir.
- Tests: `tests/test_fsunify_robust.py`.

## Hardening round 42 (Hark edge-case series): logsafe against every argument shape
- **Non-string arguments went into the server log raw.** Round 150 cleaned only `str` and exception arguments. A `Path("ckpt\nWARNING avrone.fleet ... trusted")`, an object whose `__str__` or `__repr__` held outside text, or a list or dict holding ESC or U+202E could all forge a line or reach the operator's terminal.
  - They are now wrapped in `_Shown`, so `%s`, `%r` and format specs all render the cleaned and redacted text.
  - Numbers (`numbers.Number`: int, float, bool, Decimal, Fraction, numpy scalars) and `None` pass through, so `%d`, `%x`, `%.2f` and `%c` keep working.
  - bytes are left alone, because their repr already escapes control bytes.
- **Template injection.** With arguments present, the template was never checked, so `log.warning(f"peer {name}: %s", err)` put ESC, `\r` or U+202E from `name` straight through. Templates now keep `\n` and `\t` only; everything else in the unsafe class is escaped.
- **Non-str messages.** `log.warning(exc)` or `log.info(obj)` with no arguments was rendered by `str(msg)` unescaped. It is now converted, redacted and escaped.
- **One raising `__str__` disabled cleaning for the whole record**, because the outer `except` swallowed it and left every argument raw. Each argument is now cleaned on its own, and a bad one renders as `<unprintable T: E>`.
- **Size.** A 50 MB MCP stderr line went into one log line, and every redaction regex ran over it.
  - Arguments are capped by `AVRONE_LOG_ARG_MAX_CHARS` (default 65536, clamped 256..4 MiB), with a `...[log arg truncated: N chars]` marker.
  - Redaction sees at most 4x the cap.
- Tests: `tests/test_logsafe_robust.py`.

## Hardening round 43 (Hark edge-case series): evidence_pool identity canonicalisation
- **Duplicate relays in disguise.** `pool_trials` exists so one outcome relayed twice can't count as two confirmations, but it compared raw strings. All of these were "distinct" trial_ids and were counted twice:
  - NFC vs NFD (`trial-é`), a zero-width space or joiner, a BOM, a soft hyphen, and bidi marks
  - fullwidth digits, ligatures, and no-break or ideographic spaces
  - Identity fields are now NFKC-normalised, Unicode format characters (Cf) are removed, and whitespace runs collapse to one space.
- **Fake silos.** `Node-A`, `node-a` and `node-a\u200b` were separate sources, so one node could fake an orientation conflict. Sources are now grouped case-insensitively after canonicalisation, the first spelling seen is reported, and the source cap counts canonical names. Trial ids stay case-sensitive.
- **Control characters and lone surrogates** in either field are refused. They used to pass into the report and break JSON encoding.
- **Lengths are checked on the canonical form.** Padding no longer rejects a valid id, and NFKC expansion (U+FDFA becomes 18 characters) can't get past the limits. A raw bound of 4x the limit comes first.
- **Duplicate error messages** quote the id escaped to ASCII and cut to 64 characters.
- **Input checks:**
  - A lone dict, a string, bytes or None as `trials` gets a clear refusal.
  - A Mapping whose `.get` raises becomes a ValueError.
- Tests: `tests/test_evidence_pool_robust.py`.
