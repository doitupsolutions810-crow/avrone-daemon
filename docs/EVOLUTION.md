# AVRONE evolution map: where merging the systems makes it greater

Analysis written for v0.35 (2026-11-03) after reading `avrone-daemon`, `Avrone`, `grok-bot-`,
`GreatScale-Avrone-Grok-Fusion-Bridge`, `capability-authority`, `CycleKernal`, `great-scale` and
`living-intermediate-control-plane`. The rule from `integrations/grok-bot/README.md` holds for
every merge below: **AVRONE stays the canonical orchestration and state layer; other systems join
through contracts (HTTP, MCP, events), never by copying their code over AVRONE's package.**

## 1. What each system actually is

| System | Real core (what runs) | Already in AVRONE? |
|---|---|---|
| **avrone-daemon** (this repo, v0.34 → v0.35) | Grok-Heavy squads + Lead merge, deep review waves, ctxspawn, bridge blackboard, netsquad fleet (HMAC-signed), memory, checkpoints, evolve loop, MCP client+server, Mac autopilot, ~2,950 tests | — (canonical) |
| **Avrone** | Same lineage; held 9 commits the daemon lacked (Grok Bot bridge contract, bridge chaos + multiprocess tests, cross-process journal compaction lock) | **Merged in v0.35** (no force, `--no-ff`) |
| **grok-bot-** | Docker execution, persistent browser, team tools, console UI | Contract only (`integrations/grok-bot`, `scripts/verify-grok-bridge.sh`) |
| **capability-authority** | Short-lived signed capabilities, attenuation (no amplification), dual approval, reconcile-before-retry, signed receipts, SPIFFE identity | **First slice merged in v0.35: `avrone/capgate.py`** |
| **GreatScale-…-Fusion-Bridge** | Relay: AVRONE blackboard ↔ CycleKernal CognitiveBridge (trait/mood updates) | Partly: `avrone/greatscale.py` (residual/heat coordinate). Relay still runs against a Flask *shim*, not the real `/api/bridge` |
| **CycleKernal** | Simulation engine + monitoring stack (Prometheus, Grafana, Mongo, Redis) | No |
| **great-scale** | Math research (residual partition, heat law, QGT embedding) | Yes, as `greatscale.py` (evidence → heat; "a mathematical embedding, not a physical claim") |
| **living-intermediate-control-plane** | Node control plane: lattice/policy, federation digests, attestation, Next.js *Avrone chat cockpit* | No (it proxies to AVRONE by URL) |

## 2. The merges that compound (ranked by leverage)

### A. Capability plane inside the agent loop (started: `capgate`)
AVRONE's weakest boundary is **ambient power**: every agent in a run can call every registered
tool, and a fleet peer with the shared token can ask for anything. capability-authority already
solved that shape. v0.35 folds in its core rules, scaled to one node:

- every tool call is checked against a **signed, expiring capability** (`AVRONE_CAPGATE=enforce`);
- sub-agents spawned via ctxspawn receive an **attenuation** of their parent's capability
  (subset of rights, same or earlier expiry, depth + 1); a role policy can narrow, never widen;
- every executed / refused / failed call appends a **hash-chained, HMAC-signed receipt**
  (digests of args and output, never contents); `GET /api/capgate` walks and verifies the chain.

Next steps: (1) carry the capability in fleet requests so a remote sub-squad inherits an
*attenuated* right instead of the fleet token's full power; (2) route `priv_*` tools through
capability-authority's **dual approval** service when it is reachable; (3) bind node identity to
SPIFFE IDs instead of a shared HMAC token; (4) export the receipt chain head into
living-intermediate-control-plane's attestation/federation digests.

### B. One evidence bus: CodeX ↔ CognitiveBridge ↔ Great Scale
Three systems emit "what happened" in three schemas: AVRONE's CodeX events, the bridge blackboard
(posts, conclusions, lessons), and CycleKernal's trait/mood stream. The Fusion Bridge proves the
relay but targets a shim. Point `avrone_cyclekernal_link.py` at the real AVRONE `bridge` endpoints,
and have greatscale's heat snapshot (flaky-check variance) feed the same stream. Outcome: one
timeline where a flaky test, a contested squad conclusion and a CycleKernal mood shift line up.

### C. Observability: CycleKernal's Prometheus/Grafana for AVRONE telemetry
AVRONE already has `telemetry.py`, `governor.py` (CPU/thermal/memory), fleet load, codex stats and
log health. Expose them as a `/metrics` endpoint and reuse CycleKernal's Prometheus + Grafana
compose stack instead of building a new one. Capgate receipt rates (ok / denied / error per role)
become the first security panel.

