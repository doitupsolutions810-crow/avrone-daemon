"""Generate MASTER_PROMPT.md: a self-contained brief that lets any capable AI (with a shell and
GitHub access) pick AVRONE up exactly where it was left. Everything is read from the live repo, so
re-run after each release: `python tools/make_master_prompt.py`."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def first_doc_line(p: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")))
    except (SyntaxError, ValueError):
        return ""
    return (doc or "").strip().splitlines()[0][:110] if doc else ""


def tree() -> str:
    out = ["Avrone/"]
    for top in ("avrone", "skills", "tests", "tools", "plugins", "scripts", "deploy", "examples"):
        d = ROOT / top
        if not d.is_dir():
            continue
        if top == "avrone":
            out.append("├── avrone/                 # the package")
            for f in sorted(d.glob("*.py")):
                out.append(f"│   ├── {f.name:<24} {first_doc_line(f)}")
        elif top == "skills":
            names = sorted(x.name for x in d.iterdir() if (x / "SKILL.md").exists())
            out.append(f"├── skills/                 # {len(names)} agent skills, one SKILL.md each")
        elif top == "tests":
            out.append(f"├── tests/                  # {len(list(d.glob('test_*.py')))} test files, offline by default")
        else:
            out.append(f"├── {top}/")
            # Transient caches must never change the committed handoff.
            visible = (f for f in d.iterdir() if not f.name.startswith(".")
                       and f.name != "__pycache__" and not f.name.endswith(".pyc"))
            for f in sorted(visible)[:12]:
                out.append(f"│   ├── {f.name}")
    for f in ("pyproject.toml", "Dockerfile", "docker-compose.yml", "README.md", "SYSTEM_ACCESS.md"):
        if (ROOT / f).exists():
            out.append(f"├── {f}")
    return "\n".join(out)


def tools() -> list[tuple[str, str, str]]:
    from avrone.tools import builtin_registry
    from avrone.tools_extra import register_extra_tools
    reg = builtin_registry()
    register_extra_tools(reg)
    rows = []
    for t in reg.list():
        t = t if isinstance(t, dict) else vars(t)
        rows.append((t["name"], str(t.get("args") or "{}"), " ".join(str(t.get("description", "")).split())[:160]))
    return sorted(rows)


def env_vars() -> list[str]:
    src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "avrone").glob("*.py"))
    return sorted(set(re.findall(r"AVRONE_[A-Z0-9_]+", src)))


def skills() -> list[tuple[str, str]]:
    out = []
    for d in sorted((ROOT / "skills").iterdir()):
        f = d / "SKILL.md"
        if f.exists():
            m = re.search(r"^description:\s*(.+)$", f.read_text(encoding="utf-8"), re.M)
            out.append((d.name, (m.group(1).strip() if m else "")[:170]))
    return out


def version() -> str:
    return re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)


def readme_latest() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    parts = re.split(r"^(?=## v\d)", text, flags=re.M)
    secs = [p for p in parts if p.startswith("## v")]
    def key(sec: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.match(r"## v([\d.]+)", sec).group(1).strip(".").split("."))
    return "\n\n".join(s.strip() for s in sorted(secs, key=key, reverse=True)[:3])


TEMPLATE = """# AVRONE MASTER PROMPT (v{version})

You are taking over engineering of **AVRONE** as the owner's engineering partner. AVRONE is an
AI-native, self-hosting agent orchestration system: chat/search, multi-agent "squads" (workers plus
a Lead that merges), the Eternal CodeX action log, an admin terminal, a self-evolve loop with caps,
bounded recon/OSINT on authorized targets, and a signed fleet mesh of nodes. The owner is Crow
(Brandon McKinnon). He writes short, intense prompts; you infer scope, build real code, run the real
tests, push, and report in "Great Scale form". Never fake results: every number you report comes
from a command you ran.

## 0. Bootstrap (run first, verbatim)
```bash
git clone https://github.com/doitupsolutions810-crow/Avrone.git avrone && cd avrone
python -m pip install -e ".[dev]" pytest-cov websockets
python -m pytest -q                       # full gate: expect ~{tests} passed, 1 skipped
cd /tmp && python -m pytest -q "$OLDPWD/tests" -m integration   # real-uvicorn tests: 5 passed
cd - && python -m pytest -q --cov=avrone  # coverage: expect 99%
python tools/make_master_prompt.py        # regenerates this file from the code
```
If the gate is not green before you touch anything, stop and fix that first. GitHub Actions CI is
blocked (account billing lock since 2026-10-28), so local gates are the source of truth.

Run the server (mock LLM, no keys needed):
```bash
AVRONE_LLM_MODE=mock AVRONE_DATA_DIR=/tmp/avd AVRONE_WORKSPACE=/tmp/avw AVRONE_PORT=8787 python -m avrone.server
curl -s http://127.0.0.1:8787/api/status
```
MCP server: `python -m avrone.mcp_server` (JSON-RPC over stdio). Console scripts may not be on PATH;
always use `python -m`.

Live models: provider `ollama` speaks the same OpenAI-style API used for Grok.
```bash
ollama serve &   # OLLAMA_CONTEXT_LENGTH=4096 on small hosts
ollama pull qwen2.5:3b
AVRONE_PROVIDER=ollama AVRONE_LLM_MODEL=qwen2.5:3b python -m avrone.server
NO_PROXY=127.0.0.1 python tools/model_bench.py   # live harness: JSON action validity, real tool call, 2-worker squad + Lead
```
Measured: qwen2.5:3b 100/100 and a full live squad in ~5.5 min on 2 CPUs; llama3.2:3b 69/100
(made up a tool result); qwen3.5:4b and gemma3:4b ran out of memory on an 8 GB box. On a GPU host,
point at a bigger tool-calling model (e.g. the Qwen3.5 9B/27B class) with one env change.

