"""fleet_status: a read-only, bounded, redacted summary of THIS node's view of its fleet.

    fleet_status {"limit"?: int, "need"?: str, "format"?: "text"|"json"}
        limit  per-node rows to list (1..50, default 20); totals always cover every known node
        need   optional capability for the "would pick next" preview (same match as select_nodes:
               a tool name, the runner kind or a truthy capability key; default spawn)

Reports: this node's id, whether the fleet is enabled, the governor health of THIS node (ok /
throttled / hot, headroom, active agents), the policy switches (parent, allowlist, privileged
gate, offload, multi-hop relay, squad network dispatch) shown as on/off only, and every known
child node's presence (online / degraded / offline, from heartbeats) with its health state (ok /
throttled / hot / unknown / stale), score, eligibility and in-flight load, plus totals and which
nodes FleetManager.select_nodes would pick next.

Never shown: the fleet token / HMAC key, node or parent URLs (they can carry credentials), raw
peer payloads (capabilities beyond the runner kind, presence trees, agent tasks). Node ids and
labels are charset-limited, peer health is re-sanitized (fleet.json is not trusted), and output
is capped at FLEET_STATUS_MAX_CHARS. Nothing here writes, spawns, signs or touches the network.
"""
from __future__ import annotations

import math
import json
import re
import time
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

FLEET_STATUS_MAX_LIMIT = 50
FLEET_STATUS_MAX_CHARS = 8000
FLEET_STATUS_PICK = 5
_NEED_RE = re.compile(r"[A-Za-z0-9_.:\-]{0,64}")
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.:\-]")


def _label(v: Any, n: int = 64) -> str:
    s = _SAFE_LABEL_RE.sub("?", str(v))
    return s[:n] or "?"


def _onoff(v: Any) -> str:
    return "on" if v else "off"


def _int(v: Any, default: int, lo: int, hi: int) -> int | None:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:  # a peer-reported 400-digit int
        return None
    return f if math.isfinite(f) else None


def _settings(f: Any) -> dict[str, str]:
    from . import fleet as fl
    max_hops = _num(getattr(f, "max_hops", 0)) or 0
    try:
        offload = f.offload_mode()
    except Exception:  # noqa: BLE001  -- advisory view
        offload = "off"
    try:
        dispatch = f.squad_dispatch()
    except Exception:  # noqa: BLE001
        dispatch = "local"
    return {
        "fleet": _onoff(getattr(f, "enabled", False)),
        "parent": _onoff(getattr(f, "parent", "")),
        "public_url": _onoff(getattr(f, "public_url", "")),
        "allowlist": _onoff(getattr(f, "allow", None)),
        "require_allow": _onoff(getattr(f, "require_allow", False)),
        "hops": _onoff(max_hops > 0),
        "privileged": _onoff(getattr(f, "privileged", False)),
        "privileged_nodes": _onoff(getattr(f, "privileged_nodes", None)),
        "offload": _onoff(offload != "off"),
        "network_dispatch": _onoff(dispatch != "local"),
        "agent_migrate": _onoff(fl.migrate_enabled()),
    }


def _local(core: Any) -> dict[str, Any] | None:
    from .fleet import local_health
    h = local_health(core)
    if not isinstance(h, dict):
        return None
    out: dict[str, Any] = {"state": _label(h.get("state", "unknown"), 16)}
    for k in ("headroom", "load", "mem_pct", "disk_free_pct", "temp_c"):
        v = _num(h.get(k))
        if v is not None:
            out[k] = round(v, 3)
    aa = h.get("active_agents")
    if isinstance(aa, int) and not isinstance(aa, bool):
        out["active_agents"] = aa
    return out


def fleet_status(ctx: "AgentContext", limit: int = 20, need: str = "") -> dict[str, Any]:
    from .fleet import EXCLUDED_STATES, health_score, sanitize_health
    core = ctx.core
    f = getattr(core, "fleet", None)
    rep: dict[str, Any] = {"node_id": None, "fleet": "off", "local_health": _local(core),
                           "settings": None, "totals": None, "nodes": [], "more": 0, "would_pick": []}
    if f is None:
        return rep
    rep["node_id"] = _label(getattr(f, "node_id", "?"))
    rep["settings"] = _settings(f)
    rep["fleet"] = rep["settings"]["fleet"]
    now = time.time()
    by_status: dict[str, int] = {}
    by_state: dict[str, int] = {}
    eligible = 0
    rows = []
    nodes = list(dict(getattr(f, "nodes", {}) or {}).values())
    for n in sorted((n for n in nodes if isinstance(n, dict)), key=lambda n: str(n.get("node_id"))):
        # copy only well-typed fields; health is re-sanitized (fleet.json may be stale or edited)
        clean = {"node_id": n.get("node_id"), "last_seen": _num(n.get("last_seen")) or 0.0,
                 "load": _num(n.get("load")) or 0.0, "health": sanitize_health(n.get("health"))}
        status = f.status_of(clean)
        state = f.node_state(clean)
        try:
            allowed = bool(f.allowed(str(n.get("node_id", ""))))
        except Exception:  # noqa: BLE001
            allowed = False
        ok = status == "online" and state not in EXCLUDED_STATES and allowed
        eligible += ok
        by_status[status] = by_status.get(status, 0) + 1
        by_state[state] = by_state.get(state, 0) + 1
        caps = n.get("capabilities") if isinstance(n.get("capabilities"), dict) else {}
        kids = n.get("children")
        h = clean["health"] or {}
        rows.append({"node_id": _label(n.get("node_id")), "status": status, "state": _label(state, 16),
                     "score": health_score(clean) if status == "online" else None,
                     "eligible": ok, "allowed": allowed, "load": clean["load"],
                     "headroom": h.get("headroom"), "temp_c": h.get("temp_c") if status == "online" else None,
                     "runner": _label(caps.get("runner", "?"), 32),
                     "children": len(kids) if isinstance(kids, (list, dict)) else 0,
                     "seen_s_ago": max(0, int(now - clean["last_seen"])) if clean["last_seen"] else None})
    rep["totals"] = {"known": len(rows), "eligible": eligible, "by_status": by_status, "by_state": by_state}
    rep["nodes"] = rows[:limit]
    rep["more"] = max(0, len(rows) - limit)
    try:
        rep["would_pick"] = [_label(n.get("node_id")) for n in f.select_nodes(FLEET_STATUS_PICK, need or None)]
    except Exception:  # noqa: BLE001  -- a malformed record must not break the read-only view
        rep["would_pick"] = []
    rep["need"] = need or None
    return rep