### D. One cockpit: living-intermediate-control-plane's Avrone chat UI
`avrone-chat/` (Next.js) is already shaped as a front end for AVRONE; AVRONE's own HUD is a single
static page. Make the Next.js app the primary UI over AVRONE's API (squads, runs, fleet map,
capgate chain status), keep the static HUD as the zero-dependency fallback.

### E. Execution: Grok Bot as the sandboxed runner
Keep the existing contract, then let AVRONE's `DockerRunner` delegate to Grok Bot's persistent
browser/containers for web-heavy lanes, with every delegated call receipted by capgate.

## 3. What not to merge

- Copying Grok Bot's bundled `avrone/` package or any other repo's code over this package
  (overlapping, independently evolved implementations; it would silently revert ~40 rounds of
  hardening).
- great-scale's physics language as runtime claims. Keep it as what it is: a mathematical lens on
  measured evidence.
- Anything that widens recon. Recon stays default-deny, bounded, and limited to infrastructure the
  owner controls.

## 4. Model backend (free, open-weight)

Live bench (2026-10-28, CPU sandbox, Ollama): `qwen2.5:3b` scored 1.0 (valid actions, real tool
call, full squad + Lead merge); `llama3.2:3b` valid + tool use. On real hardware use a Qwen3.5 9B/27B
class model (strongest open family on BFCL-v4 tool calling, Apache-2.0) via
`AVRONE_PROVIDER=ollama`. `tools/model_bench.py` + `.github/workflows/model-bench.yml` re-score
candidates.

## CI Status Check — 2026-11-05 (descent/xcv-trust-gates)

- **Verify.sh**: present in repo tree (sha bb14698d8645d3281234701376820ec60feffdff).
- **docker-verify job**: present in `.github/workflows/ci.yml` (runs docker build + Verify.sh inside image).
- Most recent CI run on `descent/xcv-trust-gates` (run 42062034930, head sha cb076bcde32f82b34be53d2cf0338f643c9696ea, commit "fix(verify): align Verify.sh with real test paths and greatscale.py signatures (Descent XCV)"): status completed, conclusion failure.
- Jobs (docker-verify, test (3.11), test (3.12)): all completed in ~3 seconds with **runner_id: 0**, **runner_name: ""** (blank), labels ubuntu-24.04.
- This matches the known account-wide no-runner / provisioning fault (before checkout). **Not treated as a code failure.** No logs examined for test/lint reasons; no code changes pushed.
- Same pattern observed on recent `main` CI runs (e.g. 42055121861): runner_id 0, blank name, rapid failure.
- Recommendation: pause this automation until GitHub Actions runners provision correctly for the account. No force-push or branch deletion performed.

## CI Status Check — 2026-11-05 (later, descent/xcv-trust-gates)

- **Verify.sh**: still present in repo tree.
- **docker-verify job**: still present in `.github/workflows/ci.yml`.
- Most recent CI run on `descent/xcv-trust-gates` (run 42062379997, head sha 25b50272bf1e87d4c523ccb2fee5e86f504e3507, commit "docs: append CI status findings for descent/xcv-trust-gates (known runner provisioning fault)"): status completed, conclusion failure.
- Jobs (test (3.11), test (3.12), docker-verify): all completed in ~2 seconds with **runner_id: 0**, **runner_name: ""** (blank), labels ubuntu-24.04.
- This is the known account-wide no-runner / provisioning fault (before checkout). **Not treated as a code failure.** No logs pulled for test/lint reasons; no code changes pushed.
- Same pattern on `main` (most recent CI run 42055121861): runner_id 0, blank name, rapid failure.
- Recommendation: pause this automation until GitHub Actions runners provision correctly for the account. No force-push or branch deletion performed.

## CI Status Check — 2026-11-05 (current, descent/xcv-trust-gates)

- **Verify.sh**: present in repo tree (confirmed via tree listing).
- **docker-verify job**: present in `.github/workflows/ci.yml` (builds image and runs Verify.sh).
- Most recent CI run on `descent/xcv-trust-gates` (run 42071520512, head sha 1e16dd020006e6930fd8433393159b171f94584e, commit "docs: append CI status findings for descent/xcv-trust-gates (known runner provisioning fault continues)"): status completed, conclusion failure.
- Jobs (test (3.11), test (3.12), docker-verify): all completed in ~3 seconds with **runner_id: 0**, **runner_name: ""** (blank), labels ubuntu-24.04.
- This is the known account-wide no-runner / provisioning fault (before checkout). **Not treated as a code failure.** No logs pulled for test/lint reasons; no code changes pushed.
- Same pattern on `main` (most recent CI run 42055121861): runner_id 0, blank name, rapid failure.
- CI is not green (all recent runs fail at provisioning). Recommendation: pause this automation until GitHub Actions runners provision correctly for the account. No force-push or branch deletion performed.
