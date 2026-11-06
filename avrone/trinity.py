"""Trinity Core: the daemon kernel that orchestrates agents.

Three stages, Grok Heavy style:
  1. PLAN   - a planner splits the request into parallel angles.
  2. SQUAD  - N agents work their angle at the same time, each with real tools
              (terminal, files, network) and the power to spawn sub-agents.
  3. LEAD   - a lead agent compares every agent's work, cross-checks where
              they disagree, and writes one final answer.

"chat" mode skips the squad and runs one tool-using agent for quick asks.

"deep" mode is a second wave: given a question and a first answer (or a fresh
squad answer), fixed reviewer angles stress-test and extend it, then a DEEP
LEAD writes a verdict report. With rounds > 1 (arg, settings.deep_rounds or env
AVRONE_DEEP_ROUNDS, capped at MAX_DEEP_ROUNDS) further waves stress-test the
previous round's corrected answer, stopping early once a wave reports no new
corrections (converged).

Learning bridge (avrone.bridge): every agent of a run shares one blackboard. With
AVRONE_BRIDGE_ROUNDS > 0 (default 1, max 3) a squad runs a cross-examination round between
the workers and the Lead merge: each usable worker gets one revise turn with the others'
results and the bridge, may post corrections/conclusions and revise its result; rounds stop
early once one adds no correction, and the Lead sees the bridge digest. 0 = the classic squad.
Conclusions are distilled into a "lessons" memory namespace at run end and matched back into
later runs' worker prompts (AVRONE_LESSONS=off disables).
"""
from __future__ import annotations

import asyncio
import os
import time
import json
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agentfs, memstore
from . import bridge as bridgemod
from .checkpoint import Checkpoint
from .codex import Codex
from .config import Settings
from .ctxspawn import RECENT_CALLS
from .governor import Governor
from .llm import LLM, LLMError
from .sovereign import Runner
from .tools import ToolRegistry, builtin_registry, clip_tool_name

PLANNER_PROMPT = """You are the PLANNER in AVRONE, a multi-agent system.
Split the user's task into {n} independent angles that different agents can work
on IN PARALLEL (e.g. research, implementation, verification, devil's advocate).
Every angle must be useful alone; do not make one depend on another.
Reply with JSON only: {{"subtasks": [{{"role": "<short role name>", "focus": "<what this agent should do>"}}]}}"""

WORKER_PROMPT = """You are {name}, the {role} agent in AVRONE, working inside a real Linux
environment with a workspace directory. Overall task from the user:
{task}

Your focus: {focus}

{tools}
Reply with ONE fenced JSON block per turn, either a tool call:
```json
{{"tool": "<name>", "args": {{...}}}}
```
or, for independent calls that can run at the same time (up to {max_calls}), a batch:
```json
{{"tools": [{{"tool": "<name>", "args": {{...}}}}, {{"tool": "<name>", "args": {{...}}}}]}}
```
or, when you are done, your final result:
```json
{{"final": "<your complete findings, including any code, commands run and results>"}}
```
Be concrete. Verify claims with tools when you can. You have at most {steps} turns.
Working habits: if a skill fits your job, skill_load it first; for multi-part work, put
subtasks on task_board and update them; if a tool or package is missing, install it with
env_install (self-changes are allowed and logged) and carry on; never invent tool output."""

LEAD_PROMPT = """You are the LEAD agent in AVRONE. Several agents worked the same task in
parallel from different angles. Compare their work like a study group:
- keep what is verified or agreed on,
- where they disagree, reason out which is right (say so briefly),
- drop anything unsupported,
then write ONE final answer for the user. Answer the task directly; do not
describe the agents unless it helps. You have no tools and no further turns:
never say you will check, inspect or verify something. Write the finished answer now,
with the concrete facts, numbers and exact strings the agents verified."""

_NARRATION = __import__("re").compile(r"^\s*(I['’]ll|I will|I am|I'm|Let me|Checking|I am checking)\b", __import__("re").I)


def _weak_lead(answer: str, reports: str) -> bool:
    """A lead reply that narrates intent or is far thinner than the reports it merges."""
    a = (answer or "").strip()
    return bool(_NARRATION.match(a)) or (len(a) < 300 and len(reports) > 2000)


DEEP_WORKER_PROMPT = """You are {name}, the {role} reviewer in AVRONE's DEEP ANALYSIS wave, working
inside a real Linux environment with a workspace directory. Someone already
answered the question below (the first wave). Your job is to stress-test and
extend that answer from your angle, not to rewrite it.

{task}

Your angle: {focus}

{tools}
Reply with ONE fenced JSON block per turn, either a tool call:
```json
{{"tool": "<name>", "args": {{...}}}}
```
or, when you are done, your final review:
```json
{{"final": "<your findings: each point, whether it holds, evidence and source URLs>"}}
```
Check claims with tools (fetch pages, run code) when you can and cite what you
opened. Say plainly what you could not verify. You have at most {steps} turns."""

DEEP_LEAD_PROMPT = """You are the DEEP LEAD in AVRONE. A first-wave answer to the user's question
was reviewed by a second wave of specialist reviewers (fact-checking, gaps,
counter-arguments, deeper detail, risks, sources). Weigh their reviews, keep
only what is supported, and write a markdown report with exactly these sections:

## Verdict
One of: Confirmed / Corrected / Extended, then "Confidence: High|Med|Low" and one sentence why.
## Corrections
Claims in the first answer that are wrong or outdated, each with the fix. "None" if none.
## Additions
What the first answer missed: edge cases, details, what the user should also know.
## Counterpoints
The strongest counter-arguments or alternatives, and how much weight they deserve.
## Sources
Primary sources and links the reviewers actually opened (bullet list).
## Bottom line
The corrected, complete answer in a few sentences."""

# Fixed second-wave angles, in priority order (the first n are used).
DEEP_ANGLES: list[dict[str, str]] = [
    {"role": "Fact-Checker", "focus": "Verify each factual claim in the first answer with tools or the web. "
     "Flag anything wrong, unsupported or outdated, with the correct value and its source."},
    {"role": "Gap-Finder", "focus": "Find what the first answer is missing: edge cases, prerequisites, caveats, "
     "and what the user should also know."},
    {"role": "Contrarian", "focus": "Build the strongest counter-arguments and alternative approaches or "
     "conclusions to the first answer."},
    {"role": "Deep-Diver", "focus": "Go one level deeper than the first answer: concrete numbers, "
     "implementation details, worked examples, and where they come from."},
    {"role": "Forecaster", "focus": "Assess risks and what is likely to change next that would affect the "
     "first answer (versions, prices, policy, trends)."},
    {"role": "Synthesizer-of-Sources", "focus": "Collect the primary sources and authoritative links that "
     "support or refute the first answer; open them and note what each says."},
]
MAX_DEEP = len(DEEP_ANGLES)
# Hard ceiling on successive deep waves, whatever the caller or env asks for.
MAX_DEEP_ROUNDS = 5

_NONE_RE = re.compile(r"^(none|n/?a|no corrections?|nothing)\b", re.I)


def _md_section(report: str, title: str) -> str:
    """Body of the '## <title>' section of a markdown report ('' if absent)."""
    m = re.search(rf"^##\s*{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", report or "", re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def _corrections(report: str) -> list[str]:
    """Normalized correction points from a deep report's '## Corrections' section.

    "None", "None found ...", "N/A" and blank lines are not corrections."""
    out: list[str] = []
    for line in _md_section(report, "Corrections").splitlines():
        t = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line).strip()
        if not t or _NONE_RE.match(t):
            continue
        t = re.sub(r"\s+", " ", t).strip(" .;").lower()
        if t and t not in out:
            out.append(t)
    return out


_FAILED_RE = re.compile(r"^\[[^\]\n]*? failed: ")
_CONF_RE = re.compile(r"confidence\s*[:=]?\s*\**\s*(high|medium|med|low)\b", re.I)
_CONF_LEVELS = ["Low", "Med", "High"]


def _reviewer_failed(result: str) -> bool:
    """A reviewer result that is a failure note (crash, timeout, model error) or empty."""
    r = (result or "").strip()
    return not r or bool(_FAILED_RE.match(r))


def _lead_confidence(report: str) -> str | None:
    m = _CONF_RE.search(_md_section(report, "Verdict") or report or "")
    if not m:
        return None
    v = m.group(1).lower()
    return "High" if v == "high" else "Low" if v == "low" else "Med"


