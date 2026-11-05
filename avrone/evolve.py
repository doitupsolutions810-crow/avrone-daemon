"""Self-evolution (v0.9): AVRONE proposes and validates changes to its OWN codebase.

Guardrails, by construction:
  * All work happens in a throwaway git worktree under data_dir, never the live tree.
  * A proposal must apply cleanly AND pass the full test suite to be accepted; a
    failing proposal is discarded and its test output returned so the agent can retry.
  * Nothing is pushed and nothing touches `main`. An accepted proposal lands on a
    branch in the worktree and is surfaced as a diff for a human to review/merge.
  * Edits are confined to the repo; paths escaping it are rejected.

Tools: evolve_propose, evolve_loop (bounded multi-iteration), evolve_start / evolve_cancel (the same
       loop as a background task), evolve_status, evolve_diff, evolve_list, evolve_accept (branch only),
       evolve_reset, evolve_skill_propose (a NEW skills/<name>/SKILL.md through the same gate),
       evolve_history (finished-loop summaries).
Env: AVRONE_REPO_DIR (repo root), AVRONE_SELF_EVOLVE=allow|deny (default allow; also checked between
     iterations, so flipping it to deny stops a running loop), AVRONE_EVOLVE_TEST_CMD (default: python -m pytest -q),
     AVRONE_EVOLVE_MAX_ITERS / AVRONE_EVOLVE_TIME_BUDGET (loop default + node ceiling; hard caps 10 / 1800s),
     AVRONE_EVOLVE_MAX_BG (concurrent background loops per process; default 2, hard cap 4),
     AVRONE_EVOLVE_SKILL_MAX_BYTES (proposed skill size ceiling; default/hard cap 16384),
     AVRONE_EVOLVE_HISTORY (finished-loop summaries kept in loops/history.jsonl; default 50, can only
     lower it; hard cap 200). evolve_history() reads it back, across restarts.
     AVRONE_EVOLVE_LONG=allow (default off): explicit operator opt-in that raises the loop hard caps
     from 10 / 1800s to LONG_MAX_ITERS_CAP / LONG_TIME_BUDGET_CAP_S (50 / 7200s). It changes no
     default; AVRONE_EVOLVE_MAX_ITERS / AVRONE_EVOLVE_TIME_BUDGET (or a tool argument) still pick the
     actual size, and nothing can exceed the long ceilings.
Restarts: a "running" loop whose owning process is gone is never resumed automatically. The first
     status/list/history read marks it "interrupted" on disk (with a resume_hint) and records it in history.
Resume: every loop checkpoints its progress to its loop file after each iteration ("checkpoint": next
     iteration, elapsed seconds, test_cmd and, when they fit in CHECKPOINT_MAX_BYTES, the candidate edit
     lists). evolve_start {"resume_loop_id": id} continues an "interrupted" loop (only that status) in the
     background from its last completed iteration, under the SAME loop_id. Resumed iterations and elapsed
     time count toward the SAME caps: the iteration cap and time budget are the loop's own, re-clamped by
     today's limits (a resume can lower them, never raise them), and the background-loop cap applies.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shlex
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def _repo_root(ctx: "AgentContext") -> Path:
    env = os.getenv("AVRONE_REPO_DIR")
    if env:
        return Path(env).resolve()
    # package lives at <repo>/avrone/evolve.py
    return Path(__file__).resolve().parent.parent


def _data(ctx: "AgentContext") -> Path:
    d = Path(ctx.core.settings.data_dir) / "evolve"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    try:
        ctx.core.codex.emit(ctx.run_id, kind, ctx.name, **data)
    except Exception:  # noqa: BLE001
        pass


async def _sh(ctx: "AgentContext", cmd: str, cwd: Path, timeout: int = 600):
    # quoted: an external drive like "/Volumes/My Passport" used to split into `cd /Volumes/My`
    return await ctx.core.runner.run(f"cd {shlex.quote(str(cwd))} && {cmd}", timeout=timeout)


def _meta_path(ctx: "AgentContext", pid: str) -> Path:
    return _data(ctx) / f"{pid}.json"


def _valid_pid(pid: str) -> bool:
    """Proposal ids are agent-supplied: only a plain SAFE_ID name, never a path."""
    return bool(pid) and len(pid) <= 80 and not SAFE_ID.search(pid) and not pid.startswith(".")


def _proposal(ctx: "AgentContext", pid: str) -> tuple[Path | None, dict[str, Any] | None, Path | None, str]:
    """(meta path, meta, its worktree, error). The worktree is returned only when it is a
    directory directly under data_dir/evolve (wt-*), so a corrupt or hostile meta file can never
    point evolve_reset's rmtree or evolve_diff's git at anything else."""
    if not _valid_pid(pid):
        return None, None, None, f"ERROR: invalid proposal id {pid[:80]!r}"
    mp = _meta_path(ctx, pid)
    if not mp.is_file():
        return None, None, None, f"ERROR: no proposal {pid}"
    try:
        meta = json.loads(mp.read_text())
    except (OSError, ValueError, RecursionError):
        meta = None
    if not isinstance(meta, dict):
        return mp, None, None, f"ERROR: proposal {pid} metadata is unreadable"
    raw = meta.get("worktree")
    wt = None
    if isinstance(raw, str) and raw:
        cand = Path(raw)
        root = _data(ctx).resolve()
        try:
            r = cand.resolve()
        except (OSError, RuntimeError):
            r = None
        if r is not None and r.parent == root and r.name.startswith("wt-") and not cand.is_symlink():
            wt = cand
    if wt is None:
        return mp, meta, None, f"ERROR: proposal {pid} has no worktree under the evolve dir"
    return mp, meta, wt, ""


async def _ensure_worktree(ctx: "AgentContext", pid: str) -> tuple[Path, str]:
    """A fresh checkout of the repo's HEAD for proposal `pid`, on its own branch."""
    repo = _repo_root(ctx)
    wt = _data(ctx) / f"wt-{pid}"
    branch = f"evolve/{pid}"
    if wt.exists():
        return wt, branch
    # Clone the local repo (cheap, isolated) rather than add a worktree to the live .git.
    r = await _sh(ctx, f"git clone -q --no-hardlinks . {shlex.quote(str(wt))} 2>&1", repo)
    if r.exit_code != 0:
        # Fallback: plain copy if not a git repo. A failed clone can leave a half-made dir behind,
        # which made copytree raise FileExistsError; runtime state and caches are never copied.
        shutil.rmtree(wt, ignore_errors=True)
        shutil.copytree(repo, wt, symlinks=True, ignore=shutil.ignore_patterns(*COPY_IGNORE))
        await _sh(ctx, "git init -q && git add -A && git -c user.email=a@a -c user.name=avrone commit -qm base", wt)
    await _sh(ctx, f"git checkout -q -b {shlex.quote(branch)}", wt)
    return wt, branch


COPY_IGNORE = (".git", "data", "agents", "*.egg-info", "__pycache__", ".pytest_cache", ".venv", "venv",
               "node_modules", ".smoke-tmp", ".mypy_cache", ".ruff_cache")


def commit_message(goal: Any) -> str:
    """One shell-safe argument for `git commit -m`. The old `json.dumps(goal)[:200]` put the goal in
    double quotes, where `$(...)` and backticks still run (an agent-written goal could execute
    commands), and cutting at 200 chars could drop the closing quote so the commit never happened."""
    text = " ".join(str(goal if goal is not None else "").split()) or "evolve proposal"
    text = text.replace("\x00", "")
    if len(text) > 200:
        text = text[:199] + "\u2026"
    return shlex.quote(text)


