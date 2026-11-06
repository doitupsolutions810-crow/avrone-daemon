"""flake_probe: measure the nondeterminism of one of the agent's own READ-ONLY perception tools.

    flake_probe {"tool": str, "runs"?: int, "args"?: object, "format"?: "text"|"json"}
        tool   one of FLAKE_PROBE_ALLOWED (read-only tools only; never flake_probe itself)
        runs   how many times to call it, sequentially, with the same args (1..50, default 5)
        args   the args object passed unchanged (a fresh deep copy) to every run (<= 4000 chars JSON)

Every output is compared with the FIRST run's. The report gives how many runs differed, the
number of distinct outputs, per-output SHA-256 digest + length, the first differing line number of
each differing run, error/timeout counts, run timings, and the Great Scale evidence snapshot
avrone.greatscale.evidence_snapshot(failures=differing_runs, runs=N): S = p(1 - p) is the
outcome variance of "this tool gives the same answer twice".

Output CONTENT is never echoed (the probed tools redact on their own, but echoing N copies would
only multiply exposure): only digests, lengths and line numbers. Recursion is refused, the target
must already be registered and on the allow-list, args are schema-checked before the first run,
each run has a timeout and the whole probe a time budget. Nothing here writes, spawns or networks
beyond what the allow-listed read-only tool itself does.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
import uuid
from typing import TYPE_CHECKING, Any

from .greatscale import evidence_snapshot
from .integration_contract import EvidenceContractError, build_evidence_event
from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

FLAKE_PROBE_NAME = "flake_probe"
FLAKE_PROBE_ALLOWED = ("inspect_tool", "env_report", "capabilities_report", "self_scan",
                       "codex_stats", "memory_peek", "great_scale")
FLAKE_PROBE_MAX_RUNS = 50
FLAKE_PROBE_MAX_ARGS_CHARS = 4000
FLAKE_PROBE_RUN_TIMEOUT_S = 20.0
FLAKE_PROBE_BUDGET_S = 120.0
FLAKE_PROBE_MAX_LISTED = 20
FLAKE_PROBE_MAX_CHARS = 8000


def _digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="surrogatepass")).hexdigest()


def _first_diff_line(a: str, b: str) -> int:
    la, lb = a.splitlines(), b.splitlines()
    for i, (x, y) in enumerate(zip(la, lb), 1):
        if x != y:
            return i
    return min(len(la), len(lb)) + 1


async def flake_probe(ctx: "AgentContext", reg: Any, name: str, runs: int, args: dict[str, Any],
                      run_timeout: float = FLAKE_PROBE_RUN_TIMEOUT_S,
                      budget: float = FLAKE_PROBE_BUDGET_S) -> dict[str, Any]:
    if isinstance(runs, bool) or not isinstance(runs, int) or not 1 <= runs <= FLAKE_PROBE_MAX_RUNS:
        raise ValueError(f"runs must be an integer in [1, {FLAKE_PROBE_MAX_RUNS}]")
    if (isinstance(run_timeout, bool) or not isinstance(run_timeout, (int, float))
            or not math.isfinite(run_timeout) or not 0 < run_timeout <= FLAKE_PROBE_RUN_TIMEOUT_S):
        raise ValueError(f"run_timeout must be finite and in (0, {FLAKE_PROBE_RUN_TIMEOUT_S}]")
    if (isinstance(budget, bool) or not isinstance(budget, (int, float))
            or not math.isfinite(budget) or not 0 <= budget <= FLAKE_PROBE_BUDGET_S):
        raise ValueError(f"budget must be finite and in [0, {FLAKE_PROBE_BUDGET_S}]")
    outs: list[str] = []
    timed_out: list[bool] = []
    times: list[float] = []
    t0 = time.monotonic()
    budget_hit = False
    for i in range(runs):
        if i and time.monotonic() - t0 >= budget:
            budget_hit = True
            break
        s = time.monotonic()
        try:
            out = await asyncio.wait_for(reg.call(ctx, name, copy.deepcopy(args)), run_timeout)
        except asyncio.TimeoutError:
            out = ""
            timed_out.append(True)
        else:
            timed_out.append(False)
        times.append(time.monotonic() - s)
        outs.append(out if isinstance(out, str) else str(out))
    n = len(outs)
    # The first execution is the reference, not a comparison trial. Counting it
    # in the Great Scale denominator would manufacture one guaranteed success.
    first = outs[0]
    first_timed_out = timed_out[0]
    differing = [i + 1 for i, (o, did_timeout) in enumerate(zip(outs, timed_out))
                 if did_timeout != first_timed_out or o != first]
    comparison_runs = max(0, n - 1)
    assessment = "baseline_relative" if comparison_runs else "insufficient_samples"
    great_scale = (evidence_snapshot(failures=len(differing), runs=comparison_runs)
                   if comparison_runs else None)
    evidence_event = None
    if comparison_runs:
        # Bind the measurement to the current daemon run when valid. Fall back
        # to a fresh opaque identifier if a caller supplies invalid provenance.
        raw_run_id = getattr(ctx, "run_id", None)
        check_id = f"flake_probe:{name}:{uuid.uuid4().hex}"
        event_args = dict(
            source="avrone-daemon",
            run_id=raw_run_id,
            check_id=check_id,
            failures=len(differing),
            runs=comparison_runs,
            refs=[f"tool:{name}", "measurement:first-run-baseline"],
        )
        try:
            evidence_event = build_evidence_event(**event_args)
        except EvidenceContractError:
            event_args["run_id"] = uuid.uuid4().hex
            evidence_event = build_evidence_event(**event_args)
    distinct: dict[tuple[str, str], dict[str, Any]] = {}
    for i, o in enumerate(outs, 1):
        did_timeout = timed_out[i - 1]
        d = "timeout" if did_timeout else _digest(o)
        # Group by the exact output, not a shortened digest. A digest collision
        # must never collapse two distinct tool results in the report.
        key = ("timeout", "") if did_timeout else ("output", o)
        e = distinct.setdefault(key, {"digest": d, "chars": 0 if did_timeout else len(o), "runs": 0, "first_run": i})
        e["runs"] += 1
    diffs = [{"run": r, "first_diff_line": None
              if first_timed_out or timed_out[r - 1]
              else _first_diff_line(first, outs[r - 1])} for r in differing[:FLAKE_PROBE_MAX_LISTED]]
    return {
        "tool": name, "runs_requested": runs, "runs": n, "budget_hit": budget_hit,
        "comparison_runs": comparison_runs, "assessment": assessment,
        "comparison_model": "fixed_first_run_baseline",
        "differing_runs": len(differing), "distinct_outputs": len(distinct),
        "outputs": list(distinct.values())[:FLAKE_PROBE_MAX_LISTED],
        "outputs_hidden": max(0, len(distinct) - FLAKE_PROBE_MAX_LISTED),
        "diffs": diffs, "diffs_hidden": max(0, len(differing) - FLAKE_PROBE_MAX_LISTED),
        "errors": sum(1 for o in outs if o.startswith("ERROR:")),
        "timeouts": sum(timed_out),
        "ms": {"min": round(min(times) * 1000, 1), "max": round(max(times) * 1000, 1),
               "total": round(sum(times) * 1000, 1)},
        "great_scale": great_scale,
        "evidence_event": evidence_event,
    }


def _cap(text: str) -> str:
    return text if len(text) <= FLAKE_PROBE_MAX_CHARS else text[:FLAKE_PROBE_MAX_CHARS] + "\n...[truncated]"


async def t_flake_probe(ctx: "AgentContext", args: dict[str, Any]) -> str:
    name = str(args.get("tool") or "").strip()
    if not name:
        return "ERROR: give tool (one of: " + ", ".join(FLAKE_PROBE_ALLOWED) + ")"
    if name == FLAKE_PROBE_NAME:
        return "ERROR: flake_probe cannot probe itself (recursion refused)"
    if name not in FLAKE_PROBE_ALLOWED:
        return (f"ERROR: {name[:64]!r} is not on the flake_probe allow-list (read-only tools only: "
                + ", ".join(FLAKE_PROBE_ALLOWED) + ")")
    runs = args.get("runs", 5)
    if runs is None or runs == "":
        runs = 5
    if isinstance(runs, bool) or not isinstance(runs, int):
        return "ERROR: runs must be an integer 1..50"
    if not 1 <= runs <= FLAKE_PROBE_MAX_RUNS:
        return f"ERROR: runs must be 1..{FLAKE_PROBE_MAX_RUNS}"
    targs = args.get("args")
    if targs is None:
        targs = {}
    if not isinstance(targs, dict):
        return "ERROR: args must be an object (the args passed to every run)"
    try:
        encoded = json.dumps(targs, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return "ERROR: args must be plain JSON"
    if len(encoded) > FLAKE_PROBE_MAX_ARGS_CHARS:
        return f"ERROR: args are longer than {FLAKE_PROBE_MAX_ARGS_CHARS} chars of JSON"
    targs = json.loads(encoded)  # plain data only: no shared objects between runs
    reg = getattr(ctx.core, "tools", None)
    if reg is None or not callable(getattr(reg, "get", None)) or not callable(getattr(reg, "call", None)):
        return "ERROR: the tool registry is not available"
    tool = reg.get(name)
    if tool is None:
        return f"ERROR: {name!r} is not registered on this node"
    if getattr(tool, "fn", None) is t_flake_probe:
        return "ERROR: flake_probe cannot probe itself (recursion refused)"
    if callable(getattr(reg, "check", None)):
        bad = reg.check(name, targs)
        if bad:
            return f"ERROR: args rejected by {name}'s schema: {bad[1]}"
    rep = await flake_probe(ctx, reg, name, runs, targs)
    if str(args.get("format", "text")).lower() == "json":
        return _cap(json.dumps(rep, indent=1, sort_keys=True))
    g = rep["great_scale"]
    if rep["assessment"] == "insufficient_samples":
        verdict = "INSUFFICIENT EVIDENCE (one execution; no baseline comparison)"
    elif rep["differing_runs"] == 0:
        verdict = f"BASELINE-CONSISTENT (0/{rep['comparison_runs']} comparisons differed)"
    else:
        verdict = (f"BASELINE DEVIATIONS ({rep['differing_runs']}/{rep['comparison_runs']} "
                   "comparisons differed from run 1)")
    lines = [f"flake_probe {name} x{rep['runs']}"
             + (f" (of {rep['runs_requested']}; time budget hit)" if rep["budget_hit"] else "")
             + ": " + verdict + "  (finite sample; not proof of determinism; output content hidden)",
             f"distinct outputs: {rep['distinct_outputs']}  errors: {rep['errors']}  timeouts: {rep['timeouts']}  "
             f"ms min/max/total: {rep['ms']['min']}/{rep['ms']['max']}/{rep['ms']['total']}"]
    for o in rep["outputs"]:
        lines.append(f"  {o['digest']} ({o['chars']} chars) x{o['runs']} first at run {o['first_run']}")
    if rep["outputs_hidden"]:
        lines.append(f"  ...[{rep['outputs_hidden']} more distinct outputs]")
    if rep["diffs"]:
        lines.append("differing runs: " + ", ".join(
            f"#{d['run']}" + (f"@line {d['first_diff_line']}" if d["first_diff_line"] else " (timeout)")
            for d in rep["diffs"]) + (f" ...[+{rep['diffs_hidden']}]" if rep["diffs_hidden"] else ""))
    if g is None:
        lines.append("great scale: not computed; at least two executions are required for one comparison")
    else:
        n95 = g["runsFor95"]
        lines.append(f"great scale (baseline-relative): p={g['p']:.4g}  S=p(1-p)={g['S']:.4g}  "
                     f"Psi={g['psi']:.6f}  cone floor: {'yes' if g['onConeFloor'] else 'no'}  "
                     f"orientation: {g['orientation']}  comparisons for 95% detection: "
                     f"{'n/a' if n95 is None else n95} (IID assumption)")
    lines.append("evidence event: " + ("available (content digest; not a signature)"
                                      if rep["evidence_event"] else "not emitted (no comparisons)"))
    return _cap("\n".join(lines))


FLAKE_PROBE_TOOLS: list[Tool] = [
    Tool(FLAKE_PROBE_NAME,
         "read-only: run ONE allow-listed read-only tool (" + ", ".join(FLAKE_PROBE_ALLOWED) + ") N times "
         "(1..50) with the same args and report how many runs differed from the first, distinct output "
         "digests and the Great Scale evidence snapshot (S = p(1-p)). Measures nondeterminism of your own "
         "perception tools; output content is never echoed; cannot probe itself.",
         '{"tool": str, "runs"?: int, "args"?: object, "format"?: "text"|"json"}',
         t_flake_probe, source="builtin",
         schema={"required": {"tool": "str"}, "optional": {"runs": "int", "args": "dict", "format": "str"}}),
]


def register_flakeprobe_tools(registry) -> None:
    for t in FLAKE_PROBE_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
