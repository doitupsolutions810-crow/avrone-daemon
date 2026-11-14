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
import re
import stat
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

DAY = 86400
# round 61: older_than_days / keep had no upper bound, so {"prune": true, "older_than_days": 10**400}
# raised OverflowError (an int too large for float) in the cutoff, after the report was built.
MAX_DAYS = 36500  # 100 years: older than anything on disk
MAX_KEEP = 1_000_000


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
    # round 61: a bool max_parallel printed "= True", and a bool semaphore value did arithmetic
    if isinstance(mp, int) and not isinstance(mp, bool):
        free = getattr(sem, "_value", None)
        used = f", {mp - free} in use now" if isinstance(free, int) and not isinstance(free, bool) else ""
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
        # round 61: one non-numeric count (a str, None) raised TypeError in the sort and the whole
        # status call failed; run ids were printed raw, so a run id holding a newline forged lines
        from .textsafe import escape
        nums = [(k, v) for k, v in ra.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v]
        top = sorted(nums, key=lambda kv: -kv[1])[:5]
        if top:
            lines.append("agents started per run (top): "
                         + ", ".join(f"{escape(str(k))}={v}" for k, v in top))
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


_ARCHIVE_SUFFIX = re.compile(r"\.jsonl\.([1-9][0-9]{0,5})")
USAGE_SCAN_MAX = 500_000  # entries looked at per call; past it the report says so


def usage(runs: Path) -> dict[str, Any]:
    """Run logs grouped by run: (mtime, bytes, active path, [files]) newest first.

    Round 103: rotated archives (<run>.jsonl.N, round 72) were not counted, so run_logs reported a
    fraction of the real disk use, and prune deleted only the active file and left every archive
    behind (an orphaned archive was never freed). One scandir + lstat per entry, symlinks and
    non-regular files ignored."""
    groups: dict[str, list] = {}
    scanned = 0
    truncated = False
    try:
        it = os.scandir(runs)
    except OSError:
        return {"files": [], "count": 0, "bytes": 0, "archives": 0, "orphans": 0, "truncated": False}
    with it:
        for e in it:
            scanned += 1
            if scanned > USAGE_SCAN_MAX:
                truncated = True
                break
            name = e.name
            if name.endswith(".jsonl"):
                stem, active = name[:-6], True
            else:
                m = _ARCHIVE_SUFFIX.search(name)
                if m is None or m.end() != len(name):
                    continue
                stem, active = name[:m.start()], False
            if not stem:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            g = groups.setdefault(stem, [0.0, 0, False, []])
            g[0] = max(g[0], st.st_mtime)
            g[1] += st.st_size
            g[2] = g[2] or active
            g[3].append((Path(e.path), st.st_mtime_ns, st.st_size))
    files = [(g[0], g[1], runs / f"{stem}.jsonl", g[3], g[2]) for stem, g in groups.items()]
    files.sort(key=lambda x: x[0], reverse=True)  # newest first
    return {"files": files, "count": len(files), "bytes": sum(f[1] for f in files),
            "archives": sum(1 for f in files for x in f[3] if not x[0].name.endswith(".jsonl")),
            "orphans": sum(1 for f in files if not f[4]), "truncated": truncated}


def _unlink_if_same(p: Path, mtime_ns: int, size: int) -> bool:
    """Unlink only if the file is still the one usage() saw: a run that appended (or a codex
    rotation that renamed onto it) since the scan is left alone."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_mtime_ns != mtime_ns or st.st_size != size:
        return False
    try:
        p.unlink()
    except OSError:
        return False
    return True


def prune(runs: Path, older_than_days: int, keep: int, active: set[str], now: float | None = None) -> dict:
    u = usage(runs)
    # round 61: `now or time.time()` replaced an explicit now=0.0 with the wall clock
    cutoff = (time.time() if now is None else now) - min(max(older_than_days, 1), MAX_DAYS) * DAY  # the tool's own floor: 1 day
    removed = freed = 0
    for i, f in enumerate(u["files"]):
        mtime, _size, p, parts = f[0], f[1], f[2], f[3]
        if i < keep or mtime >= cutoff or p.stem in active:
            continue
        gone = 0
        # oldest archive first, active log last: a crash part-way never leaves a gap in the
        # newest-first archive chain that log_chain() walks
        for path, mns, sz in sorted(parts, key=lambda x: -_archive_no(x[0])):
            if _unlink_if_same(path, mns, sz):
                freed += sz
                gone += 1
        removed += 1 if gone else 0
    return {"removed": removed, "freed": freed}


def _archive_no(p: Path) -> int:
    m = _ARCHIVE_SUFFIX.search(p.name)
    return int(m.group(1)) if m and m.end() == len(p.name) else 0


def _int(v: Any, default: int, lo: int, hi: int = MAX_KEEP) -> int | None:
    """Round 61: through argnum.tool_int (the one integer rule), clamped to [lo, hi]. Junk is None
    rather than the default: prune deletes, so a garbled keep must never prune with keep=50."""
    from .argnum import tool_int
    return tool_int(v, default, lo, hi, bad=None)


def _ago(now: float, t: float) -> str:
    """Round 61: a log with a future mtime (clock skew, a copied file) read "newest -5d ago"."""
    if t > now:
        return "in the future (clock skew)"
    return f"{int((now - t) // DAY)}d ago"


def _mb(n: int) -> str:
    return f"{n / 1048576:.2f} MB"


async def t_run_logs(ctx: "AgentContext", args: dict[str, Any]) -> str:
    runs = _runs_dir(ctx)
    if runs is None:
        return "run_logs: no data_dir configured on this node"
    days = _int(args.get("older_than_days"), 30, 1, MAX_DAYS)
    keep = _int(args.get("keep"), 50, 10, MAX_KEEP)
    if days is None or keep is None:
        return "ERROR: older_than_days and keep must be integers"
    u = await asyncio.to_thread(usage, runs)
    if not u["count"]:
        return "run_logs: no run logs yet"
    now = time.time()
    newest, oldest = u["files"][0][0], u["files"][-1][0]
    extra = ""
    if u.get("archives"):
        extra += f" (incl. {u['archives']} rotated archive file(s))"
    if u.get("orphans"):
        extra += f", {u['orphans']} run(s) with archives only"
    if u.get("truncated"):
        extra += f", listing capped at {USAGE_SCAN_MAX} entries"
    head = (f"run_logs: {u['count']} run log(s), {_mb(u['bytes'])}{extra}; newest {_ago(now, newest)}, "
            f"oldest {_ago(now, oldest)}")
    from .argnum import tool_bool
    # round 61: MCP / fleet / admin skip check(), and prune="true" from them silently did a dry run
    if not tool_bool(args.get("prune"), False):
        stale = sum(1 for i, f in enumerate(u["files"]) if i >= keep and f[0] < now - days * DAY)
        return head + f"\nprunable with older_than_days={days}, keep={keep}: {stale}"
    active = {str(getattr(ctx, "run_id", "") or "")}
    core = getattr(ctx, "core", None)
    act = getattr(core, "active", None)
    # round 61: a non-dict core.active (a list) raised AttributeError mid-prune
    for a in (act.values() if isinstance(act, dict) else ()):
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
    from .codex import open_log
    # round 106: p.open() followed a FIFO swapped in after the listing and parked the scan
    # thread (and the log_health call awaiting it) forever; open_log is O_NOFOLLOW|O_NONBLOCK
    with open_log(p) as f:
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
    from .argnum import tool_int
    n = tool_int(args.get("runs"), 50, 1, 200)
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