def _in_repo(wt: Path, rel: str) -> Path:
    """Resolve an agent-given repo-relative path inside the clone. The old string-prefix check let
    `../wt-<pid>0/x` (a sibling clone whose name starts with this one's) through; .git is refused
    because `.git/config` can set core.fsmonitor / core.hooksPath and make git run commands."""
    rel = str(rel)
    if not rel.strip() or "\x00" in rel:
        raise ValueError(f"bad path: {rel[:80]!r}")
    root = wt.resolve()
    p = (wt / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes repo: {rel}")
    if p == root:
        raise ValueError(f"path is the repo root, not a file: {rel!r}")
    if p.relative_to(root).parts[0] == ".git":
        raise ValueError(f"edits inside .git are not allowed: {rel}")
    return p


def _denied() -> bool:
    return os.getenv("AVRONE_SELF_EVOLVE", "allow").lower() == "deny"


DENIED_MSG = "ERROR: self-evolution is disabled on this node (AVRONE_SELF_EVOLVE=deny)"

# Monotonic clock, module-level so tests can drive the loop's time budget deterministically.
_now = time.monotonic


# Identifies THIS process run: a container restart can reuse the same pid (often 1), so a loop file's
# owner_pid alone cannot tell "my loop" from "a loop of the process that ran here before".
_PROC_TOKEN = __import__("uuid").uuid4().hex


def _new_pid(ctx: "AgentContext") -> str:
    import uuid
    return SAFE_ID.sub("-", f"{int(time.time())}-{ctx.name}-{uuid.uuid4().hex[:6]}")[:56]


MAX_EDITS = 200
MAX_EDITS_BYTES = 16 * 1024 * 1024


def _edits_too_big(edits: Any) -> str:
    if not isinstance(edits, list):
        return "edits must be a list"
    if len(edits) > MAX_EDITS:
        return f"{len(edits)} edits; cap is {MAX_EDITS} per proposal"
    total = 0
    for e in edits:
        c = e.get("content") if isinstance(e, dict) else None
        if isinstance(c, str):
            total += len(c)  # chars: a cheap upper-bound proxy, no encode of a huge string
        elif c is not None:
            try:
                total += len(json.dumps(c))
            except (TypeError, ValueError, RecursionError, OverflowError):
                return "an edit's content is not text or JSON"
        if total > MAX_EDITS_BYTES:
            return f"edits hold more than {MAX_EDITS_BYTES} characters of content; cap per proposal"
    return ""


def _edits_shape_error(edits: Any) -> str | None:
    """A malformed edit list, refused before the clone. edits=[1] or an edit without a path used to
    be found only after cloning the whole repo (~16 s, and an evolve/<pid> branch left behind)."""
    if not isinstance(edits, list):
        return "edits must be a list of {path, content} objects"
    for e in edits:
        if not isinstance(e, dict):
            return f"each edit must be an object with path and content, got {type(e).__name__}"
        if "path" not in e:
            return "edit is missing 'path'"
        if not isinstance(e["path"], str) or not e["path"].strip():
            return "bad path: each edit's path must be a non-empty string"
        if e.get("content") is None:
            return f"edit for {e['path'][:200]} has no content (whole-file text is required)"
    return None


async def _propose_once(ctx: "AgentContext", goal: str, edits: list, test_cmd: str | None = None,
                        timeout: int = 900, extra_meta: dict[str, Any] | None = None,
                        confine_to: str | None = None) -> dict[str, Any]:
    """One proposal -> test cycle in its own throwaway clone. Returns a result dict.

    Every guardrail lives here so single proposals and loop iterations share it: fresh clone
    under data_dir, edits confined to the clone, accept only on a clean full-suite pass.
    `confine_to` (a repo-relative dir) narrows the edits further, e.g. "skills" for skill proposals.
    """
    pid = _new_pid(ctx)
    t0 = _now()
    too_big = _edits_too_big(edits) or _edits_shape_error(edits)
    if too_big:  # checked before the clone: 100k edits or 1 GB of content used to be written to disk
        return {"pid": pid, "error": too_big, "passed": False, "branch": None, "files": [], "duration_s": 0.0}
    wt, branch = await _ensure_worktree(ctx, pid)
    changed = []
    base_r = await _sh(ctx, "git rev-parse HEAD 2>/dev/null", wt, timeout=60)
    base_sha = (base_r.stdout or "").strip() if getattr(base_r, "exit_code", 1) == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{7,64}", base_sha):
        base_sha = ""
    for e in edits:
        try:
            if not isinstance(e, dict):
                raise ValueError(f"each edit must be an object with path and content, got {type(e).__name__}")
            p = _in_repo(wt, str(e["path"]))
            if confine_to is not None:
                base = _in_repo(wt, confine_to)
                if p == base or base not in p.parents:
                    raise ValueError(f"path escapes {confine_to}/: {e['path']}")
            content = e.get("content")
            if content is None:  # `null` used to be written as the literal text "None"
                raise ValueError(f"edit for {e['path']} has no content (whole-file text is required)")
            if not isinstance(content, str):
                content = json.dumps(content, indent=2) if isinstance(content, (dict, list)) else str(content)
            if p.is_dir():
                raise ValueError(f"path is a directory: {e['path']}")
            p.parent.mkdir(parents=True, exist_ok=True)
            # bytes, not write_text: no locale encoding surprise, no newline translation, and lone
            # surrogates from a model's JSON become U+FFFD instead of a UnicodeEncodeError crash
            p.write_bytes(content.encode("utf-8", errors="replace"))
        except (KeyError, ValueError, TypeError, OSError) as ex:
            shutil.rmtree(wt, ignore_errors=True)
            msg = str(ex) if not isinstance(ex, KeyError) else f"edit is missing {ex}"
            return {"pid": pid, "error": msg, "passed": False, "branch": None, "files": changed,
                    "duration_s": round(_now() - t0, 3)}
        changed.append(str(e["path"]))
    # The gate runs the deterministic suite by default: the real-uvicorn integration tests flake
    # under the parallel-clone load of a squad and would reject good unit-level proposals.
    test_cmd = str(test_cmd or os.getenv("AVRONE_EVOLVE_TEST_CMD")
                   or 'python -m pytest -q -m "not integration"')
    timeout = max(1, int(timeout))
    # Test the clone's code via PYTHONPATH (beats the installed package) without re-pointing the
    # node's own install, so parallel proposals can't hijack each other or the live system.
    pp = shlex.quote(str(wt))
    res = await _sh(ctx, f"PYTHONPATH={pp}${{PYTHONPATH:+:$PYTHONPATH}} {test_cmd} 2>&1", wt, timeout=timeout)
    passed = res.exit_code == 0
    flaky = False
    if not passed and "pytest" in test_cmd:
        # Separate real failures from load flakes: re-run only what failed, once, alone.
        again = await _sh(ctx, f"PYTHONPATH={pp}${{PYTHONPATH:+:$PYTHONPATH}} {test_cmd} --lf 2>&1", wt,
                          timeout=min(600, timeout))
        if again.exit_code == 0:
            passed, flaky = True, True
            res = again
    tail = "\n".join((res.stdout or res.stderr or "").splitlines()[-25:])
    commit_error = ""
    if passed:
        c = await _sh(ctx, f"git add -A && git -c user.email=a@a -c user.name=avrone -c core.hooksPath=/dev/null "
                           f"commit -q --no-verify --allow-empty -m {commit_message(goal)} 2>&1", wt)
        if getattr(c, "exit_code", 1) != 0:  # tests passed but nothing landed on the branch: say so
            passed = False
            commit_error = ("commit failed: " + str(getattr(c, "stdout", "") or getattr(c, "stderr", ""))[-300:]).strip()
            tail = (tail + "\n" + commit_error).strip()
    duration = round(_now() - t0, 3)
    meta = {"pid": pid, "goal": goal, "branch": branch, "files": changed, "passed": passed,
            "by": ctx.name, "ts": time.time(), "test_cmd": test_cmd, "worktree": str(wt),
            "flaky_retry": flaky, "test_tail": tail[-3000:], "duration_s": duration, "base": base_sha,
            **({"commit_error": commit_error} if commit_error else {}), **(extra_meta or {})}
    _meta_path(ctx, pid).write_text(json.dumps(meta))
    _emit(ctx, "evolve.propose", pid=pid, passed=passed, files=changed, duration_s=duration)
    return {"pid": pid, "passed": passed, "branch": branch if passed else None, "files": changed,
            "flaky_retry": flaky, "test_tail": tail, "worktree": str(wt), "duration_s": duration}


async def t_evolve_propose(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if _denied():
        return DENIED_MSG
    goal = str(args.get("goal", "")).strip()
    edits = args.get("edits")
    if not goal or not isinstance(edits, list) or not edits:
        return ('ERROR: give {"goal": str, "edits": [{"path": str, "content": str}], "test_cmd"?: str}. '
                "Each edit replaces that file's whole contents with content.")
    r = await _propose_once(ctx, goal, edits, args.get("test_cmd"))
    if r.get("error"):
        return f"ERROR: {r['error']}"
    pid, passed, flaky, branch, tail = r["pid"], r["passed"], r["flaky_retry"], f"evolve/{r['pid']}", r["test_tail"]
    verdict = ("ACCEPTED (tests pass" + (" after re-running a flaky failure" if flaky else "")
               + ", committed to branch " + branch + ")") if passed else "REJECTED (tests fail; discard or retry)"
    return (f"proposal {pid}: {verdict}\ngoal: {goal}\nfiles: {', '.join(r['files'])}\n"
            f"--- test tail ---\n{tail}\n"
            f"{'Use evolve_diff ' + pid + ' to see the change.' if passed else 'Fix your edits and propose again.'}")


# ---- bounded multi-iteration loop -------------------------------------------------------------
# Hard ceilings: no argument or env setting can exceed these.
MAX_ITERS_CAP = 10
TIME_BUDGET_CAP_S = 1800
DEFAULT_MAX_ITERS = 3
DEFAULT_TIME_BUDGET_S = 600
# Opt-in long loops (AVRONE_EVOLVE_LONG=allow) may rise to these, and never past them.
LONG_MAX_ITERS_CAP = 50
LONG_TIME_BUDGET_CAP_S = 7200


def long_loops_allowed() -> bool:
    """Exactly "allow" (case-insensitive) opts in; anything else, including unset, keeps the normal caps."""
    return os.getenv("AVRONE_EVOLVE_LONG", "").strip().lower() == "allow"


def hard_caps() -> tuple[int, int]:
    """The (iterations, seconds) ceilings in force right now."""
    if long_loops_allowed():
        return LONG_MAX_ITERS_CAP, LONG_TIME_BUDGET_CAP_S
    return MAX_ITERS_CAP, TIME_BUDGET_CAP_S


def _env_num(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        x = float(raw)
    except ValueError:
        return None
    return x if x == x and x > 0 else None


def loop_limits(max_iters: Any = None, time_budget_s: Any = None) -> tuple[int, float]:
    """Effective (max_iters, time_budget_s) for a loop.

    AVRONE_EVOLVE_MAX_ITERS / AVRONE_EVOLVE_TIME_BUDGET set the node's default AND its ceiling
    (an agent can ask for less, never more); the hard caps bound everything, env included. The hard
    caps are 10 / 1800s unless AVRONE_EVOLVE_LONG=allow raises them to 50 / 7200s.
    """
    hard_iters, hard_budget = hard_caps()
    env_iters, env_budget = _env_num("AVRONE_EVOLVE_MAX_ITERS"), _env_num("AVRONE_EVOLVE_TIME_BUDGET")
    iter_cap = min(hard_iters, env_iters) if env_iters else hard_iters
    budget_cap = min(hard_budget, env_budget) if env_budget else hard_budget
    n = int(_clamp(max_iters, env_iters or DEFAULT_MAX_ITERS, iter_cap))
    budget = _clamp(time_budget_s, env_budget or DEFAULT_TIME_BUDGET_S, budget_cap)
    return max(1, n), budget


def _loops_dir(ctx: "AgentContext") -> Path:
    d = _data(ctx) / "loops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _attempt_row(a: dict[str, Any]) -> dict[str, Any]:
    outcome = "error" if a.get("error") else ("accepted" if a["passed"] else "rejected")
    return {"iteration": a["iteration"], "pid": a["pid"], "outcome": outcome, "passed": a["passed"],
            "duration_s": a.get("duration_s"), "branch": a.get("branch"), "error": a.get("error")}


def _clamp(v: Any, default: float, cap: float, lo: float = 1) -> float:
    try:
        x = float(v) if v is not None and not isinstance(v, bool) else float(default)
    except OverflowError:  # 10**400 raised out of loop_limits
        x = float(cap)
    except (TypeError, ValueError):
        x = float(default)
    if x != x:  # NaN
        x = float(default)
    return max(lo, min(cap, x))


def _loop_path(ctx: "AgentContext", loop_id: str) -> Path:
    return _loops_dir(ctx) / f"{SAFE_ID.sub('-', loop_id)}.json"


def _cancel_path(ctx: "AgentContext", loop_id: str) -> Path:
    # A file, not just an in-memory flag: a cancel survives a restart and works across processes.
    return _loops_dir(ctx) / f"{SAFE_ID.sub('-', loop_id)}.cancel"


def _cancel_requested(ctx: "AgentContext", loop_id: str) -> bool:
    return _cancel_path(ctx, loop_id).exists()


# Candidate edit lists are checkpointed into the loop file so a resume needs nothing but the loop_id;
# larger sets are not stored (the resume must then pass candidates again).
CHECKPOINT_MAX_BYTES = 1_000_000


def _checkpoint_candidates(candidates: list | None) -> list | None:
    if candidates is None:
        return None
    try:
        raw = json.dumps(candidates)
    except (TypeError, ValueError):
        return None
    return json.loads(raw) if len(raw.encode("utf-8")) <= CHECKPOINT_MAX_BYTES else None


async def evolve_loop(ctx: "AgentContext", goal: str, candidates: list | None = None, edits_fn=None,
                      max_iters: int | None = None, time_budget_s: float | None = None,
                      test_cmd: str | None = None, loop_id: str | None = None,
                      mode: str = "foreground", resume: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run up to `max_iters` proposal->test iterations, each in its own throwaway clone.

    Edits for iteration i come from `candidates[i]` or `edits_fn(i, attempts_so_far)` (sync or
    async; returning None/[] ends the loop). Stops at the first accepted proposal, when the
    iteration cap is hit, when the time budget runs out, or when candidates are exhausted.
    Caps: max_iters <= MAX_ITERS_CAP, time_budget_s <= TIME_BUDGET_CAP_S. Each iteration's test
    timeout is bounded by the remaining budget. Between iterations it also stops on a cancel
    request (evolve_cancel -> status "cancelled") or if AVRONE_SELF_EVOLVE flips to deny.

    `resume` (set only by evolve_start's resume path) continues an interrupted loop:
    {"rows": its completed iteration rows, "elapsed": seconds already spent, "meta": keys to keep}.
    Iteration numbering continues at len(rows) and the time budget clock starts `elapsed` seconds in,
    so prior iterations and time count toward the same caps.
    """
    if _denied():
        return {"error": DENIED_MSG, "attempts": [], "accepted": None, "stopped": "denied"}
    goal = str(goal or "").strip()
    if not goal or (candidates is None and edits_fn is None):
        return {"error": "ERROR: give a goal and candidates (list of edit lists) or edits_fn",
                "attempts": [], "accepted": None, "stopped": "invalid"}
    n, budget = loop_limits(max_iters, time_budget_s)
    loop_id = loop_id or _new_pid(ctx)
    prior: list[dict[str, Any]] = [dict(r) for r in (resume or {}).get("rows") or [] if isinstance(r, dict)]
    first = len(prior)
    elapsed = float((resume or {}).get("elapsed") or 0.0)
    start = _now() - max(0.0, elapsed)
    attempts: list[dict[str, Any]] = []
    stopped, accepted = "max_iters", None
    loop_meta = {"loop_id": loop_id, "goal": goal, "by": ctx.name, "ts": time.time(), "max_iters": n,
                 "time_budget_s": budget, "status": "running", "stopped": None, "accepted": None,
                 "iterations": [], "duration_s": 0.0, "mode": mode, "owner_pid": os.getpid(),
                 "owner_token": _PROC_TOKEN, **((resume or {}).get("meta") or {})}
    stored = _checkpoint_candidates(candidates)
    loop_path = _loop_path(ctx, loop_id)

    def save() -> None:
        loop_meta["iterations"] = prior + [_attempt_row(a) for a in attempts]
        loop_meta["duration_s"] = round(_now() - start, 3)
        loop_meta["checkpoint"] = {"next_iteration": first + len(attempts), "elapsed_s": loop_meta["duration_s"],
                                   "test_cmd": test_cmd, "candidates": stored,
                                   "candidates_stored": stored is not None}
        loop_path.write_text(json.dumps(loop_meta))
    save()
    def halted() -> str | None:
        if _cancel_requested(ctx, loop_id):
            return "cancelled"
        if _denied():
            return "denied"
        return None
    for i in range(first, n):
        h = halted()
        if h:
            stopped = h
            break
        remaining = budget - (_now() - start)
        if remaining <= 0:
            stopped = "time_budget"
            break
        if candidates is not None:
            edits = candidates[i] if i < len(candidates) else None
        else:
            edits = edits_fn(i, [dict(a) for a in prior + attempts])
            if hasattr(edits, "__await__"):
                edits = await edits
        h = halted()  # generating the candidate may take a while: re-check before cloning/testing
        if h:
            stopped = h
            break
        remaining = budget - (_now() - start)
        if not edits or not isinstance(edits, list):
            stopped = "exhausted"
            break
        r = await _propose_once(ctx, goal, edits, test_cmd, timeout=int(max(1, min(900, remaining))),
                                extra_meta={"loop_id": loop_id, "iteration": i})
        r["iteration"] = i
        attempts.append(r)
        row = _attempt_row(r)
        _emit(ctx, "evolve.iteration", loop_id=loop_id, **row)
        save()
        if r["passed"]:
            stopped, accepted = "accepted", {"pid": r["pid"], "branch": r["branch"]}
            break
        if _now() - start >= budget:
            stopped = "time_budget"
            break
    loop_meta.update(status="cancelled" if stopped == "cancelled" else "done", stopped=stopped, accepted=accepted)
    save()
    record_history(ctx, loop_meta)
    _cancel_path(ctx, loop_id).unlink(missing_ok=True)
    _emit(ctx, "evolve.loop", loop_id=loop_id, iterations=len(attempts), stopped=stopped,
          accepted=accepted and accepted["pid"], duration_s=loop_meta["duration_s"])
    return {"loop_id": loop_id, "goal": goal, "max_iters": n, "time_budget_s": budget,
            "attempts": attempts, "accepted": accepted, "stopped": stopped,
            "duration_s": loop_meta["duration_s"], "status": loop_meta["status"],
            "resumed_from": first if resume is not None else None, "iterations_total": first + len(attempts)}


# ---- bounded finished-loop history -----------------------------------------------------------
# One summary line per finished loop, newest last, trimmed to history_cap() lines. It survives
# restarts (unlike _BG) and stays bounded however many loops a node runs. Best effort: a failed
# history write never breaks a loop.
HISTORY_CAP = 200
DEFAULT_HISTORY = 50
HISTORY_GOAL_CHARS = 200


def history_cap() -> int:
    """Entries kept: AVRONE_EVOLVE_HISTORY may only LOWER the default; HISTORY_CAP bounds everything."""
    env = _env_num("AVRONE_EVOLVE_HISTORY")
    return int(max(1, min(HISTORY_CAP, DEFAULT_HISTORY, env if env else DEFAULT_HISTORY)))


def _history_path(ctx: "AgentContext") -> Path:
    # .jsonl, so list_loops' "*.json" glob never mistakes it for a loop.
    return _loops_dir(ctx) / "history.jsonl"


def _settings_loops_dir(settings) -> Path:
    return Path(settings.data_dir) / "evolve" / "loops"


def _read_history(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue  # a torn or corrupt line is skipped, not fatal
        if isinstance(row, dict) and row.get("loop_id"):
            out.append(row)
    return out


def _its(m: dict[str, Any]) -> list[dict[str, Any]]:
    """A loop's iterations, objects only (a hand-edited or torn loop file may hold anything)."""
    its = m.get("iterations")
    return [i for i in its if isinstance(i, dict)] if isinstance(its, list) else []


def _num(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        f = float(v)
    except OverflowError:
        return default
    return f if math.isfinite(f) else default


def _history_row(m: dict[str, Any]) -> dict[str, Any]:
    its = _its(m)
    acc = m.get("accepted") if isinstance(m.get("accepted"), dict) else None
    return {"loop_id": str(m.get("loop_id")), "goal": str(m.get("goal") or "")[:HISTORY_GOAL_CHARS],
            "status": m.get("status"), "stopped": m.get("stopped"), "mode": m.get("mode"),
            "iterations": len(its), "accepted_count": sum(1 for i in its if i.get("outcome") == "accepted"),
            "accepted_pid": acc and acc.get("pid"), "duration_s": m.get("duration_s"),
            "max_iters": m.get("max_iters"), "time_budget_s": m.get("time_budget_s"), "ended": time.time()}


# record_history is a read-modify-write of one shared file. Two loops finishing at once (loops in
# different threads / event loops of one process) each read the old rows, and the later os.replace
# dropped the other's row; both also wrote the same ".jsonl.tmp", so one replace could fail outright.
_HISTORY_LOCK = threading.Lock()


def record_history(ctx: "AgentContext", loop_meta: dict[str, Any]) -> None:
    """Append one finished loop's summary (replacing any earlier row for the same loop_id) and trim."""
    tmp = None
    try:
        _record_history_at(_history_path(ctx), loop_meta)
    except Exception:  # noqa: BLE001 - history is best effort
        pass


def _record_history_at(path: Path, loop_meta: dict[str, Any]) -> None:
    tmp = None  # an error before the tmp file existed raised UnboundLocalError out of the handler
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = _history_row(loop_meta)
        with _HISTORY_LOCK:
            rows = [r for r in _read_history(path) if r.get("loop_id") != row["loop_id"]]
            rows = (rows + [row])[-history_cap():]
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            os.replace(tmp, path)
            tmp = None
    except Exception:  # noqa: BLE001 - history is best effort
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def evolve_history(ctx: "AgentContext", limit: int | None = None) -> list[dict[str, Any]]:
    """Finished-loop summaries, oldest first (at most `limit`, newest kept)."""
    reconcile_interrupted(ctx.core.settings)
    rows = _read_history(_history_path(ctx))[-history_cap():]
    if limit is not None:
        n = int(_clamp(limit, history_cap(), history_cap()))
        rows = rows[-n:]
    return rows


DEFAULT_HISTORY_TOOL_ROWS = 10


async def t_evolve_history(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """The evolve_history tool: newest finished-loop summaries, read-only, bounded 1..HISTORY_CAP."""
    if _denied():
        return DENIED_MSG
    raw = args.get("limit") if isinstance(args, dict) else None
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float, str))):
        return f"ERROR: limit must be an integer 1..{HISTORY_CAP}"
    try:
        limit = DEFAULT_HISTORY_TOOL_ROWS if raw is None or str(raw).strip() == "" else int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return f"ERROR: limit must be an integer 1..{HISTORY_CAP}"
    limit = max(1, min(HISTORY_CAP, limit))
    rows = evolve_history(ctx, limit)
    if not rows:
        return f"no finished evolve loops yet (history keeps the newest {history_cap()})"
    lines = [f"evolve history: {len(rows)} loop(s), oldest first (keeps newest {history_cap()})"]
    for h in rows:
        acc = f" accepted={h['accepted_pid']}" if h.get("accepted_pid") else ""
        lines.append(f"  {h['loop_id']} [{h.get('status')}/{h.get('stopped')}] {str(h.get('goal') or '')[:60]} "
                     f"iters={h.get('iterations')}/{h.get('max_iters')} "
                     f"{float(h.get('duration_s') or 0):.1f}s{acc}")
    return "\n".join(lines)


# ---- background / resumable loops ------------------------------------------------------------
MAX_BG_LOOPS_CAP = 4
DEFAULT_MAX_BG_LOOPS = 2
_BG: dict[str, "asyncio.Task"] = {}   # loop_id -> task (strong refs: asyncio only keeps weak ones)


def max_bg_loops() -> int:
    env = _env_num("AVRONE_EVOLVE_MAX_BG")
    return int(max(1, min(MAX_BG_LOOPS_CAP, env if env else DEFAULT_MAX_BG_LOOPS)))


def _bg_running() -> list[str]:
    return [lid for lid, t in _BG.items() if not t.done()]


async def evolve_start(ctx: "AgentContext", goal: str, candidates: list | None = None, edits_fn=None,
                       max_iters: int | None = None, time_budget_s: float | None = None,
                       test_cmd: str | None = None, resume_loop_id: str | None = None) -> dict[str, Any]:
    """Launch evolve_loop as a background asyncio task and return its loop_id immediately.

    Same guardrails and the SAME hard caps as evolve_loop (it IS evolve_loop). The loop json is
    written (status "running") before this returns, so evolve_status sees it at once.
    With `resume_loop_id`, continue that interrupted loop instead (see _resume_start).
    """
    if _denied():
        return {"error": DENIED_MSG, "loop_id": None}
    if resume_loop_id is not None:
        return _resume_start(ctx, str(resume_loop_id), candidates, edits_fn, max_iters, time_budget_s, test_cmd)
    goal = str(goal or "").strip()
    if not goal or (candidates is None and edits_fn is None):
        return {"error": "ERROR: give a goal and candidates (list of edit lists) or edits_fn", "loop_id": None}
    cap = max_bg_loops()
    if len(_bg_running()) >= cap:
        return {"error": f"ERROR: {cap} background evolve loop(s) already running (AVRONE_EVOLVE_MAX_BG); "
                         "evolve_cancel one or wait", "loop_id": None}
    n, budget = loop_limits(max_iters, time_budget_s)
    loop_id = _new_pid(ctx)
    _loop_path(ctx, loop_id).write_text(json.dumps(
        {"loop_id": loop_id, "goal": goal, "by": ctx.name, "ts": time.time(), "max_iters": n,
         "time_budget_s": budget, "status": "running", "stopped": None, "accepted": None, "iterations": [],
         "duration_s": 0.0, "mode": "background", "owner_pid": os.getpid(),
         "owner_token": _PROC_TOKEN}))

    _spawn_bg(ctx, goal, candidates, edits_fn, n, budget, test_cmd, loop_id)
    _emit(ctx, "evolve.start", loop_id=loop_id, max_iters=n, time_budget_s=budget)
    return {"loop_id": loop_id, "status": "running", "max_iters": n, "time_budget_s": budget}


def _spawn_bg(ctx: "AgentContext", goal: str, candidates: list | None, edits_fn, n: int, budget: float,
              test_cmd: str | None, loop_id: str, resume: dict[str, Any] | None = None) -> None:
    async def body() -> dict[str, Any]:
        try:
            r = await evolve_loop(ctx, goal, candidates=candidates, edits_fn=edits_fn, max_iters=n,
                                  time_budget_s=budget, test_cmd=test_cmd, loop_id=loop_id, mode="background",
                                  resume=resume)
            if resume is not None and r.get("stopped") == "denied" and r.get("error"):
                # refused before its first iteration: it stays resumable, not stuck "running"
                _finish_abnormally(ctx, loop_id, "interrupted", "interrupted")
            return r
        except asyncio.CancelledError:
            _finish_abnormally(ctx, loop_id, "cancelled", "cancelled")
            raise
        except Exception as ex:  # noqa: BLE001 - a crashed loop must not stay "running" forever
            _finish_abnormally(ctx, loop_id, "error", f"error: {ex}"[:300])
            return {"loop_id": loop_id, "error": str(ex), "stopped": "error", "status": "error"}
        finally:
            _BG.pop(loop_id, None)

    _BG[loop_id] = asyncio.get_running_loop().create_task(body(), name=f"evolve-{loop_id}")


def _num_or(v: Any, default: float) -> float:
    if isinstance(v, bool):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x and x not in (float("inf"), float("-inf")) else default


def _resume_start(ctx: "AgentContext", loop_id: str, candidates: list | None, edits_fn,
                  max_iters: Any, time_budget_s: Any, test_cmd: str | None) -> dict[str, Any]:
    """Continue an "interrupted" loop in the background from its last completed iteration.

    Refused for unknown, running, finished (done/cancelled/error) loops, a loop that already has an
    accepted proposal, or one whose caps are already spent. Caps: the loop's own max_iters and
    time_budget_s, optionally lowered by the arguments, re-clamped by loop_limits (today's env and hard
    caps); completed iterations and elapsed time count toward them. The background-loop cap applies.
    """
    lp = evolve_status(ctx, loop_id) if loop_id else None
    if lp is None:
        return {"error": f"ERROR: no loop {loop_id[:80]}. evolve_list to see them.", "loop_id": None}
    status = lp.get("status")
    if status != "interrupted" or loop_id in _bg_running():
        return {"error": f"ERROR: loop {loop_id} is {status}; only an interrupted loop can be resumed",
                "loop_id": None}
    p = _loop_path(ctx, loop_id)
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError, RecursionError):
        raw = None
    if not isinstance(raw, dict):
        return {"error": f"ERROR: loop {loop_id} file is unreadable; cannot resume", "loop_id": None}
    rows = [r for r in (raw.get("iterations") or []) if isinstance(r, dict)]
    if raw.get("accepted") or any(r.get("outcome") == "accepted" or r.get("passed") is True for r in rows):
        return {"error": f"ERROR: loop {loop_id} already has an accepted proposal; nothing to resume",
                "loop_id": None}
    ck = raw.get("checkpoint") if isinstance(raw.get("checkpoint"), dict) else {}
    if candidates is None and edits_fn is None:
        candidates = ck.get("candidates") if isinstance(ck.get("candidates"), list) else None
    if candidates is not None and (not isinstance(candidates, list)
                                   or not all(isinstance(c, list) and c for c in candidates)):
        return {"error": "ERROR: candidates must be a list of non-empty edit lists", "loop_id": None}
    if candidates is None and edits_fn is None:
        return {"error": f"ERROR: loop {loop_id} has no checkpointed candidates; pass the loop's full candidates "
                         "list (iteration i uses candidates[i])", "loop_id": None}
    cap = max_bg_loops()
    if len(_bg_running()) >= cap:
        return {"error": f"ERROR: {cap} background evolve loop(s) already running (AVRONE_EVOLVE_MAX_BG); "
                         "evolve_cancel one or wait", "loop_id": None}
    n0 = int(max(1, _num_or(raw.get("max_iters"), DEFAULT_MAX_ITERS)))
    b0 = max(1.0, _num_or(raw.get("time_budget_s"), DEFAULT_TIME_BUDGET_S))
    want_n = n0 if max_iters is None else min(n0, _num_or(max_iters, n0))
    want_b = b0 if time_budget_s is None else min(b0, _num_or(time_budget_s, b0))
    n, budget = loop_limits(want_n, want_b)
    done = len(rows)
    elapsed = max(0.0, _num_or(raw.get("duration_s"), 0.0), _num_or(ck.get("elapsed_s"), 0.0))
    if done >= n:
        return {"error": f"ERROR: loop {loop_id} already used its iteration cap ({done}/{n}); start a new loop",
                "loop_id": None}
    if elapsed >= budget:
        return {"error": f"ERROR: loop {loop_id} already used its time budget ({elapsed:.0f}s/{budget:.0f}s); "
                         "start a new loop", "loop_id": None}
    if test_cmd is None and isinstance(ck.get("test_cmd"), str):
        test_cmd = ck["test_cmd"]
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        return {"error": f"ERROR: loop {loop_id} has no goal; cannot resume", "loop_id": None}
    resumed = int(_num_or(raw.get("resumed"), 0)) + 1
    keep = {"ts": raw.get("ts", time.time()), "by": raw.get("by", ctx.name), "resumed": resumed,
            "resumed_at": time.time(), "resumed_by": ctx.name}
    raw.update(status="running", stopped=None, max_iters=n, time_budget_s=budget, mode="background",
               owner_pid=os.getpid(), owner_token=_PROC_TOKEN, **keep)
    for k in ("resume_hint", "interrupted_at"):
        raw.pop(k, None)
    p.write_text(json.dumps(raw))  # "running" + our token before returning: a second resume is refused
    _cancel_path(ctx, loop_id).unlink(missing_ok=True)
    _spawn_bg(ctx, goal, candidates, edits_fn, n, budget, test_cmd, loop_id,
              resume={"rows": rows, "elapsed": elapsed, "meta": keep})
    _emit(ctx, "evolve.resume", loop_id=loop_id, from_iteration=done, max_iters=n, time_budget_s=budget,
          elapsed_s=round(elapsed, 3))
    return {"loop_id": loop_id, "status": "running", "max_iters": n, "time_budget_s": budget,
            "resumed_from": done, "elapsed_s": round(elapsed, 3), "resumed": resumed}


def _finish_abnormally(ctx: "AgentContext", loop_id: str, status: str, stopped: str) -> None:
    p = _loop_path(ctx, loop_id)
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError, RecursionError):
        m = {"loop_id": loop_id, "iterations": []}
    if not isinstance(m, dict):
        m = {"loop_id": loop_id, "iterations": []}
    m.update(status=status, stopped=stopped)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(m))
        os.replace(tmp, p)  # atomic: a crash here never leaves the loop file torn
    except OSError:
        tmp.unlink(missing_ok=True)
    _cancel_path(ctx, loop_id).unlink(missing_ok=True)
    record_history(ctx, m)


async def evolve_wait(loop_id: str, timeout: float | None = None) -> dict[str, Any] | None:
    """Await a background loop started in this process (None if it is not running here)."""
    t = _BG.get(loop_id)
    if t is None:
        return None
    return await asyncio.wait_for(asyncio.shield(t), timeout)


def _pid_alive(pid: Any) -> bool:
    try:
        if isinstance(pid, bool) or int(pid) <= 0:  # 0/-1 address process groups, not one process
            return False
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    return True


def _orphaned(lp: dict[str, Any]) -> bool:
    """A "running" loop whose owning process is gone: its pid is dead, or it is our pid but was written
    by an earlier process run (a different owner_token, e.g. a container restart reusing pid 1).
    Unknown owners (no owner_pid) and live foreign processes are left alone."""
    if lp.get("status") != "running" or lp.get("owner_pid") is None:
        return False
    if lp["owner_pid"] == os.getpid():
        tok = lp.get("owner_token")
        return tok is not None and tok != _PROC_TOKEN
    return not _pid_alive(lp["owner_pid"])


def _effective(lp: dict[str, Any]) -> dict[str, Any]:
    """A "running" loop whose owning process is gone was interrupted by a restart: say so."""
    if _orphaned(lp):
        lp = dict(lp, status="interrupted")
    return lp


def _resume_hint(lp: dict[str, Any]) -> str:
    done = len(lp.get("iterations") or [])
    return (f"interrupted by a restart after {done}/{lp.get('max_iters')} iteration(s); not resumed automatically. "
            f"To continue from iteration {done} under the same caps, evolve_start "
            f"{{\"resume_loop_id\": \"{lp.get('loop_id')}\"}}; or evolve_start again with the same goal and only "
            "the candidates not yet tried (see this loop's iterations); evolve_history lists it.")


def reconcile_interrupted(settings) -> list[str]:
    """Mark every orphaned "running" loop as "interrupted" ON DISK (never resumes it: a half-run loop's
    candidates and gate are not re-run behind the operator's back), add a resume_hint, drop any stale
    cancel file, and record it in history. Idempotent and best effort. Returns the loop_ids marked."""
    d = _settings_loops_dir(settings)
    marked: list[str] = []
    if not d.exists():
        return marked
    for f in sorted(d.glob("*.json")):
        try:
            lp = json.loads(f.read_text())
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(lp, dict) or not isinstance(lp.get("loop_id"), str) or not lp["loop_id"] \
                or not _orphaned(lp):
            continue
        lp.update(status="interrupted", stopped="interrupted", resume_hint=_resume_hint(lp),
                  interrupted_at=time.time())
        try:
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(lp))
            os.replace(tmp, f)
            (d / f"{f.stem}.cancel").unlink(missing_ok=True)
        except OSError:
            continue
        _record_history_at(d / "history.jsonl", lp)
        marked.append(str(lp["loop_id"]))
    return marked


