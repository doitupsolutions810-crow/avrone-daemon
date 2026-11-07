"""limits_status + run_logs.

    limits_status {}
        Every load cap this node enforces, its current value and where it comes from (env or
        default), plus live usage where this node can see it: concurrent model calls in use,
        agents active, agents started per run, request body caps, fleet peers vs the registry cap.
        Read-only.

    run_logs {"prune"?: bool, "older_than_days"?: int, "keep"?: int}
        Run-log disk usage (count, bytes, oldest/newest). Run logs are append-only and never
        rotated, so a busy node grows data/runs forever. prune=true deletes run logs older than
        older_than_days (default 30, min 1) while always keeping the newest `keep` (default 50,
        min 10) and any run still active on this node. Acting when pruning.

    log_health {"runs"?: int}
        Integrity of the newest run logs (default 50, max 200; at most 256 MB read): per log with a
        problem, corrupt lines, lines too long to load (> 4 MB, skipped by every reader), a torn last
        line (crash mid-write), and whether it holds more events than a run view shows
        (AVRONE_REPLAY_MAX_EVENTS). Read-only; never prints event content.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

DAY = 86400


def _limits() -> list[tuple[str, str, int, str]]:
    from . import codex, ctxspawn, fleet, mcp_server, server
    rows = []
    for env, fn in (("AVRONE_MAX_BACKGROUND_RUNS", server.max_background_runs),
                    ("AVRONE_MAX_MESSAGE_CHARS", server.max_message_chars),
                    ("AVRONE_MAX_BODY_BYTES", server.max_body_bytes),
                    ("AVRONE_FLEET_MAX_NODES", fleet.max_nodes),
                    ("AVRONE_MAX_WS_SUBSCRIBERS", codex.max_subscribers),
                    ("AVRONE_REPLAY_MAX_EVENTS", codex.replay_limit),
                    ("AVRONE_MCP_MAX_INFLIGHT", mcp_server.max_inflight),
                    ("AVRONE_MAX_AGENTS_PER_RUN", ctxspawn.max_agents_per_run),
                    ("AVRONE_SPAWN_CONTEXT_CHARS", ctxspawn.context_chars)):
        try:
            v = fn()
        except Exception:  # noqa: BLE001
            continue
        rows.append((env, "env" if os.environ.get(env, "").strip() else "default", v, ""))
    return rows


async def t_limits_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    core = getattr(ctx, "core", None)
    lines = ["limits_status:"]
    mp = getattr(getattr(core, "settings", None), "max_parallel", None)
    sem = getattr(core, "_sem", None)
    if isinstance(mp, int):
        free = getattr(sem, "_value", None)
        used = f", {mp - free} in use now" if isinstance(free, int) else ""
        lines.append(f"- AVRONE_MAX_PARALLEL (concurrent model calls) = {mp}{used}")
    for env, src, v, _ in _limits():
        lines.append(f"- {env} = {v} ({src})")
    from . import server
    bg = server.state.get("tasks")
    if isinstance(bg, set):
        lines.append(f"live: {len(bg)} background run(s) in flight of {server.max_background_runs()}")
    active = getattr(core, "active", None)
    if isinstance(active, dict):
        working = sum(1 for a in active.values() if isinstance(a, dict) and a.get("status") == "working")
        lines.append(f"live: {working} agent(s) working, {len(active)} known")
    ra = getattr(core, "run_agents", None)
    if isinstance(ra, dict) and ra:
        top = sorted(ra.items(), key=lambda kv: -kv[1])[:5]
        lines.append("agents started per run (top): " + ", ".join(f"{k}={v}" for k, v in top))
    try:
        pack_body = server.max_body_bytes("/api/fleet/agentpack")
        lines.append(f"body cap on /api/fleet/agentpack (fits a quota-sized pack) = {pack_body}")
    except Exception:  # noqa: BLE001
        pass
    cx = getattr(core, "codex", None)
    if isinstance(getattr(cx, "_subscribers", None), set):
        from .codex import max_subscribers
        lines.append(f"live: {len(cx._subscribers)} event-stream subscriber(s) of {max_subscribers()}")
    f = getattr(core, "fleet", None)
    nodes = getattr(f, "nodes", None)
    if getattr(f, "enabled", False) and isinstance(nodes, dict):
        from .fleet import max_nodes
        lines.append(f"live: {len(nodes)} fleet peer(s) registered of {max_nodes()}")
    return "\n".join(lines)


def _runs_dir(ctx: "AgentContext") -> Path | None:
    dd = getattr(getattr(getattr(ctx, "core", None), "settings", None), "data_dir", None)
    return Path(dd) / "runs" if dd is not None else None


def usage(runs: Path) -> dict[str, Any]:
    files = []
    for p in runs.glob("*.jsonl") if runs.is_dir() else []:
        try:
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                files.append((st.st_mtime, st.st_size, p))
        except OSError:
            continue
    files.sort(key=lambda x: x[0], reverse=True)  # newest first
    return {"files": files, "count": len(files), "bytes": sum(f[1] for f in files)}


def prune(runs: Path, older_than_days: int, keep: int, active: set[str], now: float | None = None) -> dict:
    u = usage(runs)
    cutoff = (now or time.time()) - older_than_days * DAY
    removed = freed = 0
    for i, (mtime, size, p) in enumerate(u["files"]):
        if i < keep or mtime >= cutoff or p.stem in active:
            continue
        try:
            p.unlink()
            removed += 1
            freed += size
        except OSError:
            continue
    return {"removed": removed, "freed": freed}


def _int(v: Any, default: int, lo: int) -> int | None:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return max(lo, v)


def _mb(n: int) -> str:
    return f"{n / 1048576:.2f} MB"


async def t_run_logs(ctx: "AgentContext", args: dict[str, Any]) -> str:
    runs = _runs_dir(ctx)
    if runs is None:
        return "run_logs: no data_dir configured on this node"
    days = _int(args.get("older_than_days"), 30, 1)
    keep = _int(args.get("keep"), 50, 10)
    if days is None or keep is None:
        return "ERROR: older_than_days and keep must be integers"
    u = await asyncio.to_thread(usage, runs)
    if not u["count"]:
        return "run_logs: no run logs yet"
    now = time.time()
    newest, oldest = u["files"][0][0], u["files"][-1][0]
    head = (f"run_logs: {u['count']} run log(s), {_mb(u['bytes'])}; newest {int((now - newest) // DAY)}d ago, "
            f"oldest {int((now - oldest) // DAY)}d ago")
    if args.get("prune") is not True:
        stale = sum(1 for i, f in enumerate(u["files"]) if i >= keep and f[0] < now - days * DAY)
        return head + f"\nprunable with older_than_days={days}, keep={keep}: {stale}"
    active = {str(getattr(ctx, "run_id", "") or "")}
    core = getattr(ctx, "core", None)
    for a in (getattr(core, "active", None) or {}).values():
        if isinstance(a, dict) and a.get("run_id"):
            active.add(str(a["run_id"]))
    r = await asyncio.to_thread(prune, runs, days, keep, active, now)
    return head + f"\npruned {r['removed']} run log(s) older than {days}d (kept newest {keep} and active runs), " \
                  f"freed {_mb(r['freed'])}"


LOG_HEALTH_MAX_BYTES = 256 * 1024 * 1024


def _scan_log(p: Path, budget: int) -> dict[str, Any]:
    import json
    from .codex import MAX_LINE_BYTES, iter_lines
    r = {"events": 0, "corrupt": 0, "overlong": 0, "torn": False, "read": 0}
    with p.open(encoding="utf-8", errors="replace") as f:
        last = ""
        for line in iter_lines(f, MAX_LINE_BYTES):
            if line is None:
                r["overlong"] += 1
                r["read"] += MAX_LINE_BYTES
                last = "\n"
                continue
            r["read"] += len(line)
            last = line
            if not line.strip():
                continue
            try:
                ok = isinstance(json.loads(line), dict)
            except (ValueError, RecursionError):
                ok = False
            if ok and line.endswith("\n"):
                r["events"] += 1
            elif line.endswith("\n"):
                r["corrupt"] += 1
            if r["read"] >= budget:
                r["partial"] = True
                break
        r["torn"] = bool(last) and not last.endswith("\n") and not r.get("partial")
    return r


async def t_log_health(ctx: "AgentContext", args: dict[str, Any]) -> str:
    d = _runs_dir(ctx)
    if d is None or not d.is_dir():
        return "log_health: no run logs on this node"
    try:
        n = int(args.get("runs", 50))
    except (TypeError, ValueError):
        n = 50
    n = max(1, min(200, n))
    from .codex import replay_limit
    cap = replay_limit()
    # round 95: glob + stat of every log ran on the event loop (100k logs: seconds) and followed
    # symlinks; newest_files is one scandir + lstat, bounded heap, regular files only.
    from .safeio import newest_files
    newest, total = await asyncio.to_thread(newest_files, d, ".jsonl", n)
    stamped = [(0.0, p) for p in reversed(newest)]  # newest first
    budget, checked, problems, partial = LOG_HEALTH_MAX_BYTES, 0, [], False

    def scan() -> None:
        nonlocal budget, checked, partial
        for _, p in stamped[:n]:
            if budget <= 0:
                partial = True
                break
            try:
                r = _scan_log(p, budget)
            except OSError:
                problems.append(f"- {p.stem}: unreadable")
                continue
            checked += 1
            budget -= r["read"]
            partial = partial or bool(r.get("partial"))
            notes = []
            if r["corrupt"]:
                notes.append(f"{r['corrupt']} corrupt line(s)")
            if r["overlong"]:
                notes.append(f"{r['overlong']} line(s) over 4 MB, skipped by every reader")
            if r["torn"]:
                notes.append("torn last line (crash mid-write)")
            if r["events"] > cap:
                notes.append(f"{r['events']} events, views show the newest {cap}")
            if notes:
                problems.append(f"- {p.stem}: " + "; ".join(notes))
    await asyncio.to_thread(scan)
    head = f"log_health: {checked} of {total} run log(s) checked, {len(problems)} with findings"
    if partial:
        head += " (stopped at the 256 MB read budget)"
    return "\n".join([head] + problems + ([] if problems else ["all checked logs intact"]))


LIMITS_TOOLS = [
    Tool("limits_status", "read-only: every load cap this node enforces (background runs, message size, MCP "
         "in-flight, agents per run, model concurrency) with its value, source and live usage.",
         "{}", t_limits_status, source="builtin", schema={}),
    Tool("run_logs", "run-log disk usage; prune=true deletes logs older than older_than_days (default 30), "
         "always keeping the newest `keep` (default 50, min 10) and active runs.",
         '{"prune"?: bool, "older_than_days"?: int, "keep"?: int}', t_run_logs, source="builtin",
         schema={"optional": {"prune": "bool", "older_than_days": "int", "keep": "int"}}),
    Tool("log_health", "read-only: integrity of the newest run logs - corrupt, over-long and torn lines, and "
         "logs bigger than a run view shows. Never prints event content.", '{"runs"?: int}', t_log_health,
         source="builtin", schema={"optional": {"runs": "int"}}),
]


def register_limits_tools(registry) -> None:
    for t in LIMITS_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