def deep_confidence(report: str, lead_ok: bool, ok: int, failed: int, converged: bool,
                    rounds_run: int) -> str:
    """Overall confidence for a deep run: the lead's own call, discounted for evidence gaps.

    No lead or no usable review -> Low. Otherwise start from the final lead's
    "Confidence:" (Med if absent) and drop one level each when at least half the
    reviewers failed, and when a multi-round run hit its cap still finding corrections."""
    if not lead_ok or ok == 0:
        return "Low"
    level = _CONF_LEVELS.index(_lead_confidence(report) or "Med")
    if failed and failed >= ok:
        level -= 1
    if rounds_run > 1 and not converged:
        level -= 1
    return _CONF_LEVELS[max(0, level)]


def resolve_deep_rounds(requested: int | None, settings: Any = None) -> int:
    """Rounds to run: explicit arg, else settings.deep_rounds, else env AVRONE_DEEP_ROUNDS, else 1.

    Always clamped to 1..MAX_DEEP_ROUNDS; junk values fall back to 1."""
    raw: Any = requested
    if raw is None:
        raw = getattr(settings, "deep_rounds", None)
    if raw is None:
        raw = os.environ.get("AVRONE_DEEP_ROUNDS")
    try:
        r = int(raw) if raw not in (None, "") else 1
    except (TypeError, ValueError):
        r = 1
    return max(1, min(r, MAX_DEEP_ROUNDS))


def clamp_deep_rounds(value: Any) -> int | None:
    """Clamp a caller-supplied rounds value to 1..MAX_DEEP_ROUNDS for the API/MCP/fleet surfaces.

    None, booleans, empty strings and junk mean "not given" (None), so the core falls back to
    settings.deep_rounds / AVRONE_DEEP_ROUNDS / 1 via resolve_deep_rounds."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        r = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(r, MAX_DEEP_ROUNDS))


@dataclass
class AgentContext:
    core: "TrinityCore"
    run_id: str
    name: str
    depth: int
    # Per-agent persistent workspace (workspace/agents/<agent_id>/), stable across runs for
    # the same agent_id. None when AVRONE_AGENT_FS=0 or the dir could not be made.
    agent_id: str | None = None
    agent_dir: Path | None = None
    # Pinned memory namespace; None = derived from the name (memstore.agent_namespace). Set only
    # when two live agents of one run would otherwise share a namespace (see _claim_identity).
    memory_namespace: str | None = None
    # ctxspawn: who started this agent and what a child of it inherits (see avrone.ctxspawn).
    role: str | None = None
    focus: str | None = None
    run_task: str | None = None          # the run's original task, inherited down the tree
    parent_name: str | None = None
    lineage: list[str] = field(default_factory=list)  # root -> ... -> parent (names)
    parent_dir: Path | None = None       # parent's agent dir, readable via parent/ (share_dir)
    recent_calls: deque = field(default_factory=lambda: deque(maxlen=RECENT_CALLS))  # (tool, args, output)
    # capgate (AVRONE_CAPGATE): this agent's signed capability; None = minted from policy on first use.
    capability: dict | None = None


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?(?:</\1>|$)", re.S | re.I)
# gpt-oss harmony text leaking through a server that doesn't parse it: drop the analysis channel,
# keep the final/commentary message, and drop the remaining <|...|> control tokens.
_HARMONY_ANALYSIS = re.compile(r"<\|channel\|>\s*analysis\s*<\|message\|>.*?(?=<\|(?:end|start|channel)\|>|$)", re.S)
_HARMONY_TOKEN = re.compile(r"<\|[a-z_]+\|>")
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n?(.*?)```", re.S)
_DECODER = json.JSONDecoder()
_MAX_DECODE_TRIES = 4000  # bounds the scan of a pathological reply (thousands of broken objects)
_MAX_DECODE_SECONDS = 0.3


def _is_action(obj: Any) -> bool:
    return isinstance(obj, dict) and ("tool" in obj or "final" in obj or "tools" in obj)


def _first_action(text: str) -> dict[str, Any] | None:
    """The first JSON object in text that is an action, decoding at every '{' (no greedy regex)."""
    i = text.find("{")
    tries = 0
    deadline = time.monotonic() + _MAX_DECODE_SECONDS
    while i != -1 and tries < _MAX_DECODE_TRIES:
        tries += 1
        if tries % 64 == 0 and time.monotonic() > deadline:
            break
        try:
            obj, end = _DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if _is_action(obj):
            return obj
        i = text.find("{", end)  # skip past the whole non-action object: keeps the scan linear
    return None


def parse_action(text: str) -> dict[str, Any] | None:
    """Pull the JSON action out of a model reply (fenced or bare).

    Robust to: prose with braces around the action, several objects, nested args, any fence
    language tag, <think> blocks (their JSON is ignored when the answer has its own), and leaked
    gpt-oss harmony tokens."""
    if not isinstance(text, str) or "{" not in text:
        return None
    clean = _THINK.sub(" ", text)
    clean = _HARMONY_ANALYSIS.sub(" ", clean)
    clean = _HARMONY_TOKEN.sub(" ", clean)
    for source in ((clean,) if clean == text else (clean, text)):  # visible answer first; reasoning last
        for m in _FENCE.finditer(source):
            obj = _first_action(m.group(1))
            if obj is not None:
                return obj
        obj = _first_action(source)
        if obj is not None:
            return obj
    return None


# Small local models often flatten args onto the action ({"tool":"shell","cmd":...}) or use a
# near-synonym key ("command" for "cmd"). Repair only when the declared schema says so.
ARG_ALIASES = {"command": "cmd", "cmdline": "cmd", "script": "cmd", "file": "path", "file_path": "path",
               "filename": "path", "filepath": "path", "text": "content", "data": "content",
               "body": "content", "code": "content"}


def coerce_args(registry: Any, tool: str, action: dict[str, Any], args: Any) -> Any:
    """Return args repaired for flattened/aliased keys; unchanged when nothing applies."""
    t = registry.get(tool) if registry is not None else None
    schema = getattr(t, "schema", None) if t is not None else None
    if not isinstance(args, dict) or not isinstance(schema, dict):
        return args
    known = set(schema.get("required", {})) | set(schema.get("optional", {}))
    if not known:
        return args
    out = dict(args)
    if not out:  # flattened: pull known keys from the action itself
        out = {k: v for k, v in action.items() if k not in ("tool", "args")}
    for k in list(out):
        a = ARG_ALIASES.get(k)
        if k not in known and a in known and a not in out:
            out[a] = out.pop(k)
    return out if out != {} else args


# Keys local models use for a call's arguments when they mimic other tool-call formats.
ARGS_KEYS = ("arguments", "parameters", "params", "input")
_TOOL_PREFIXES = ("functions.", "function.", "tools.", "tool.", "functions:", "repo_browser.")


def normalize_call(registry: Any, call: Any) -> Any:
    """Repair one {"tool", "args"} call the way gpt-oss/qwen actually miss the format.

    - tool name with a namespace prefix ("functions.shell") or stray spaces, when the bare
      name is a registered tool;
    - args under "arguments"/"parameters"/"params"/"input" instead of "args";
    - args sent as a JSON-encoded string ('{"cmd": "ls"}').
    Returns a new dict; anything it can't repair is passed through for the normal checks."""
    if not isinstance(call, dict):
        return call
    out = dict(call)
    tool = out.get("tool")
    if isinstance(tool, str):
        name = tool.strip()
        has = getattr(registry, "get", None)
        if callable(has) and has(name) is None:
            for pre in _TOOL_PREFIXES:
                if name.startswith(pre) and has(name[len(pre):].strip()) is not None:
                    name = name[len(pre):].strip()
                    break
        out["tool"] = name
    if out.get("args") is None:
        for k in ARGS_KEYS:
            if k in out and isinstance(out[k], (dict, str)):
                out["args"] = out.pop(k)
                break
    args = out.get("args")
    if isinstance(args, str) and args.strip().startswith("{"):
        try:
            decoded = json.loads(args)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            out["args"] = decoded
    return out


def normalize_action(registry: Any, action: dict[str, Any]) -> dict[str, Any]:
    """Repair a parsed action before it runs (see normalize_call).

    A "final" that is null or empty beside a real tool call is noise from models that fill
    every key ({"tool": "shell", "args": {...}, "final": null}): the tool runs instead of the
    run ending with "None". A non-string final keeps its JSON form instead of a Python repr."""
    out = normalize_call(registry, action)
    has_call = bool(out.get("tool")) or bool(out.get("tools"))
    if "final" in out:
        f = out["final"]
        if has_call and (f is None or (isinstance(f, str) and not f.strip())):
            del out["final"]
        elif not isinstance(f, str) and f is not None:
            try:
                out["final"] = json.dumps(f, ensure_ascii=False)
            except (TypeError, ValueError):
                out["final"] = str(f)
    if isinstance(out.get("tools"), list):
        out["tools"] = [normalize_call(registry, c) for c in out["tools"]]
    return out