def evolve_status(ctx: "AgentContext", loop_id: str | None = None) -> dict[str, Any] | None:
    """Structured progress of a loop (latest when loop_id is None); None if unknown."""
    loops = list_loops(ctx.core.settings)
    if loop_id:
        loops = [lp for lp in loops if lp.get("loop_id") == loop_id]
    if not loops:
        return None
    lp = loops[-1]
    its = lp.get("iterations", [])
    acc = sum(1 for i in its if i.get("outcome") == "accepted")
    return {**lp, "iterations_done": len(its), "accepted_count": acc, "rejected_count": len(its) - acc,
            "live": lp.get("loop_id") in _bg_running(),
            "cancel_requested": _cancel_requested(ctx, lp["loop_id"])}


def evolve_cancel(ctx: "AgentContext", loop_id: str) -> dict[str, Any]:
    """Ask a loop to stop. Cooperative: the in-flight iteration (its clone + gate) finishes cleanly,
    the next one never starts, and the loop ends with status "cancelled"."""
    st = evolve_status(ctx, loop_id) if loop_id else None
    if st is None:
        return {"error": f"ERROR: no loop {loop_id}. evolve_list to see them."}
    if st["status"] != "running":
        return {"loop_id": loop_id, "status": st["status"], "note": "not running; nothing to cancel"}
    _cancel_path(ctx, loop_id).write_text(str(time.time()))
    _emit(ctx, "evolve.cancel", loop_id=loop_id)
    return {"loop_id": loop_id, "status": "cancelling"}