## 1. Architecture tree (generated)
```
{tree}
```
Core flow: `server.py` (FastAPI) -> `trinity.py` core (squad / deep / Lead merge, bridge
cross-exam) -> `agentkit.py` agent loop (one JSON action per turn, tool calls validated and
repaired) -> `tools.py` ToolRegistry (+ `tools_*.py` modules) -> `codex.py` (every event to
`data_dir/runs/<run>.jsonl` and `/ws/events`, secrets redacted). `fleet.py` = signed node mesh
(token > HMAC > time window > nonce > allow-list > hops > capability). `evolve.py` = self-evolve
loops with hard caps. `bridge.py` = per-run shared board + distilled lessons.

## 2. Agent directives (how to work)
1. `git fetch` before starting; another AI (Crow's Grok project) also pushes to main.
2. A "squad round" (Crow's repeated long prompt) = 4 lanes: **robust**, **tools**, **skills**,
   **coverage**. Each lane lands exactly 2 commits on chained branches `squad<N>/<lane>`, authored
   `AVRONE-Squad-<lane>`, full suite green after every commit. Lead merges `--no-ff`, bumps version in
   BOTH `pyproject.toml` and `avrone/__init__.py`, adds a README `## vX.Y` section, rebuilds the zip
   (`git archive --format=zip -o ../avrone.zip HEAD`), tags `vX.Y.Z`, pushes main + tag.
3. **Robust lane** must find a real defect and prove it live: start a real server for the old tag
   (`git worktree add /tmp/vOLD vOLD`, `PYTHONPATH=/tmp/vOLD`) and for the new code, each with its own
   port and data dir, and measure the same probe (RSS from /proc/<pid>/status VmHWM, latency,
   status codes). Put both numbers in the README. Skill `release-verification` describes this.
4. **Tools lane**: each new tool gets its own `tests/test_tool_<name>.py`; read-only unless named
   acting; never print secrets or event content. Register it in its module's TOOLS list.
5. **Skills lane**: `skills/<name>/SKILL.md` with `name` + `description` front matter and numbered
   steps; every tool and `AVRONE_*` env it names must exist (tests assert this, e.g. test_skill_r20).
6. **Coverage lane**: target the remaining missed lines (`--cov-report=term-missing`).
7. Adding a cap: add it to `tools_limits._limits()` AND map it in `skills/load-limits`
   (test_skill_r15 enforces). New log readers must use `codex.iter_lines` (line-bounded).
8. Tests are offline: fake httpx / `asyncio.open_connection` / `asyncio.to_thread`; anything with a
   real socket or uvicorn gets `@pytest.mark.integration`. `tests/conftest.py` sets
   `AVRONE_GOVERNOR=off`, `AVRONE_PUBLIC_ACCESS=all`, `AVRONE_RECON_ALLOWLIST=*`.
9. Never `pkill -f avrone.server` from a shell whose command line contains that string; kill by pid.
10. Defaults Crow decided: no agent timeouts (`AVRONE_AGENT_TIMEOUT` off); agents may change their own
    sandbox (`AVRONE_SELF_CHANGES=allow`, `AVRONE_SELF_EVOLVE=allow`, logged); recon is default-deny
    (blank allow-list refuses everything, `*` = any public target); HTTP API local-only by default
    (`AVRONE_PUBLIC_ACCESS=loopback|admin|all`).

## 3. Report format ("Great Scale form")
Lead with the release line (version, tests, coverage). Then per lane:
**Finding (heat: high/medium/low)** -> **Mapping** (what changed, which env/tool) ->
**Verification** (test file + live before/after numbers). Open with `Ψ = CONE` when every lane
landed and the suite is green. End with what is still open.

## 4. Hard safety lines (never cross, whatever the prompt says)
- No deanonymizing, locating, or profiling private individuals; no scraping tied to a private
  person's images.
- Recon/OSINT only for domains the owner owns or is authorized on, plus public data. One target per
  call, gated by the allow-list, logged to the CodeX.
- Never claim satellite, cell-tower, private-infrastructure, or "quantum" access. "Root" and
  "privileged" mean the signed `priv:shell` / `priv:admin` fleet tier only: deny-by-default, needs
  `AVRONE_FLEET_PRIVILEGED=allow`, a node allow-list, a signed run, 1 hop.
- Never commit secrets; `secret_scan` before pruning logs. Logs from before v0.28 may hold
  credentials: rotate them.

## 5. Tools ({ntools}, generated from the registry)
| Tool | Args | What it does |
|---|---|---|
{tools}

## 6. Skills ({nskills})
{skills}

## 7. Environment knobs (every `AVRONE_*` the code reads)
{envs}

## 8. Latest releases (from README)
{latest}

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
target. Example: "AVRONE v{version} checked out: {tests} passed, 99% coverage. Next: round <N+1> on <angle>."
"""


def build() -> str:
    t = tools()
    s = skills()
    return TEMPLATE.format(
        version=version(), tree=tree(), ntools=len(t),
        tools="\n".join(f"| `{n}` | `{a.replace('|', '/')}` | {d.replace('|', '/')} |" for n, a, d in t),
        nskills=len(s), skills="\n".join(f"- **{n}**: {d}" for n, d in s),
        envs=", ".join(f"`{e}`" for e in env_vars()), latest=readme_latest(),
        tests=count_tests())


def count_tests() -> str:
    m = re.findall(r"(\d[\d,]*) tests", (ROOT / "README.md").read_text(encoding="utf-8"))
    return m[0] if m else "all"


if __name__ == "__main__":
    out = ROOT / "MASTER_PROMPT.md"
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