DEFAULT_TOOL_OUTPUT_MAX = 20000
REPEAT_CALL_LIMIT = 3  # same tool + args + output this many times in a row = the agent is looping


def tool_output_max() -> int:
    """AVRONE_TOOL_OUTPUT_MAX (default 20000 chars), clamped to 1000..500000; junk = default.
    A safety net for tools (MCP, plugins) that don't cap their own output, so one huge result
    can't blow the model's context window and wedge the agent."""
    try:
        n = int(os.environ.get("AVRONE_TOOL_OUTPUT_MAX", DEFAULT_TOOL_OUTPUT_MAX))
    except (TypeError, ValueError):
        n = DEFAULT_TOOL_OUTPUT_MAX
    return max(1000, min(n, 500000))


def clip_output(out: str, limit: int) -> str:
    """Keep the head and the tail (errors usually sit at the end) of an over-long tool output."""
    if len(out) <= limit:
        return out
    marker = f"\n...[{len(out) - limit} chars cut; {len(out)} total]...\n"
    head = (limit * 2) // 3
    tail = max(0, limit - head)
    return out[:head] + marker + (out[-tail:] if tail else "")


def reply_text(reply: Any) -> str:
    """The model's reply as text. A client returning None, bytes, or an OpenAI-style message
    dict must not crash the agent loop with a TypeError."""
    if isinstance(reply, str):
        return reply
    if reply is None:
        return ""
    if isinstance(reply, (bytes, bytearray)):
        return bytes(reply).decode("utf-8", "replace")
    if isinstance(reply, dict):
        c = reply.get("content")
        if isinstance(c, str):
            return c
        if isinstance(reply.get("message"), dict) and isinstance(reply["message"].get("content"), str):
            return reply["message"]["content"]
        return json.dumps(reply, ensure_ascii=False, default=str)
    return str(reply)


DEFAULT_CONTEXT_CHARS = 120000
CONTEXT_KEEP_TAIL = 4  # newest messages never elided
ELIDED = "[older tool output elided to fit the context window]"


def context_chars() -> int:
    """AVRONE_CONTEXT_CHARS (default 120000), clamped to 8000..2000000; junk = default.
    Ollama silently cuts an over-long prompt from the FRONT, which drops the system prompt
    (the tool docs and the JSON action format) and leaves the agent improvising."""
    try:
        n = int(os.environ.get("AVRONE_CONTEXT_CHARS", DEFAULT_CONTEXT_CHARS))
    except (TypeError, ValueError):
        n = DEFAULT_CONTEXT_CHARS
    return max(8000, min(n, 2000000))


def compact_messages(messages: list[dict[str, Any]], limit: int) -> int:
    """Elide the oldest tool-result bodies, in place, until the history fits in limit chars.
    The system prompt, the first user turn, and the newest CONTEXT_KEEP_TAIL messages are
    never touched, and assistant turns are kept, so the agent still sees what it did.
    Returns how many messages were elided."""
    def size() -> int:
        return sum(len(m.get("content") or "") for m in messages if isinstance(m, dict))
    if size() <= limit:
        return 0
    n = 0
    for i in range(2, max(2, len(messages) - CONTEXT_KEEP_TAIL)):
        m = messages[i]
        c = m.get("content") if isinstance(m, dict) else None
        if m.get("role") != "user" or not isinstance(c, str) or not c.startswith("TOOL RESULT"):
            continue
        head = c.split("\n", 1)[0]
        new = f"{head}\n{ELIDED}"
        if len(new) >= len(c):
            continue
        m["content"] = new
        n += 1
        if size() <= limit:
            break
    return n


def out_of_turns_result(reply: Any, name: str, steps: int) -> str:
    """The answer to the last "out of turns" turn. Models often answer in the JSON action
    format anyway: take a final's text, and never hand back a raw tool call or nothing."""
    text = reply_text(reply).strip()
    action = parse_action(text) if text else None
    if isinstance(action, dict):
        if "final" in action and action["final"] not in (None, ""):
            f = action["final"]
            return f if isinstance(f, str) else json.dumps(f, ensure_ascii=False, default=str)
        text = ""
    return text or f"[{name} ran out of turns ({steps} steps) without a final answer]"


def _call_key(tool: Any, args: Any, out: str) -> str | None:
    try:
        return json.dumps([tool, args, out], sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


DEFAULT_MAX_CALLS_PER_TURN = 4
HARD_MAX_CALLS_PER_TURN = 8
# After this many consecutive ARG ERRORs from one agent, stop blocking its calls on the
# schema (the tools' own error handling takes over) so a confused model can't stall a run.
ARG_ERROR_BUDGET = 3


def max_calls_per_turn() -> int:
    """AVRONE_MAX_CALLS_PER_TURN (default 4), clamped to 1..8; junk falls back to the default."""
    try:
        n = int(os.environ.get("AVRONE_MAX_CALLS_PER_TURN", DEFAULT_MAX_CALLS_PER_TURN))
    except (TypeError, ValueError):
        n = DEFAULT_MAX_CALLS_PER_TURN
    return max(1, min(n, HARD_MAX_CALLS_PER_TURN))


def _normalize_subtasks(reply: str, n: int) -> list[dict[str, str]]:
    """Turn whatever the planner replied into 1..n clean {"role": str, "focus": str} dicts.

    Model output is untrusted: subtasks may be missing, not a list, hold non-dicts, null or
    numeric roles, nested focus objects, or far more than n items. Anything unusable falls
    back to n independent solvers (pure Heavy style).
    """
    raw: Any = []
    try:
        m = re.search(r"\{.*\}", reply or "", re.S)
        obj = json.loads(m.group(0)) if m else {}
        raw = obj.get("subtasks", []) if isinstance(obj, dict) else []
    except (json.JSONDecodeError, RecursionError):
        raw = []
    subtasks: list[dict[str, str]] = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict) or not s.get("focus"):
            continue
        focus = s["focus"] if isinstance(s["focus"], str) else json.dumps(s["focus"], default=str)
        role = s.get("role")
        role = role.strip() if isinstance(role, str) and role.strip() else f"Agent{len(subtasks) + 1}"
        subtasks.append({"role": role[:60], "focus": focus[:2000]})
        if len(subtasks) >= n:
            break
    if not subtasks:
        subtasks = [{"role": f"Solver{i + 1}", "focus": "Solve the whole task independently."} for i in range(n)]
    return subtasks


