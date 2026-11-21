"""replay_guard + stream_status.

    replay_guard {}
        This node's fleet replay protection: live nonces in the cache vs its cap, whether the cache
        is persisted (data_dir/fleet_nonces.json) and readable, and whether the boot guard is active
        (the saved cache was unreadable at boot, so requests signed before boot are refused for two
        windows). Verdict OK / DEGRADED / OFF. Never prints nonces. Read-only.

    stream_status {}
        The live event stream (/ws/events): connected subscribers, each one's queue depth vs its
        1000-event limit, events emitted and events dropped because a subscriber was too slow, the
        subscriber cap (AVRONE_MAX_WS_SUBSCRIBERS) and connections refused at it. Read-only.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from .tools import Tool, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext


def replay_guard(fleet: Any) -> dict[str, Any]:
    from .fleet import NONCE_CACHE_MAX, WINDOW_S
    now = time.time()
    raw_n = getattr(fleet, "_nonces", None)
    # round 50: a non-dict cache (None after a failed load, a list from a test double) raised
    # out of dict(); bools are not expiry times
    nonces = dict(raw_n) if isinstance(raw_n, dict) else {}
    live = sum(1 for v in nonces.values()
               if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= now)
    path = getattr(fleet, "_nonce_path", None)
    persisted = readable = False
    exists = False
    if path is not None:
        try:
            # round 50: Path.exists() raises PermissionError (an unreadable data_dir) on 3.12
            exists = path.exists() or path.is_symlink()
        except OSError:
            exists = True  # it is there, we just can not look: report it as unreadable
    if exists:
        persisted = True
        try:
            # round 50: the same reader and parser the fleet boots with (O_NOFOLLOW, 64 MB cap,
            # strict json), so "readable" here means the fleet can load it too
            from .fleet import _load_nonce_snapshot
            readable = isinstance(_load_nonce_snapshot(path), dict)
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            readable = False
    guard = getattr(fleet, "_boot_guard", None)
    guard_left = 0
    # round 50: a boot guard of inf raised OverflowError and NaN ValueError out of int()
    if isinstance(guard, (int, float)) and not isinstance(guard, bool):
        try:
            g = float(guard)
        except OverflowError:
            g = math.inf
        left = g + 2 * WINDOW_S - now
        if math.isfinite(left):
            guard_left = max(0, min(int(left), 2 * WINDOW_S))
        elif left > 0:
            guard_left = 2 * WINDOW_S
    return {"enabled": bool(getattr(fleet, "enabled", False)), "live": live, "cap": NONCE_CACHE_MAX,
            "persisted": persisted, "readable": readable, "has_path": path is not None,
            "boot_guard_s": guard_left, "window_s": WINDOW_S}


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_replay_guard(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = getattr(getattr(ctx, "core", None), "fleet", None)
    if f is None or not getattr(f, "enabled", False):
        return "replay_guard: OFF - fleet disabled on this node (no AVRONE_FLEET_TOKEN), nothing to replay"
    r = replay_guard(f)
    problems = []
    if not r["has_path"]:
        problems.append("cache is memory-only: a restart forgets it")
    elif r["persisted"] and not r["readable"]:
        problems.append("saved cache unreadable: the next restart falls back to the boot guard")
    if r["live"] >= 0.9 * r["cap"]:
        problems.append("cache near its cap: new signed requests will be refused when full")
    head = "DEGRADED" if problems else "OK"
    lines = [f"replay_guard: {head} - {r['live']}/{r['cap']} live nonce(s), window {r['window_s']}s, "
             f"persisted={'yes' if r['persisted'] else 'not yet'}",
             f"boot guard: {'active, ' + str(r['boot_guard_s']) + 's left' if r['boot_guard_s'] else 'off'}"]
    lines += [f"- {p}" for p in problems]
    return "\n".join(lines)


STREAM_MAX_ROWS = 30


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


async def t_stream_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    cx = getattr(getattr(ctx, "core", None), "codex", None)
    if cx is None or not hasattr(cx, "_subscribers"):
        return "stream_status: no event bus on this core"
    try:
        subs = list(cx._subscribers)
    except TypeError:
        subs = []
    depths = []
    for q in subs:
        # round 50: a subscriber with no qsize()/maxsize (a test double, a wrapped socket) raised
        # AttributeError and the whole status failed; asyncio treats maxsize <= 0 as unbounded
        try:
            d, m = q.qsize(), getattr(q, "maxsize", 0)
        except Exception:  # noqa: BLE001 - one odd subscriber must not hide the rest
            continue
        if not isinstance(d, int) or isinstance(d, bool):
            continue
        m = m if isinstance(m, int) and not isinstance(m, bool) and m > 0 else 0
        depths.append((d, m))
    # round 50: deepest first, and at most STREAM_MAX_ROWS rows (the cap allows 1024 subscribers)
    depths.sort(key=lambda x: (-(x[0] / x[1] if x[1] else 0), -x[0]))
    emitted, dropped = _count(getattr(cx, "emitted", 0)), _count(getattr(cx, "dropped", 0))
    from .codex import max_subscribers
    cap = max_subscribers()
    refused = _count(getattr(cx, "refused", 0))
    lines = [f"stream_status: {len(subs)} subscriber(s) of {cap} allowed; {emitted} event(s) emitted since start, "
             f"{dropped} dropped, {refused} connection(s) refused at the cap"]
    for i, (d, m) in enumerate(depths[:STREAM_MAX_ROWS], 1):
        flag = " FULL - dropping" if m and d >= m else (" backed up" if m and d >= m * 0.8 else "")
        lines.append(f"- subscriber {i}: {d}/{m or 'unbounded'} queued{flag}")
    if len(depths) > STREAM_MAX_ROWS:
        lines.append(f"... {len(depths) - STREAM_MAX_ROWS} more subscriber(s) not shown (least backed up)")
    if dropped:
        lines.append("a slow dashboard missed events; the run logs still hold every one (codex_search)")
    if refused or len(subs) >= cap:
        lines.append("the stream is or was full: close idle dashboards or raise AVRONE_MAX_WS_SUBSCRIBERS")
    return "\n".join(lines)


GATEWAYVIEW_TOOLS = [
    Tool("replay_guard", "read-only: fleet replay protection state (live nonces vs cap, persisted cache, boot "
         "guard). Never prints nonces.", "{}", t_replay_guard, source="builtin", schema={}),
    Tool("stream_status", "read-only: live event stream subscribers, queue depths, events emitted and dropped.",
         "{}", t_stream_status, source="builtin", schema={}),
]


def register_gatewayview_tools(registry) -> None:
    for t in GATEWAYVIEW_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