async def t_evolve_start(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if _denied():
        return DENIED_MSG
    rid = args.get("resume_loop_id")
    if rid is not None:
        if not isinstance(rid, str) or not rid.strip() or len(rid) > 200:
            return "ERROR: resume_loop_id must be a loop id string (evolve_list shows them)"
        cands = args.get("candidates")
        r = await evolve_start(ctx, "", candidates=cands, max_iters=args.get("max_iters"),
                               time_budget_s=args.get("time_budget_s"), test_cmd=args.get("test_cmd"),
                               resume_loop_id=rid.strip())
        if r.get("error"):
            return r["error"]
        return (f"resumed loop {r['loop_id']} in the background from iteration {r['resumed_from']} "
                f"({r['elapsed_s']:.0f}s already spent; caps: {r['max_iters']} iters, {r['time_budget_s']:.0f}s "
                f"including earlier iterations). evolve_status {{\"loop_id\": \"{r['loop_id']}\"}} for progress.")
    cands = args.get("candidates")
    if not isinstance(cands, list) or not cands or not all(isinstance(c, list) and c for c in cands):
        return ('ERROR: give {"goal": str, "candidates": [[{"path": str, "content": str}], ...], '
                '"max_iters"?: int, "time_budget_s"?: number, "test_cmd"?: str}')
    r = await evolve_start(ctx, args.get("goal", ""), candidates=cands, max_iters=args.get("max_iters"),
                           time_budget_s=args.get("time_budget_s"), test_cmd=args.get("test_cmd"))
    if r.get("error"):
        return r["error"]
    return (f"started background loop {r['loop_id']} (caps: {r['max_iters']} iters, {r['time_budget_s']:.0f}s). "
            f"evolve_status {{\"loop_id\": \"{r['loop_id']}\"}} for progress; evolve_cancel to stop it.")


async def t_evolve_cancel(ctx: "AgentContext", args: dict[str, Any]) -> str:
    r = evolve_cancel(ctx, str(args.get("loop_id") or ""))
    if r.get("error"):
        return r["error"]
    if r["status"] == "cancelling":
        return f"cancel requested for loop {r['loop_id']}: it stops before its next iteration"
    return f"loop {r['loop_id']} is {r['status']}: nothing to cancel"


async def t_evolve_loop(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if _denied():
        return DENIED_MSG
    cands = args.get("candidates")
    if not isinstance(cands, list) or not cands or not all(isinstance(c, list) and c for c in cands):
        return ('ERROR: give {"goal": str, "candidates": [[{"path": str, "content": str}], ...], '
                '"max_iters"?: int, "time_budget_s"?: number, "test_cmd"?: str}')
    r = await evolve_loop(ctx, args.get("goal", ""), candidates=cands, max_iters=args.get("max_iters"),
                          time_budget_s=args.get("time_budget_s"), test_cmd=args.get("test_cmd"))
    if r.get("error"):
        return r["error"]
    lines = [f"loop {r['loop_id']}: stopped={r['stopped']} after {len(r['attempts'])} iteration(s) "
             f"in {r['duration_s']:.1f}s (caps: {r['max_iters']} iters, {r['time_budget_s']:.0f}s)"]
    for a in r["attempts"]:
        d = f" [{a.get('duration_s') or 0:.1f}s]"
        if a.get("error"):
            lines.append(f"  #{a['iteration']} {a['pid']}: ERROR {a['error']}{d}")
        elif a["passed"]:
            lines.append(f"  #{a['iteration']} {a['pid']}: ACCEPTED on branch {a['branch']}{d}")
        else:
            tail = "\n      ".join(a["test_tail"].splitlines()[-8:])
            lines.append(f"  #{a['iteration']} {a['pid']}: REJECTED{d}\n      {tail}")
    return "\n".join(lines)


# ---- bounded skill self-authoring ------------------------------------------------------------
SKILL_MAX_BYTES_CAP = 16384
SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SKILLS_DIR = "skills"


def skill_max_bytes() -> int:
    env = _env_num("AVRONE_EVOLVE_SKILL_MAX_BYTES")
    return int(min(SKILL_MAX_BYTES_CAP, env)) if env else SKILL_MAX_BYTES_CAP


def _skill_markdown(name: str, content: str | None, description: str | None, body: str | None) -> str:
    """Full SKILL.md text: `content` as given, or frontmatter built from description + body."""
    if content is not None:
        return str(content)
    desc = " ".join(str(description or "").split())
    return f"---\nname: {name}\ndescription: {desc}\n---\n{str(body or '').strip()}\n"


def check_skill(name: Any, md: str, repo: Path | None = None) -> str | None:
    """Why a skill proposal is refused, or None. Refused BEFORE any clone is made."""
    from .agentkit import parse_skill
    raw = str(name or "")
    if not SKILL_NAME_RE.fullmatch(raw) or "--" in raw:
        return (f"bad skill name {raw[:80]!r}: use 1-64 chars of lowercase a-z, 0-9 and single hyphens "
                "(no slashes, dots, or path parts)")
    size, cap = len(md.encode("utf-8")), skill_max_bytes()
    if size > cap:
        return f"skill too large: {size} bytes > {cap} byte cap"
    fm, steps = parse_skill(md)
    if fm.get("name") != raw:
        return f"frontmatter name must be {raw!r} (got {fm.get('name')!r})"
    if len(fm.get("description", "")) < 20:
        return "frontmatter description must be at least 20 characters"
    if not re.search(r"(?m)^\s*\d+[.)]\s+\S", steps):
        return "skill body needs numbered steps (1. ..., 2. ...)"
    if repo is not None and (repo / SKILLS_DIR / raw).exists():
        return f"skill {raw!r} already exists; proposals add NEW skills only (use evolve_propose to edit one)"
    return None


async def evolve_skill_propose(ctx: "AgentContext", name: str, content: str | None = None,
                               description: str | None = None, body: str | None = None,
                               test_cmd: str | None = None, goal: str | None = None) -> dict[str, Any]:
    """Propose a NEW skill at skills/<name>/SKILL.md through the same throwaway-clone + full-suite gate
    as evolve_propose (it IS _propose_once, confined to skills/). Lands only on a branch in the clone,
    and only if the suite (which validates every shipped skill) still passes. Never touches the live tree.
    """
    if _denied():
        return {"error": DENIED_MSG, "passed": False}
    md = _skill_markdown(str(name or ""), content, description, body)
    why = check_skill(name, md, _repo_root(ctx))
    if why:
        return {"error": f"ERROR: {why}", "passed": False}
    rel = f"{SKILLS_DIR}/{name}/SKILL.md"
    r = await _propose_once(ctx, goal or f"add skill {name}", [{"path": rel, "content": md}], test_cmd,
                            extra_meta={"kind": "skill", "skill": name}, confine_to=SKILLS_DIR)
    _emit(ctx, "evolve.skill", skill=name, pid=r.get("pid"), passed=r.get("passed"))
    return {**r, "skill": name, "path": rel, "bytes": len(md.encode("utf-8"))}


async def t_evolve_skill_propose(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if _denied():
        return DENIED_MSG
    name = args.get("name")
    if not name or (args.get("content") is None and not args.get("body")):
        return ('ERROR: give {"name": str, "content": str (whole SKILL.md)} or '
                '{"name": str, "description": str, "body": str (numbered steps)}, "test_cmd"?: str')
    r = await evolve_skill_propose(ctx, name, content=args.get("content"), description=args.get("description"),
                                   body=args.get("body"), test_cmd=args.get("test_cmd"), goal=args.get("goal"))
    if r.get("error"):
        return r["error"] if r["error"].startswith("ERROR") else f"ERROR: {r['error']}"
    verdict = (f"ACCEPTED (tests pass, committed to branch {r['branch']})" if r["passed"]
               else "REJECTED (suite fails with this skill; fix and propose again)")
    return (f"skill proposal {r['pid']}: {verdict}\nskill: {r['skill']} -> {r['path']} ({r['bytes']} bytes)\n"
            f"--- test tail ---\n{r['test_tail']}")


async def t_evolve_diff(ctx: "AgentContext", args: dict[str, Any]) -> str:
    pid = str(args.get("pid", ""))
    mp, meta, wt, err = _proposal(ctx, pid)
    if mp is None:
        return f"{err}. evolve_list to see them."
    if wt is None or not wt.is_dir():
        return err or f"ERROR: proposal {pid} worktree is gone"
    base = meta.get("base") if isinstance(meta, dict) else None
    if isinstance(base, str) and re.fullmatch(r"[0-9a-f]{7,64}", base):
        # against the recorded base: shows a rejected (uncommitted) proposal's edits, new files
        # included, instead of HEAD~1..HEAD, which then showed the repo's previous commit
        r = await _sh(ctx, f"git add -A -N >/dev/null 2>&1; git --no-pager diff {base}", wt)
    else:
        r = await _sh(ctx, "git --no-pager diff HEAD~1..HEAD 2>/dev/null || git --no-pager diff", wt)
    return r.text(8000) or "(no diff)"


async def t_evolve_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    rows = []
    for f in sorted(_data(ctx).glob("*.json")):
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(m, dict) or not all(k in m for k in ("pid", "passed", "goal", "files", "by")):
            continue  # a partial write or a foreign json file never breaks the listing
        m = dict(m, files=[str(x)[:200] for x in m["files"]] if isinstance(m["files"], list) else [])
        tag = f" loop={m['loop_id']}#{m.get('iteration')}" if m.get("loop_id") else ""
        dur = f" {_num(m['duration_s']):.1f}s" if isinstance(m.get("duration_s"), (int, float)) else ""
        rows.append(f"{m['pid']} [{'PASS' if m['passed'] else 'FAIL'}] {str(m['goal'])[:70]} "
                    f"({', '.join(m['files'])}) by {m['by']}{dur}{tag}")
    loops = list_loops(ctx.core.settings)
    if loops:
        rows.append("loops:")
        rows += ["  " + _loop_line(lp) for lp in loops]
    return "\n".join(rows) or "no proposals yet"


def _loop_line(lp: dict[str, Any]) -> str:
    its = _its(lp)
    acc = sum(1 for i in its if i.get("outcome") == "accepted")
    why = lp.get("stopped")
    return (f"{str(lp.get('loop_id'))[:80]} [{lp.get('status')}{'/' + str(why) if why and why != lp.get('status') else ''}] "
            f"{str(lp.get('goal') or '')[:60]} iters={len(its)}/{lp.get('max_iters')} accepted={acc} "
            f"rejected={len(its) - acc} {_num(lp.get('duration_s')):.1f}s/"
            f"{_num(lp.get('time_budget_s')):.0f}s")


def _clean_loop(lp: dict[str, Any]) -> dict[str, Any]:
    """A loop file as evolve_status / evolve_list read it. A hand-edited, torn or foreign file used
    to raise out of every listing: iterations as a dict or holding ints, outcome not a string,
    accepted not an object, a str ts or a huge owner_pid."""
    its = []
    for i in _its(lp):
        its.append({**i, "iteration": i.get("iteration"), "pid": str(i.get("pid"))[:80],
                    "outcome": str(i.get("outcome") or "unknown")[:20], "duration_s": _num(i.get("duration_s"))})
    acc = lp.get("accepted")
    acc = {**acc, "pid": str(acc.get("pid"))[:80], "branch": str(acc.get("branch"))[:200]} \
        if isinstance(acc, dict) else None
    return {**lp, "iterations": its, "accepted": acc, "goal": str(lp.get("goal") or "")}


def list_loops(settings) -> list[dict[str, Any]]:
    """Every evolve loop's meta (oldest first), for evolve_list / evolve_status."""
    reconcile_interrupted(settings)
    d = _settings_loops_dir(settings)
    out = []
    if d.exists():
        for f in d.glob("*.json"):
            try:
                lp = json.loads(f.read_text())
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(lp, dict) and isinstance(lp.get("loop_id"), str) and lp["loop_id"]:
                out.append(_clean_loop(_effective(lp)))
    return sorted(out, key=lambda m: _num(m.get("ts")))  # a str ts raised TypeError in the sort


async def t_evolve_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    loops = list_loops(ctx.core.settings)
    lid = str(args.get("loop_id") or "")
    if lid:
        loops = [lp for lp in loops if lp.get("loop_id") == lid]
        if not loops:
            return f"ERROR: no loop {lid}. evolve_list to see them."
    if not loops:
        n, b = loop_limits()
        return f"no evolve loops yet (limits: max_iters={n}, time_budget_s={b:.0f})"
    lp = loops[-1]
    lines = [_loop_line(lp)]
    if lp.get("mode") == "background" and lp.get("status") == "running":
        lines.append("background loop" + (" (cancel requested)" if _cancel_requested(ctx, lp["loop_id"]) else ""))
    if lp.get("accepted"):
        lines.append(f"accepted: {lp['accepted']['pid']} on branch {lp['accepted']['branch']}")
    if lp.get("status") == "interrupted" and lp.get("resume_hint"):
        lines.append(f"resume: {lp['resume_hint']}")
    for it in lp.get("iterations", []):
        lines.append(f"  #{it['iteration']} {it['pid']} {it['outcome'].upper()} "
                     f"{float(it.get('duration_s') or 0):.2f}s" + (f" ({it['error']})" if it.get("error") else ""))
    return "\n".join(lines)


async def t_evolve_reset(ctx: "AgentContext", args: dict[str, Any]) -> str:
    pid = str(args.get("pid", ""))
    mp, _, wt, err = _proposal(ctx, pid)
    if mp is None:
        return err
    if wt is not None:  # only ever our own data_dir/evolve/wt-* dir
        shutil.rmtree(wt, ignore_errors=True)
    mp.unlink(missing_ok=True)
    _emit(ctx, "evolve.reset", pid=pid)
    return f"discarded proposal {pid}"


EVOLVE_TOOLS = [
    ("evolve_propose", "propose a change to AVRONE's OWN code: it is applied in a throwaway copy of the repo and the "
     "test suite is run; it is kept only if every test passes. Nothing touches the live tree or main.",
     '{"goal": str, "edits": [{"path": str, "content": str}], "test_cmd"?: str}', t_evolve_propose),
    ("evolve_loop", "bounded self-evolution loop: try candidate edit sets one by one (each in its own throwaway "
     "copy, each needing a full test pass) and stop at the first that passes or when the iteration/time budget "
     "runs out (hard caps: 10 iterations, 1800s; 50 / 7200s only if the operator set AVRONE_EVOLVE_LONG=allow). Returns every attempt's verdict and failing test output.",
     '{"goal": str, "candidates": [[{"path": str, "content": str}], ...], "max_iters"?: int, '
     '"time_budget_s"?: number, "test_cmd"?: str}', t_evolve_loop),
    ("evolve_start", "evolve_loop in the BACKGROUND: returns a loop_id at once and keeps iterating (same guardrails "
     "and hard caps: 10 iterations, 1800s; at most 4 background loops). Poll with evolve_status, stop with "
     "evolve_cancel. resume_loop_id continues an INTERRUPTED loop from its last completed iteration under the "
     "same caps (earlier iterations and time count; candidates come from its checkpoint unless given).",
     '{"goal": str, "candidates": [[{"path": str, "content": str}], ...], "max_iters"?: int, '
     '"time_budget_s"?: number, "test_cmd"?: str} | {"resume_loop_id": str, "candidates"?: [...], '
     '"max_iters"?: int, "time_budget_s"?: number}', t_evolve_start),
    ("evolve_cancel", "stop an evolve loop cleanly: the running iteration finishes, the next never starts.",
     '{"loop_id": str}', t_evolve_cancel),
    ("evolve_skill_propose", "propose a NEW skill (skills/<name>/SKILL.md) for AVRONE itself: built in a throwaway "
     "copy and kept on a branch only if the full test suite (which checks every skill) passes. Name: lowercase "
     "a-z0-9-; max 16384 bytes; never overwrites an existing skill or writes outside skills/.",
     '{"name": str, "content"?: str, "description"?: str, "body"?: str, "test_cmd"?: str}', t_evolve_skill_propose),
    ("evolve_diff", "show the unified diff of an accepted proposal.", '{"pid": str}', t_evolve_diff),
    ("evolve_list", "list self-evolution proposals (pass/fail, duration, loop iteration) and loops.", "{}",
     t_evolve_list),
    ("evolve_status", "show an evolve loop (latest by default): each iteration's outcome and duration, "
     "accept/reject counts, and the budget used.", '{"loop_id"?: str}', t_evolve_status),
    ("evolve_history", "list finished evolve loops (done, cancelled, crashed), newest kept, across restarts: "
     "status, why it stopped, iterations, accepted proposal, duration.", '{"limit"?: int (1..200, default 10)}',
     t_evolve_history),
    ("evolve_reset", "discard a proposal and its worktree.", '{"pid": str}', t_evolve_reset),
]


def register_evolve_tools(registry) -> None:
    for name, desc, args_doc, fn in EVOLVE_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))


def list_accepted(settings) -> list[dict[str, Any]]:
    """Accepted proposals, for /api/evolve and the CLI."""
    d = Path(settings.data_dir) / "evolve"
    out = []
    if d.exists():
        for f in sorted(d.glob("*.json")):
            try:
                m = json.loads(f.read_text())
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(m, dict) and m.get("passed") is True and isinstance(m.get("pid"), str):
                row = {k: m.get(k) for k in ("pid", "goal", "branch", "files", "by", "ts")}
                # files as a str/int/[1, null] crashed the CLI's ", ".join and the /api/evolve view
                row["files"] = [str(x)[:200] for x in row["files"]] if isinstance(row["files"], list) else []
                row["goal"] = "" if row["goal"] is None else str(row["goal"])
                out.append(row)
    return out