class TrinityCore:
    def __init__(self, settings: Settings, llm: LLM, runner: Runner, codex: Codex,
                 tools: ToolRegistry | None = None):
        self.settings = settings
        self.tools = tools or builtin_registry()
        self.llm = llm
        self.runner = runner
        self.codex = codex
        self._counter = 0
        # Bound concurrent model calls (squads, sub-agents and leads all share it).
        self._sem = asyncio.Semaphore(max(1, settings.max_parallel))
        # Advisory health/thermal governor: consulted before a squad or deep wave spawns its
        # workers, so a hot or loaded node runs fewer of them at once (never fewer than 1).
        self.governor = Governor.from_settings(settings)
        self.active: dict[str, dict[str, Any]] = {}  # agent name -> status, for the HUD
        self.inbox: dict[str, list[str]] = {}  # agent name -> messages waiting for its next step
        self.run_agents: dict[str, int] = {}  # run id -> agents started (AVRONE_MAX_AGENTS_PER_RUN)
        self.squad_roles: dict[str, list[dict[str, str]]] = {}  # run id -> squad roles/focuses

    def message_agent(self, name: str, text: str, sender: str = "operator") -> bool:
        """Queue a message for a live agent; it reads it before its next step. False if not live."""
        a = self.active.get(name)
        if not a or a.get("status") != "working":
            return False
        self.inbox.setdefault(name, []).append(f"MESSAGE FROM {sender}: {text}")
        self.codex.emit(a.get("run_id", "fleet"), "agent.message", name, sender=sender, text=text[:2000])
        return True

    def _name(self, role: str) -> str:
        self._counter += 1
        return f"{re.sub(r'[^A-Za-z0-9]', '', str(role or ''))[:16] or 'Agent'}-{self._counter}"

    def _claim_identity(self, run_id: str, name: str, agent_id: Any) -> tuple[str, str | None]:
        """(agent dir id, pinned memory namespace or None) for a new agent of `run_id`.

        Roles that sanitize alike ("Builder!" / "Builder?") would make two LIVE agents of one run
        share a home dir and memory namespace. The first keeps the plain id (so the same role
        keeps its persistence across runs); later concurrent ones get a -2, -3... suffix on both.
        An agent that has finished frees its id: a sequential re-spawn is the same agent."""
        base = agentfs.sanitize_agent_id(agent_id)
        ns_base = memstore.agent_namespace(name)
        live = [a for a in self.active.values()
                if a.get("run_id") == run_id and a.get("status") == "working"]
        ids = {a.get("agent_id") for a in live}
        spaces = {a.get("memory_ns") for a in live}
        k = 1
        aid, ns = base, ns_base
        while aid in ids or ns in spaces:
            k += 1
            sfx = f"-{k}"
            aid = base[:agentfs.MAX_ID_LEN - len(sfx)].rstrip("-_") + sfx
            ns = ns_base[:63 - len(sfx)].rstrip("-_") + sfx
        return aid, (ns if k > 1 else None)

    def _attach_agent_dir(self, ctx: AgentContext, agent_id: Any) -> None:
        """Give the agent its own persistent dir. Never fatal: on failure the agent runs as before."""
        if not agentfs.agent_fs_enabled():
            return
        ws = getattr(self.runner, "workspace", None) or getattr(self.settings, "workspace", None)
        if ws is None:
            return
        try:
            ctx.agent_id = agentfs.sanitize_agent_id(agent_id)
            ctx.agent_dir = agentfs.agent_dir(Path(ws), ctx.agent_id)
        except (OSError, agentfs.AgentFSError) as e:
            ctx.agent_id, ctx.agent_dir = None, None
            self.codex.emit(ctx.run_id, "error", ctx.name, message=f"agent dir unavailable: {e}")

    async def _chat(self, messages: list[dict[str, str]], **kw: Any) -> str:
        async with self._sem:
            return await self.llm.chat(messages, **kw)

    # ------------------------------------------------------------ one agent
    async def run_agent(self, run_id: str, role: str, task: str, focus: str | None = None,
                        parent: str | None = None, depth: int = 0, system: str | None = None,
                        agent_id: str | None = None, lineage: list[str] | None = None,
                        run_task: str | None = None, context_pack: str = "",
                        context_mode: str | None = None, parent_dir: Path | None = None,
                        parent_capability: dict | None = None) -> str:
        name = self._name(role)
        ctx = AgentContext(self, run_id, name, depth, role=role, focus=focus, run_task=run_task or task,
                           parent_name=parent, lineage=list(lineage or []))
        if parent_capability is not None:  # capgate: a child only ever holds a subset of its parent
            from . import capgate
            try:
                ctx.capability = capgate.attenuate_for_child(parent_capability, name, role)
            except capgate.CapDenied as e:
                return f"ERROR: could not delegate a capability to {role}: {e}"
        self.run_agents[run_id] = self.run_agents.get(run_id, 0) + 1
        claimed, ctx.memory_namespace = self._claim_identity(run_id, name, agent_id or role)
        self._attach_agent_dir(ctx, claimed)
        if parent_dir is not None and ctx.agent_dir is not None:
            ctx.parent_dir = Path(parent_dir)
        # agent_id is recorded even when agent fs is off, so the claim (and fleet.idle_agents'
        # busy check) sees it; no await between claim and registration, so no race.
        self.active[name] = {"role": role, "status": "working", "parent": parent, "run_id": run_id,
                             "task": (focus or task)[:300], "depth": depth, "started": time.time(),
                             "agent_id": claimed,
                             "memory_ns": ctx.memory_namespace or memstore.agent_namespace(name)}
        self.codex.emit(run_id, "agent.spawn", name, role=role, focus=focus or task, parent=parent, depth=depth,
                        context_mode=context_mode, context_chars=len(context_pack or ""),
                        lineage=ctx.lineage, share_dir=ctx.parent_dir is not None)
        prompt = (system or WORKER_PROMPT).format(
            name=name, role=role, task=task, focus=focus or task, tools=self.tools.docs(),
            steps=self.settings.max_steps, max_calls=max_calls_per_turn())
        if context_pack:
            prompt += "\n\n" + context_pack
        if ctx.parent_dir is not None:
            prompt += ("\n\nYour parent's agent dir is readable (not writable) under the `parent/` "
                       "prefix in read_file/list_files/search_files/diff_files.")
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Begin."},
        ]
        result = ""
        timeout = float(getattr(self.settings, "agent_timeout", 0) or 0)
        try:
            loop = self._agent_loop(run_id, name, ctx, messages)
            result = await (asyncio.wait_for(loop, timeout) if timeout > 0 else loop)
        except LLMError as e:
            result = f"[{name} failed: {e}]"
            self.codex.emit(run_id, "error", name, message=str(e))
        except Exception as e:  # any other crash stays inside this agent; the squad goes on
            if isinstance(e, asyncio.TimeoutError) and timeout > 0:
                why = f"timed out after {timeout:g}s"
            else:
                why = f"{type(e).__name__}: {e}"
            result = f"[{name} failed: {why}]"
            self.codex.emit(run_id, "error", name, message=why)
        finally:
            # Always retire the agent (also on cancellation) so the HUD never shows a ghost
            # "working" agent and message_agent stops accepting messages for it.
            if name in self.active:
                self.active[name]["status"] = "done"
            dropped = self.inbox.pop(name, None)
            if dropped:  # arrived during the agent's last turn: it never read them; say so
                self.codex.emit(run_id, "agent.message_dropped", name, count=len(dropped),
                                messages=[m[:500] for m in dropped[:5]])
        self.codex.emit(run_id, "agent.done", name, result=result[:6000])
        return result

    async def _agent_loop(self, run_id: str, name: str, ctx: AgentContext,
                          messages: list[dict[str, str]]) -> str:
        result = ""
        nudges = 0
        repeats, last_key = 0, None
        budget = {"arg_errors": 0, "spent": 0}  # consecutive ARG ERRORs; spent = stop validating
        for step in range(self.settings.max_steps):
            for note in self.inbox.pop(name, []):
                messages.append({"role": "user", "content": reply_text(note) + "\nAct on this now, inside your own environment."})
            elided = compact_messages(messages, context_chars())
            if elided:
                self.codex.emit(run_id, "context.compact", name, elided=elided, step=step)
            reply = reply_text(await self._chat(messages, session=f"avrone:{run_id}:{name}"))
            messages.append({"role": "assistant", "content": reply})
            if not reply.strip():
                # An empty turn (gpt-oss does this after a long tool result, or when all its
                # tokens went to reasoning) is not an answer: accepting it ends the agent with
                # nothing. Ask again, bounded like the malformed-action nudge.
                if nudges < 2:
                    nudges += 1
                    self.codex.emit(run_id, "action.empty", name, step=step)
                    messages.append({"role": "user", "content": (
                        "Your last message was empty. Reply with ONE JSON object: "
                        "{\"tool\": ..., \"args\": ...} or {\"final\": ...}.")})
                    continue
                result = ""
                break
            action = parse_action(reply)
            if action is None:
                # A reply that clearly MEANT to call a tool (a json fence or a "tool"/"final"
                # key) but didn't parse is a malformed action, not a final answer. Accepting it
                # as the result silently throws the whole run away, so nudge once and retry
                # instead (bounded, so a model that keeps failing still terminates).
                looks_like_action = ('"tool"' in reply or '"final"' in reply
                                     or re.search(r"```(?:json)?\s*\{", reply))
                if looks_like_action and nudges < 2:
                    nudges += 1
                    self.codex.emit(run_id, "action.malformed", name, step=step)
                    messages.append({"role": "user", "content": (
                        "Your last message was not valid JSON, so no tool ran. Resend ONLY one "
                        "JSON object, either {\"tool\": ..., \"args\": ...} or {\"final\": ...}. "
                        "If a field holds code or multi-line text, escape every newline as \\n and "
                        "every quote as \\\" so it is strict JSON. No prose, no code fences.")})
                    continue
                # Genuine prose answer: accept it as the final result.
                result = reply.strip()
                break
            action = normalize_action(self.tools, action)
            if "final" in action:
                result = "" if action["final"] is None else str(action["final"])
                break
            if "tools" in action and "tool" not in action:
                calls = action.get("tools")
                if not isinstance(calls, list) or not calls:
                    messages.append({"role": "user", "content": (
                        "ERROR: \"tools\" must be a non-empty list of {\"tool\": ..., \"args\": ...} "
                        "objects. Nothing ran.")})
                    continue
                cap = max_calls_per_turn()
                dropped = max(0, len(calls) - cap)
                calls = calls[:cap]
                self.codex.emit(run_id, "tool.batch", name, count=len(calls), dropped=dropped, step=step)
                outs = await asyncio.gather(*(self._one_call(run_id, name, ctx, c, step, budget)
                                              for c in self._prechecked(run_id, name, calls, step, budget)))
                total = len(outs)
                parts = [f"TOOL RESULT [{i + 1}/{total}] ({t}):\n{o}" for i, (t, o) in enumerate(outs)]
                if dropped:
                    parts.append(f"NOTE: {dropped} more call(s) were skipped (limit {cap} per turn); "
                                 "send them next turn if still needed.")
                messages.append({"role": "user", "content": "\n\n".join(parts)})
                continue
            [call] = self._prechecked(run_id, name, [action], step, budget)
            tool, out = await self._one_call(run_id, name, ctx, call, step, budget)
            key = _call_key(tool, call.get("args"), out)
            repeats = repeats + 1 if key is not None and key == last_key else 1
            last_key = key
            note = ""
            if repeats >= REPEAT_CALL_LIMIT:
                self.codex.emit(run_id, "tool.repeat", name, tool=tool, count=repeats, step=step)
                note = (f"\n\nNOTE: this is the {repeats}th identical call in a row with the same "
                        "result. Repeating it will not change anything: try a different approach "
                        "or give your final answer now.")
            messages.append({"role": "user", "content": f"TOOL RESULT ({tool}):\n{out}{note}"})
        else:
            messages.append({"role": "user", "content": "Out of turns. Give your final result now as plain text."})
            compact_messages(messages, context_chars())
            self.codex.emit(run_id, "agent.out_of_turns", name, steps=self.settings.max_steps)
            result = out_of_turns_result(await self._chat(messages, session=f"avrone:{run_id}:{name}"),
                                         name, self.settings.max_steps)
        return result

    def _prechecked(self, run_id: str, name: str, calls: list[Any], step: int,
                    budget: dict[str, int]) -> list[dict[str, Any]]:
        """Validate calls in order (before anything runs) and tag each with its arg error.

        Each call becomes {"tool", "args", "error"}; error is None when it may run. Errors
        count toward the agent's consecutive ARG_ERROR_BUDGET (reset by a clean call); once
        it is spent, schema checks stop for the rest of this agent's loop and bad-args calls
        run as before (unknown tools still can't run)."""
        checked: list[dict[str, Any]] = []
        for c in calls:
            if not isinstance(c, dict):
                tool, args = "?", {}
                problem: tuple[str, str] | None = (
                    "bad_args", "ARG ERROR (?): each batch entry must be {\"tool\": ..., \"args\": {...}}")
            else:
                tool, args = str(c.get("tool")), c.get("args")
                args = {} if args is None else args
                args = coerce_args(self.tools, tool, c, args)
                problem = self.tools.check(tool, args)
                if problem and problem[0] == "unknown_tool":
                    tool = clip_tool_name(tool)  # never echoed whole into codex or the next prompt
            if problem and problem[0] == "bad_args" and budget["spent"] and isinstance(c, dict):
                problem = None  # budget spent: stop nudging, let the tool handle its args
            if problem:
                budget["arg_errors"] += 1
                if budget["arg_errors"] >= ARG_ERROR_BUDGET:
                    budget["spent"] = 1
                self.codex.emit(run_id, "tool.arg_error", name, tool=tool, reason=problem[0],
                                message=problem[1][:1000], step=step, consecutive=budget["arg_errors"])
            else:
                budget["arg_errors"] = 0
            checked.append({"tool": tool, "args": args, "error": problem[1] if problem else None})
        return checked

    async def _one_call(self, run_id: str, name: str, ctx: AgentContext, call: dict[str, Any],
                        step: int, budget: dict[str, int]) -> tuple[str, str]:
        """Run one pre-checked call, logged as tool.call/tool.result. Never raises."""
        tool, args = call["tool"], call["args"]
        self.codex.emit(run_id, "tool.call", name, tool=tool, args=args, step=step)
        if call["error"] is not None:
            out = call["error"]
        else:
            try:
                out = await self.tools.call(ctx, tool, args if isinstance(args, dict) else {})
            except Exception as e:  # a crash in one call is that call's result, never the turn's
                out = f"ERROR: {type(e).__name__}: {e}"
            out = out if isinstance(out, str) else str(out)
        out = clip_output(out, tool_output_max())
        try:
            ctx.recent_calls.append((tool, args, out[:2000]))  # ctxspawn ring buffer
        except AttributeError:  # pragma: no cover - foreign ctx objects
            pass
        self.codex.emit(run_id, "tool.result", name, tool=tool, output=out[:4000])
        return tool, out

    def _worker_limit(self, run_id: str, n: int) -> int:
        """Ask the governor how many of n workers may run at once; never raises, never < 1."""
        try:
            limit = int(self.governor.target(n))
        except Exception:
            return n
        limit = max(1, min(n, limit))
        if limit < n:
            self.codex.emit(run_id, "governor.throttle", "Governor", workers=n, parallel=limit,
                            reason=self.governor.last_reason)
        return limit

    def _privileged_run(self) -> bool:
        if getattr(self, "fleet_privileged", ""):
            return True
        try:
            from .tools_extra import PRIV_GRANT
            return bool(PRIV_GRANT.get())
        except Exception:  # pragma: no cover
            return True  # unsure = treat as privileged = never offload

    def _offload_plan(self, run_id: str, n: int) -> "tuple[int, dict[int, str]] | None":
        """edgeroute: (governor limit, {worker index: node id}) when fleet offload is on, else None.
        The overflow (indices >= limit) goes to healthy edge nodes; privileged runs never offload."""
        fleet = getattr(self, "fleet", None)
        try:
            if fleet is None or not getattr(fleet, "enabled", False) or fleet.offload_mode() not in ("spill", "spread"):
                return None
        except Exception:
            return None
        if self._privileged_run() or n <= 1:
            return None
        limit = self._worker_limit(run_id, n)
        if limit >= n:
            return limit, {}
        try:
            targets = list(fleet.offload_plan(n - limit, int(getattr(self, "fleet_hops", 0) or 0)))
        except Exception:
            targets = []
        return limit, {limit + i: str(t) for i, t in enumerate(targets[:n - limit])}

    async def _offloaded(self, run_id: str, node: str, s: dict[str, str], task: str,
                         gate: asyncio.Semaphore, local: Any) -> str:
        """Run worker `s` on `node`; on any remote failure run it here, behind the local gate."""
        try:
            return await self.fleet.offload_worker(run_id, node, str(s.get("role")), task, str(s["focus"]),
                                                   int(getattr(self, "fleet_hops", 0) or 0))
        except asyncio.CancelledError:
            raise
        except Exception:
            async with gate:
                return await local()

    def _netsquad_plan(self, run_id: str, n: int, dispatch: str | None = None
                       ) -> "tuple[int, dict[int, str]] | None":
        """netsquad: (governor limit, {subtask index: node id}) when squad dispatch is network or
        hybrid, else None (local = unchanged). Privileged runs never leave the node."""
        fleet = getattr(self, "fleet", None)
        try:
            if fleet is None or not getattr(fleet, "enabled", False) or fleet.squad_dispatch(dispatch) == "local":
                return None
        except Exception:
            return None
        if self._privileged_run() or n <= 0:
            return None
        limit = self._worker_limit(run_id, n)
        try:
            remote = dict(fleet.netsquad_plan(n, limit, int(getattr(self, "fleet_hops", 0) or 0),
                                              self._netsquad_path(), dispatch, self._netsquad_budget()))
        except Exception:
            remote = {}
        return limit, {int(i): str(v) for i, v in remote.items() if 0 <= int(i) < n}

    def _netsquad_path(self) -> list[str]:
        p = getattr(self, "netsquad_path", None)
        return [str(x) for x in p] if isinstance(p, list) else []

    def _netsquad_budget(self) -> int | None:
        b = getattr(self, "netsquad_budget", None)
        return b if isinstance(b, int) and not isinstance(b, bool) else None

    async def _netsquad(self, run_id: str, i: int, node: str, s: dict[str, str], task: str, share: int,
                        gate: asyncio.Semaphore, local: Any, placed: dict[int, dict[str, Any]]) -> str:
        """Run subtask `s` as a sub-squad on `node`; on any remote failure run it here, behind the gate."""
        try:
            r = await self.fleet.netsquad_worker(run_id, node, str(s.get("role")), task, str(s["focus"]),
                                                 int(getattr(self, "fleet_hops", 0) or 0),
                                                 self._netsquad_path(), share)
            placed[i] = {"node": r.get("node", node), "route": list(r.get("route") or [])}
            return str(r["answer"])
        except asyncio.CancelledError:
            raise
        except Exception:
            async with gate:
                return await local()

    async def _gather_agents(self, run_id: str, coros: list[Any], limit: int | None = None) -> list[str]:
        """Run agents in parallel; one that still blows up becomes a failure note, never a crash.

        The governor may cap how many run at once: the rest wait at a local gate (workers'
        own sub-agents bypass it, so the gate can never deadlock). A caller that already asked
        the governor passes its `limit`."""
        if limit is None:
            limit = self._worker_limit(run_id, len(coros)) if coros else 0
        if 0 < limit < len(coros):
            gate = asyncio.Semaphore(limit)

            async def gated(c: Any) -> Any:
                try:
                    async with gate:
                        return await c
                finally:
                    c.close()  # no-op once awaited; avoids a never-awaited warning on cancel
            coros = [gated(c) for c in coros]
        out = await asyncio.gather(*coros, return_exceptions=True)
        results: list[str] = []
        for i, r in enumerate(out):
            if isinstance(r, BaseException):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                self.codex.emit(run_id, "error", "Trinity", message=f"agent {i + 1} crashed: {r!r}")
                r = f"[agent {i + 1} failed: {type(r).__name__}: {r}]"
            results.append(str(r))
        return results

    async def _ck_step(self, ck: "Checkpoint | None", key: str, factory: Any) -> str:
        """Run one worker unless the checkpoint already holds its good result; record it after.

        Failure notes are never recorded, so a resumed run retries those workers."""
        if ck is not None:
            cached = ck.get(key)
            if isinstance(cached, str) and not _reviewer_failed(cached):
                return cached
        result = await factory()
        if ck is not None and isinstance(result, str) and not _reviewer_failed(result):
            await asyncio.to_thread(ck.put, key, result)  # rewrites the whole file: off the loop
        return result

    def _ck_open(self, checkpoint: Any, task: str, mode: str, run_id: str | None,
                 **extra: Any) -> tuple["Checkpoint | None", str | None, Any]:
        """(checkpoint view, run_id to use, stored final result or None). Never raises."""
        try:
            ck = Checkpoint.coerce(checkpoint)
            if ck is None:
                return None, run_id, None
            prev = ck.resume_point(task, mode, **extra)
            if prev and not run_id:
                run_id = prev.get("run_id")
            return ck, run_id, (ck.get("result") if prev else None)
        except Exception:  # a bad checkpoint path must not sink the run
            return None, run_id, None

    # ------------------------------------------------------------ the squad
    async def plan(self, run_id: str, task: str, n: int) -> list[dict[str, str]]:
        self.codex.emit(run_id, "lead.plan", "Planner", squad_size=n)
        reply = await self._chat([
            {"role": "system", "content": PLANNER_PROMPT.format(n=n)},
            {"role": "user", "content": f"TASK: {task}"},
        ], temperature=0.4, session=f"avrone:{run_id}:planner")
        subtasks = _normalize_subtasks(reply, n)
        self.codex.emit(run_id, "lead.planned", "Planner", subtasks=subtasks)
        return subtasks

    async def squad(self, task: str, n: int | None = None, run_id: str | None = None,
                    checkpoint: "Checkpoint | str | os.PathLike[str] | None" = None,
                    dispatch: str | None = None) -> dict[str, Any]:
        """Plan, run the squad in parallel, merge with the lead.

        `dispatch` (local|network|hybrid) overrides AVRONE_SQUAD_DISPATCH for this run (netsquad).

        With `checkpoint` (a path or Checkpoint), the plan, every finished worker result and
        the final result are recorded; calling again with the same task and file resumes:
        finished workers are not re-run and a finished run is returned as is."""
        n = max(1, min(n or self.settings.squad_size, 8))
        ck, run_id, done = self._ck_open(checkpoint, task, "squad", run_id, n=n)
        if isinstance(done, dict):
            return done
        run_id = run_id or uuid.uuid4().hex[:10]
        if ck is not None:
            ck.begin(task, "squad", run_id, n=n)
        self.codex.emit(run_id, "run.start", "Trinity", prompt=task, mode="squad", squad_size=n)
        subtasks = ck.get("subtasks") if ck is not None else None
        if ck is not None and isinstance(subtasks, list) and subtasks:
            finished = sum(1 for i in range(len(subtasks)) if isinstance(ck.get(f"worker.{i}"), str))
            self.codex.emit(run_id, "checkpoint.resume", "Trinity", path=str(ck.path),
                            workers_done=finished, workers=len(subtasks))
        else:
            try:
                subtasks = await self.plan(run_id, task, n)
            except LLMError as e:
                self.codex.emit(run_id, "error", "Planner", message=str(e))
                self.codex.emit(run_id, "run.done", "Trinity", answer=f"Model backend error: {e}")
                return {"run_id": run_id, "answer": f"Model backend error: {e}", "agents": []}
            if ck is not None:
                await asyncio.to_thread(ck.put, "subtasks", subtasks)

        self.squad_roles[run_id] = [{"role": str(s.get("role")), "focus": str(s.get("focus", ""))}
                                    for s in subtasks if isinstance(s, dict)]
        # in a thread: it parses the whole memory store (200k lines: ~1 s with the node frozen)
        lessons = await asyncio.to_thread(self._lessons_block, run_id, task)
        if lessons:
            subtasks_run = [dict(s, focus=s["focus"] + lessons) for s in subtasks]
        else:
            subtasks_run = subtasks
        placed: dict[int, dict[str, Any]] = {}
        nplan = self._netsquad_plan(run_id, len(subtasks), dispatch)
        plan = nplan if nplan is not None else self._offload_plan(run_id, len(subtasks))
        net = nplan is not None
        if plan and plan[1]:
            limit, remote = plan
            gate = asyncio.Semaphore(limit)
            share = self.fleet.netsquad_share(len(remote), self._netsquad_budget()) if net else 0

            async def gated(local: Any) -> str:
                async with gate:
                    return await local()
            coros = []
            for i, s in enumerate(subtasks_run):
                local = (lambda s=s: self.run_agent(run_id, s["role"], task, focus=s["focus"]))
                if i in remote and net:
                    fac = (lambda i=i, s=s, node=remote[i], local=local:
                           self._netsquad(run_id, i, node, s, task, share, gate, local, placed))
                elif i in remote:
                    fac = (lambda s=s, node=remote[i], local=local: self._offloaded(run_id, node, s, task, gate, local))
                else:
                    fac = (lambda local=local: gated(local))
                coros.append(self._ck_step(ck, f"worker.{i}", fac))
            results = await self._gather_agents(run_id, coros, limit=len(coros))
        else:
            results = await self._gather_agents(run_id, [
                self._ck_step(ck, f"worker.{i}",
                              lambda s=s: self.run_agent(run_id, s["role"], task, focus=s["focus"]))
                for i, s in enumerate(subtasks_run)], limit=plan[0] if plan else None)
        self.squad_roles.pop(run_id, None)
        xinfo: dict[str, Any] | None = None
        rounds = bridgemod.bridge_rounds()
        if rounds > 0:
            results, xinfo = await self._cross_examine(run_id, task, subtasks, list(results), rounds)

        def via(i: int) -> str:
            p = placed.get(i)
            return f" [node {p['node']} via {'/'.join(p['route'])}]" if p else ""
        reports = "\n\n".join(
            f"### Agent {i + 1}: {s.get('role')} (focus: {s['focus']}){via(i)}\n{r}"
            for i, (s, r) in enumerate(zip(subtasks, results)))
        lead_in = f"TASK:\n{task}\n\nAGENT REPORTS:\n{reports}"
        if xinfo is not None and xinfo.get("digest"):
            lead_in += ("\n\nBRIDGE DIGEST (the agents cross-examined each other; weigh what they "
                        "concluded together and settle the unresolved points):\n" + xinfo["digest"])
        self.codex.emit(run_id, "lead.merge", "Lead", agents=len(results))
        try:
            answer = await self._chat([
                {"role": "system", "content": LEAD_PROMPT},
                {"role": "user", "content": lead_in},
            ], lead=True, temperature=0.3, session=f"avrone:{run_id}:lead")
            if _weak_lead(answer, reports):
                self.codex.emit(run_id, "lead.retry", "Lead", reason="narration or too thin")
                answer = await self._chat([
                    {"role": "system", "content": LEAD_PROMPT},
                    {"role": "user", "content": lead_in},
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": "That is a plan, not an answer. You cannot run anything. "
                     "Write the complete final answer now from the reports: exact findings, what was "
                     "verified, what is only claimed, what is unknown."},
                ], lead=True, temperature=0.2, session=f"avrone:{run_id}:lead2")
        except Exception as e:  # never throw away the squad's work because the merge failed
            self.codex.emit(run_id, "error", "Lead", message=f"{type(e).__name__}: {e}")
            answer = f"Lead failed ({e}). Raw agent reports:\n\n{reports}"
        self.codex.emit(run_id, "run.done", "Trinity", answer=answer)
        out = {"run_id": run_id, "answer": answer,
               "agents": [{"role": s.get("role"), "focus": s["focus"], "result": r, **placed.get(i, {})}
                          for i, (s, r) in enumerate(zip(subtasks, results))]}
        if xinfo is not None:
            out["bridge"] = {k: v for k, v in xinfo.items() if k != "digest"}
        learned = await asyncio.to_thread(self._distill, run_id, task)  # memory rewrite: off the loop
        if learned:
            out["lessons_learned"] = learned
        if ck is not None and not answer.startswith("Lead failed ("):
            await asyncio.to_thread(ck.put, "result", out)  # a failed merge is retried on resume
        return out

    # ------------------------------------------------------------ deep analysis
    async def deep(self, task: str, first_answer: str | None = None, n: int | None = None,
                   run_id: str | None = None, rounds: int | None = None,
                   checkpoint: "Checkpoint | str | os.PathLike[str] | None" = None) -> dict[str, Any]:
        """Stress-test and extend an existing answer to `task`, in up to `rounds` waves.

        Round 1 is the classic second wave. Each later round reviews the previous round's
        corrected answer (its Bottom line) and is told which corrections are already applied.
        The loop stops early when a round's lead reports no correction not seen before.

        With `checkpoint`, the first wave (its own squad checkpoint), each reviewer result and
        each completed round are recorded, so an interrupted run resumes mid-round."""
        # n=None -> configured size; n<=0 -> zero reviewers (the answer is reported unreviewed).
        n = max(0, min(self.settings.deep_squad_size if n is None else int(n), MAX_DEEP))
        max_rounds = resolve_deep_rounds(rounds, self.settings)
        given = first_answer if first_answer and str(first_answer).strip() else None
        ck, run_id, done = self._ck_open(checkpoint, task, "deep", run_id, n=n, first_answer=given)
        if isinstance(done, dict):
            return done
        if ck is not None and given is None:
            # The first-wave squad is sized by settings.squad_size; if that changed since the
            # checkpoint was written, the wave re-runs (its own meta no longer matches) and every
            # cached review of the OLD first answer is stale too: start the deep scope fresh.
            w1 = ck.get("wave1.meta")
            if isinstance(w1, dict) and w1.get("n") != max(1, min(self.settings.squad_size, 8)):
                ck.clear()
        run_id = run_id or uuid.uuid4().hex[:10]
        if ck is not None:
            ck.begin(task, "deep", run_id, n=n, first_answer=given)
        self.codex.emit(run_id, "run.start", "Trinity", prompt=task, mode="deep", squad_size=n,
                        has_first_answer=first_answer is not None, max_rounds=max_rounds)
        first_run_id = None
        if first_answer is None or not str(first_answer).strip():
            first_run_id = f"{run_id}-wave1"
            self.codex.emit(run_id, "deep.first_wave", "Trinity", first_run_id=first_run_id)
            if ck is not None:
                first = await self.squad(task, run_id=first_run_id, checkpoint=ck.scope("wave1."))
            else:
                first = await self.squad(task, run_id=first_run_id)
            first_answer = first["answer"]
            self.codex.emit(run_id, "deep.first_wave_done", "Trinity", first_run_id=first_run_id,
                            answer=first_answer[:6000])

        # in a thread: it parses the whole memory store (200k lines: ~1 s with the node frozen)
        lessons = await asyncio.to_thread(self._lessons_block, run_id, task)
        angles = [dict(a, focus=a["focus"] + lessons) for a in DEEP_ANGLES[:n]] if lessons else DEEP_ANGLES[:n]
        current = first_answer
        seen: list[str] = []
        history: list[dict[str, Any]] = []
        all_agents: list[dict[str, Any]] = []
        report = ""
        converged = False
        lead_ok = True
        stop_reason = "cap"
        total_ok = total_failed = 0
        start = 1
        saved = ck.get("rounds") if ck is not None else None
        if ck is not None and isinstance(saved, dict) and saved.get("history"):
            history, all_agents, seen = saved["history"], saved["all_agents"], saved["seen"]
            current, report = saved["current"], saved["report"]
            converged, lead_ok = saved["converged"], saved["lead_ok"]
            total_ok, total_failed = saved["total_ok"], saved["total_failed"]
            stop_reason = saved.get("stop_reason") or "cap"
            start = max_rounds + 1 if saved.get("stop_reason") else len(history) + 1
            self.codex.emit(run_id, "checkpoint.resume", "DeepLead", path=str(ck.path),
                            rounds_done=len(history), next_round=start)

        async def save_rounds(stop: str | None = None) -> None:
            if ck is not None:
                await asyncio.to_thread(ck.put, "rounds", {"history": history, "all_agents": all_agents, "seen": seen,
                                  "current": current, "report": report, "converged": converged,
                                  "lead_ok": lead_ok, "total_ok": total_ok,
                                  "total_failed": total_failed, "stop_reason": stop})

        for rnd in range(start, max_rounds + 1):
            self.codex.emit(run_id, "deep.round.start", "DeepLead", round=rnd, max_rounds=max_rounds)
            if rnd == 1:
                brief = f"QUESTION:\n{task}\n\nFIRST-WAVE ANSWER TO REVIEW:\n{current}"
            else:
                applied = "\n".join(f"- {c}" for c in seen) or "- None"
                brief = (f"QUESTION:\n{task}\n\nCURRENT ANSWER TO REVIEW (after deep round {rnd - 1}):\n"
                         f"{current}\n\nCORRECTIONS ALREADY APPLIED (do not repeat them):\n{applied}\n\n"
                         "Find anything that is STILL wrong, missing or unsupported.")
            report, results, lead_ok = await self._deep_wave(
                run_id, rnd, brief, angles, current,
                ck=ck.scope(f"round{rnd}.") if ck is not None else None)
            all_agents += [{"role": a["role"], "focus": a["focus"], "result": r, "round": rnd,
                            "ok": not _reviewer_failed(r)} for a, r in zip(angles, results)]
            ok = sum(not _reviewer_failed(r) for r in results)
            failed = len(results) - ok
            total_ok += ok
            total_failed += failed
            found = _corrections(report) if lead_ok else []
            new = [c for c in found if c not in seen]
            seen += new
            # A failed lead tells us nothing: not converged, and no basis for another round.
            # Nothing reviewed is not the same as nothing wrong: no usable review never converges.
            converged = lead_ok and ok > 0 and not new
            history.append({"round": rnd, "report": report, "corrections": found, "new_corrections": new,
                            "converged": converged, "lead_ok": lead_ok,
                            "reviewers_ok": ok, "reviewers_failed": failed})
            self.codex.emit(run_id, "deep.round.done", "DeepLead", round=rnd, corrections=len(found),
                            new_corrections=new[:20], converged=converged, lead_ok=lead_ok,
                            reviewers_ok=ok, reviewers_failed=failed, report=report[:6000])
            if ok == 0:
                # Nothing usable came back (no reviewers, or every one failed): another
                # round would only repeat that, and the lead had nothing to weigh.
                stop_reason = "no_reviews"
                await save_rounds(stop_reason)
                break
            if converged:
                stop_reason = "converged"
                await save_rounds(stop_reason)
                break
            if not lead_ok:
                stop_reason = "lead_failed"
                break  # not saved: a resume retries this round's lead (its reviews are cached)
            current = _md_section(report, "Bottom line") or report
            await save_rounds()
        last = history[-1]
        confidence = deep_confidence(report, lead_ok, last["reviewers_ok"], last["reviewers_failed"],
                                     converged, len(history))
        why = {"converged": f"converged: no new corrections in round {len(history)}",
               "cap": "stopped at the round cap" + ("" if max_rounds == 1 else ", still finding corrections"),
               "lead_failed": "stopped: the deep lead failed",
               "no_reviews": "stopped: no usable reviews came back"}[stop_reason]
        report = (f"{report.rstrip()}\n\n## Deep rounds\n"
                  f"Rounds run: {len(history)} of {max_rounds} ({why}).\n"
                  f"Reviewers: {total_ok} of {total_ok + total_failed} returned usable reviews.\n"
                  f"Confidence: {confidence}")
        self.codex.emit(run_id, "deep.done", "DeepLead", rounds_run=len(history), max_rounds=max_rounds,
                        converged=converged, stop_reason=stop_reason, confidence=confidence,
                        reviewers_ok=total_ok, reviewers_failed=total_failed)
        self.codex.emit(run_id, "run.done", "Trinity", answer=report)
        out: dict[str, Any] = {
            "run_id": run_id, "mode": "deep", "first_answer": first_answer, "report": report,
            # "answer" mirrors report so clients that read .answer keep working.
            "answer": report,
            "agents": all_agents,
            "rounds_run": len(history), "max_rounds": max_rounds, "converged": converged,
            "stop_reason": stop_reason, "rounds": history, "confidence": confidence,
            "reviewers_ok": total_ok, "reviewers_failed": total_failed,
        }
        if first_run_id:
            out["first_run_id"] = first_run_id
        learned = await asyncio.to_thread(self._distill, run_id, task)  # memory rewrite: off the loop
        if learned:
            out["lessons_learned"] = learned
        if ck is not None and lead_ok:
            await asyncio.to_thread(ck.put, "result", out)
        return out

    async def _deep_wave(self, run_id: str, rnd: int, brief: str,
                         angles: list[dict[str, str]],
                         current: str = "", ck: "Checkpoint | None" = None) -> tuple[str, list[str], bool]:
        """One wave: reviewers in parallel (isolated failures), then the deep lead's report.

        Returns (report, reviewer results, lead_ok). With no usable review the lead is not
        called: the report says the answer stands unverified."""
        self.codex.emit(run_id, "lead.plan", "DeepLead", squad_size=len(angles), round=rnd)
        self.codex.emit(run_id, "lead.planned", "DeepLead", subtasks=angles, round=rnd)
        results = await self._gather_agents(run_id, [
            self._ck_step(ck, f"reviewer.{i}", lambda a=a: self.run_agent(
                run_id, a["role"], brief, focus=a["focus"], system=DEEP_WORKER_PROMPT))
            for i, a in enumerate(angles)])
        reviews = "\n\n".join(f"### Reviewer {i + 1}: {a['role']}\n{r}"
                               for i, (a, r) in enumerate(zip(angles, results)))
        if not any(not _reviewer_failed(r) for r in results):
            why = "no reviewers were assigned" if not angles else "every reviewer failed"
            self.codex.emit(run_id, "error", "DeepLead", message=f"round {rnd}: {why}")
            report = (f"## Verdict\nUnverified: {why}. Confidence: Low\n\n## Corrections\nNone\n\n"
                      f"## Additions\nNone\n\n## Counterpoints\nNone\n\n## Sources\nNone\n\n"
                      f"## Bottom line\n{current}")
            if reviews:
                report += f"\n\n## Reviewer errors\n{reviews}"
            return report, results, True
        self.codex.emit(run_id, "lead.merge", "DeepLead", agents=len(results), round=rnd)
        try:
            report = await self._chat([
                {"role": "system", "content": DEEP_LEAD_PROMPT},
                {"role": "user", "content": f"{brief}\n\nSECOND-WAVE REVIEWS:\n{reviews}"},
            ], lead=True, temperature=0.3,
                session=f"avrone:{run_id}:deeplead" + (f":{rnd}" if rnd > 1 else ""))
        except Exception as e:  # never throw away the reviews because the merge failed
            msg = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {e}"
            self.codex.emit(run_id, "error", "DeepLead", message=msg)
            return f"Deep lead failed ({msg}). Raw reviews:\n\n{reviews}", results, False
        if not isinstance(report, str) or not report.strip():
            self.codex.emit(run_id, "error", "DeepLead", message="empty deep lead reply")
            return f"Deep lead failed (empty reply). Raw reviews:\n\n{reviews}", results, False
        return report, results, True

    # ------------------------------------------------------------ learning bridge
    def _lessons_block(self, run_id: str, task: str) -> str:
        """Relevant lessons from earlier runs for worker prompts ('' when off/none). Never raises."""
        if not bridgemod.lessons_enabled():
            return ""
        try:
            block = bridgemod.lessons_block(self.settings.data_dir, task)
        except Exception:  # noqa: BLE001
            return ""
        if block:
            self.codex.emit(run_id, "lessons.inject", "Bridge", count=block.count("\n- "), chars=len(block))
        return block

    def _distill(self, run_id: str, task: str) -> int:
        """Distill the run bridge's conclusions into the lessons namespace. Never raises."""
        if not bridgemod.lessons_enabled():
            return 0
        try:
            saved = bridgemod.distill_lessons(self.settings.data_dir, bridgemod.for_core(self, run_id), task)
        except Exception as e:  # noqa: BLE001 - learning must never sink a finished run
            self.codex.emit(run_id, "error", "Bridge", message=f"lessons distill failed: {type(e).__name__}: {e}")
            return 0
        if saved:
            self.codex.emit(run_id, "lessons.distill", "Bridge", saved=len(saved))
        return len(saved)

    async def _cross_examine(self, run_id: str, task: str, subtasks: list[dict[str, str]],
                             results: list[str], max_rounds: int) -> tuple[list[str], dict[str, Any]]:
        """Cross-examination: each usable worker gets one short revise turn per round with the
        others' results and the bridge, and may post corrections/conclusions or revise its result.
        Stops early when a round adds no correction. Crashed/empty workers are skipped."""
        b = bridgemod.for_core(self, run_id)
        labels = [f"Agent{i + 1}:{s.get('role')}" for i, s in enumerate(subtasks)]
        for i, r in enumerate(results):
            if not _reviewer_failed(r):
                try:
                    b.post(labels[i], "finding", r, role=str(subtasks[i].get("role") or ""))
                except bridgemod.BridgeError:
                    pass
        info: dict[str, Any] = {"rounds_run": 0, "max_rounds": max_rounds, "stop_reason": "cap",
                                "corrections": 0, "revised": 0}
        for rnd in range(1, max_rounds + 1):
            live = [i for i, r in enumerate(results) if not _reviewer_failed(r)]
            if len(live) < 2:
                info["stop_reason"] = "too_few_workers"
                break
            self.codex.emit(run_id, "bridge.round.start", "Bridge", round=rnd, workers=len(live))
            board = bridgemod.compact(b.read(limit=60), width=400)
            snapshot = list(results)

            async def turn(i: int) -> str:
                others = "\n\n".join(f"### {labels[j]}\n{snapshot[j][:3000]}" for j in live if j != i)
                return await self._chat([
                    {"role": "system", "content": bridgemod.REVISE_PROMPT.format(label=labels[i], rnd=rnd)},
                    {"role": "user", "content": f"TASK:\n{task}\n\nYOUR RESULT:\n{snapshot[i][:4000]}\n\n"
                     f"OTHER AGENTS' RESULTS:\n{others}\n\nBRIDGE POSTS:\n{board}"},
                ], temperature=0.3, session=f"avrone:{run_id}:xexam{rnd}:{i}")
            replies = await asyncio.gather(*(turn(i) for i in live), return_exceptions=True)
            corrections = 0
            for i, rep_ in zip(live, replies):
                if isinstance(rep_, BaseException):
                    if isinstance(rep_, asyncio.CancelledError):
                        raise rep_
                    self.codex.emit(run_id, "error", labels[i], message=f"cross-exam turn failed: {rep_!r}"[:500])
                    continue
                posts, revised = bridgemod.parse_revise(rep_)
                for p in posts:
                    refs = [r for r in p["refs"] if b.get(r) is not None]
                    try:
                        b.post(labels[i], p["kind"], p["text"], refs=refs,
                               role=str(subtasks[i].get("role") or ""))
                    except bridgemod.BridgeError:
                        continue
                    corrections += p["kind"] == "correction"
                if revised and not _reviewer_failed(revised):
                    results[i] = revised
                    info["revised"] += 1
            info["rounds_run"] = rnd
            info["corrections"] += corrections
            self.codex.emit(run_id, "bridge.round.done", "Bridge", round=rnd, corrections=corrections,
                            posts=len(b))
            if corrections == 0:
                info["stop_reason"] = "converged"
                break
        info["posts"] = len(b)
        info["unresolved"] = len(b.unresolved())
        info["digest"] = b.digest()
        return results, info

    async def chat(self, task: str, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex[:10]
        self.codex.emit(run_id, "run.start", "Trinity", prompt=task, mode="chat")
        answer = await self.run_agent(run_id, "Assistant", task)
        self.codex.emit(run_id, "run.done", "Trinity", answer=answer)
        return {"run_id": run_id, "answer": answer, "agents": []}
