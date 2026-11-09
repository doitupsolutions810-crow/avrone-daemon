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
from .tools import ToolRegistry, builtin_registry

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
        except (json.JSONDecodeError, RecursionError):
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
