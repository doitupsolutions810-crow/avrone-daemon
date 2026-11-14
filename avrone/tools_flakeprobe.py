"""flake_probe: measure the nondeterminism of one of the agent's own READ-ONLY perception tools.

    flake_probe {"tool": str, "runs"?: int, "args"?: object, "format"?: "text"|"json"}
        tool   one of FLAKE_PROBE_ALLOWED (read-only tools only; never flake_probe itself)
        runs   how many times to call it, sequentially, with the same args (1..50, default 5)
        args   the args object passed unchanged (a fresh deep copy) to every run (<= 4000 chars JSON)

Every output is compared with the FIRST run's. The report gives how many runs differed, the
number of distinct outputs, per-output sha256 prefix + length, the first differing line number of
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
import time
from typing import TYPE_CHECKING, Any

from .greatscale import evidence_snapshot
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
_TIMEOUT_MARK = "\x00flake_probe:timeout\x00"


class _Timeout(str):
    """A run that timed out. Round 47: a plain string mark meant a tool that returned those exact
    characters counted as a timeout; identity (``is``) can not be forged by output."""


_TIMEOUT = _Timeout(_TIMEOUT_MARK)


def _is_timeout(o: Any) -> bool:
    return o is _TIMEOUT


def _is_error(o: str) -> bool:
    # round 47: the registry's own refusals start "ARG ERROR" (wrong arg types) and were not counted
    return o.startswith("ERROR") or o.startswith("ARG ERROR")


def _digest(s: str) -> str:
    # round 62: errors="replace" turned every lone surrogate into "?", so "a\ud800" and "a\ud801"
    # had one digest: the report said 1 distinct output while counting 1 differing run
    return hashlib.sha256(s.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


def _lines(s: str, keepends: bool = False) -> list[str]:
    """Lines split on \\n only (round 62): splitlines() also broke at U+2028, \\x0c, \\x1c ... so a
    tool output holding one named a line number that does not exist in the \\n-split text."""
    parts = s.split("\n")
    if keepends:
        out = [x + "\n" for x in parts[:-1]]
        return out + ([parts[-1]] if parts[-1] else [])
    return parts[:-1] + ([parts[-1]] if parts[-1] else [])


def _first_diff_line(a: str, b: str) -> int:
    """1-based line where a and b first differ, line endings included. Round 47: endings were
    dropped, so "x\r\n" vs "x\n" and "x" vs "x\n" named a line one past the end of both."""
    la, lb = _lines(a), _lines(b)
    for i, (x, y) in enumerate(zip(la, lb), 1):
        if x != y:
            return i
    if len(la) != len(lb):
        return min(len(la), len(lb)) + 1  # the first extra line, which exists in the longer one
    ka, kb = _lines(a, True), _lines(b, True)
    for i, (x, y) in enumerate(zip(ka, kb), 1):
        if x != y:  # same text, different line ending
            return i
    return max(1, len(la))  # cannot happen for a != b; never a line past the end


async def flake_probe(ctx: "AgentContext", reg: Any, name: str, runs: int, args: dict[str, Any],
                      run_timeout: float = FLAKE_PROBE_RUN_TIMEOUT_S,
                      budget: float = FLAKE_PROBE_BUDGET_S) -> dict[str, Any]:
    outs: list[str] = []
    times: list[float] = []
    t0 = time.monotonic()
    budget_hit = False
    for i in range(runs):
        if i and time.monotonic() - t0 >= budget:
            budget_hit = True
            break
        s = time.monotonic()
        # round 47: a run started near the end of the budget could overrun it by a full
        # run_timeout; later runs get only what is left of the budget
        left = run_timeout if not i else max(0.001, min(run_timeout, budget - (s - t0)))
        try:
            out = await asyncio.wait_for(reg.call(ctx, name, copy.deepcopy(args)), left)
        except asyncio.TimeoutError:
            out = _TIMEOUT
        except Exception as e:  # noqa: BLE001 - round 47: one raising run sank the whole probe
            out = f"ERROR: {type(e).__name__} raised"
        times.append(time.monotonic() - s)
        if out is not _TIMEOUT:
            if isinstance(out, (bytes, bytearray)):
                out = bytes(out).decode("utf-8", "replace")
            elif not isinstance(out, str):
                try:
                    out = str(out)
                except Exception as e:  # noqa: BLE001 - a hostile __str__
                    out = f"ERROR: unprintable {type(out).__name__} output ({type(e).__name__})"
            else:
                out = str.__str__(out)  # a str subclass never passes for the timeout sentinel
        outs.append(out)
    n = len(outs)
    first = outs[0]
    differing = [i + 1 for i, o in enumerate(outs) if o != first or _is_timeout(o) != _is_timeout(first)]
    distinct: dict[str, dict[str, Any]] = {}
    for i, o in enumerate(outs, 1):
        d = "timeout" if _is_timeout(o) else _digest(o)
        e = distinct.setdefault(d, {"digest": d, "chars": 0 if _is_timeout(o) else len(o), "runs": 0, "first_run": i})
        e["runs"] += 1
    diffs = [{"run": r, "first_diff_line": None if _is_timeout(first) or _is_timeout(outs[r - 1])
              else _first_diff_line(first, outs[r - 1])} for r in differing[:FLAKE_PROBE_MAX_LISTED]]
    return {
        "tool": name, "runs_requested": runs, "runs": n, "budget_hit": budget_hit,
        "differing_runs": len(differing), "distinct_outputs": len(distinct),
        "outputs": list(distinct.values())[:FLAKE_PROBE_MAX_LISTED],
        "outputs_hidden": max(0, len(distinct) - FLAKE_PROBE_MAX_LISTED),
        "diffs": diffs, "diffs_hidden": max(0, len(differing) - FLAKE_PROBE_MAX_LISTED),
        "errors": sum(1 for o in outs if not _is_timeout(o) and _is_error(o)),
        "timeouts": sum(1 for o in outs if _is_timeout(o)),
        "ms": {"min": round(min(times) * 1000, 1), "max": round(max(times) * 1000, 1),
               "total": round(sum(times) * 1000, 1)},
        "great_scale": evidence_snapshot(failures=len(differing), runs=n),
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
    # round 47: int(1e999) raised OverflowError, 2.7 ran twice, "1_0" ran ten times
    from .argnum import tool_int
    if isinstance(runs, float) and not (runs == runs and runs not in (float("inf"), float("-inf"))
                                        and runs.is_integer()):
        return "ERROR: runs must be an integer 1..50"
    runs = tool_int(runs, 5, bad=None)
    if runs is None:
        return "ERROR: runs must be an integer 1..50"
    if not 1 <= runs <= FLAKE_PROBE_MAX_RUNS:
        return f"ERROR: runs must be 1..{FLAKE_PROBE_MAX_RUNS}"
    targs = args.get("args")
    if targs is None:
        targs = {}
    if not isinstance(targs, dict):
        return "ERROR: args must be an object (the args passed to every run)"
    try:
        # round 47: allow_nan=False (NaN/Infinity args are not JSON); non-str keys were
        # silently turned into strings by dumps, so {1: "a"} and {"1": "a"} probed the same
        encoded = json.dumps(targs, sort_keys=True, allow_nan=False)
        if json.loads(encoded) != targs:  # a non-str key at any depth, a tuple, a str subclass key...
            raise TypeError("args do not survive a JSON round trip")
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
    if str(args.get("format", "text")).strip().lower() == "json":
        return _cap(json.dumps(rep, indent=1, sort_keys=True))
    g = rep["great_scale"]
    verdict = ("deterministic" if rep["differing_runs"] == 0 else
               f"NONDETERMINISTIC ({rep['differing_runs']}/{rep['runs']} runs differed from run 1)")
    lines = [f"flake_probe {name} x{rep['runs']}"
             + (f" (of {rep['runs_requested']}; time budget hit)" if rep["budget_hit"] else "")
             + ": " + verdict + "  (outputs compared by digest; content never shown)",
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
    n95 = g["runsFor95"]
    lines.append(f"great scale: p={g['p']:.4g}  S=p(1-p)={g['S']:.4g}  Psi={g['psi']:.6f}  "
                 f"cone floor: {'yes' if g['onConeFloor'] else 'no'}  orientation: {g['orientation']}  "
                 f"runs for 95% detection: {'n/a' if n95 is None else n95}")
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