def _cap(text: str) -> str:
    return text if len(text) <= FLEET_STATUS_MAX_CHARS else text[:FLEET_STATUS_MAX_CHARS] + "\n...[truncated]"


def _json(rep: dict[str, Any]) -> str:
    """JSON within the cap: drop whole node rows (counted in "more") so the result stays parseable."""
    text = json.dumps(rep, indent=1, sort_keys=True)
    while len(text) > FLEET_STATUS_MAX_CHARS and rep["nodes"]:
        rep["nodes"] = rep["nodes"][:-1]
        rep["more"] += 1
        text = json.dumps(rep, indent=1, sort_keys=True)
    return _cap(text)


async def t_fleet_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    limit = _int(args.get("limit"), 20, 1, FLEET_STATUS_MAX_LIMIT)
    if limit is None:
        return "ERROR: limit must be an integer"
    need = str(args.get("need") or "").strip()
    if not _NEED_RE.fullmatch(need):
        return "ERROR: need must be letters, digits, '.', '_', ':' or '-' (max 64), e.g. \"docker\""
    rep = fleet_status(ctx, limit, need)
    if str(args.get("format", "text")).lower() == "json":
        return _json(rep)
    lh = rep["local_health"]
    lhs = ("unknown (no governor reading)" if not lh else
           f"{lh['state']} headroom={lh.get('headroom', '?')} active_agents={lh.get('active_agents', '?')}"
           + (f" temp={lh['temp_c']}C" if lh.get("temp_c") is not None else ""))
    if rep["settings"] is None:
        return _cap("fleet_status (read-only; tokens and URLs never shown)\n"
                    f"no fleet manager on this node\nthis node health: {lhs}")
    s = rep["settings"]
    lines = [f"fleet_status node {rep['node_id']} (read-only; tokens and URLs never shown)",
             f"this node health: {lhs}",
             "settings: " + "  ".join(f"{k}={v}" for k, v in s.items())]
    t = rep["totals"]
    lines.append(f"known nodes: {t['known']}  eligible: {t['eligible']}  "
                 f"presence: {', '.join(f'{k}={v}' for k, v in sorted(t['by_status'].items())) or '-'}  "
                 f"health: {', '.join(f'{k}={v}' for k, v in sorted(t['by_state'].items())) or '-'}")
    for r in rep["nodes"]:
        extra = f" score={r['score']}" if r["score"] is not None else ""
        if r["temp_c"] is not None:
            extra += f" temp={r['temp_c']}C"
        seen = "never" if r["seen_s_ago"] is None else f"{r['seen_s_ago']}s ago"
        lines.append(f"- {r['node_id']} [{r['status']}/{r['state']}]{extra} load={r['load']:g} "
                     f"runner={r['runner']} children={r['children']} seen={seen}"
                     f"{'' if r['allowed'] else ' NOT-ALLOWED'}{' eligible' if r['eligible'] else ''}")
    if rep["more"]:
        lines.append(f"...[{rep['more']} more nodes; raise limit]")
    if not t["known"]:
        lines.append("no child nodes registered")
    lines.append(f"would pick next{' for ' + need if need else ''}: {', '.join(rep['would_pick']) or '(none eligible)'}")
    return _cap("\n".join(lines))


FLEET_STATUS_TOOLS: list[Tool] = [
    Tool("fleet_status",
         "read-only: THIS node's fleet view: own governor health, policy switches (parent, allowlist, "
         "privileged, offload, hops, network dispatch) as on/off, every known node's presence "
         "(online/degraded/offline) + health (ok/throttled/hot), score, eligibility and load, totals, "
         "and which nodes would be picked next. Tokens, keys and URLs are never shown.",
         '{"limit"?: int, "need"?: str, "format"?: "text"|"json"}',
         t_fleet_status, source="builtin",
         schema={"optional": {"limit": "int", "need": "str", "format": "str"}}),
]


def register_fleetstatus_tools(registry) -> None:
    for t in FLEET_STATUS_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
