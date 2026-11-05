"""Telemetry + System Map: AVRONE traces its own nodes.

Nodes are the parts of AVRONE itself: the core, the model providers, the
runner (environment), every tool (builtin, plugin, MCP server) and every agent
that has run. Edges are the calls that flowed between them, built from the
Eternal CodeX event logs. Nothing here probes external networks.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .trinity import TrinityCore


DEFAULT_WINDOW_S = 86400.0
MAX_WINDOW_S = 366 * 86400.0
DEFAULT_MAX_FILE_BYTES, HARD_MAX_FILE_BYTES = 8 << 20, 256 << 20


def _finite(v: Any, default: float = 0.0) -> float:
    """v as a finite float; bools, strings, NaN, inf and junk read as default."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        f = float(v)
    except OverflowError:  # a 400-digit int ts in a run log 500'd /api/telemetry
        return default
    return f if math.isfinite(f) else default


def clamp_window(window_s: Any) -> float:
    """A usable look-back window: NaN, inf, zero, negative or junk -> 24 h; capped at 366 days.
    ?hours=nan used to make every ts comparison False, so the HUD silently showed nothing."""
    w = _finite(window_s, DEFAULT_WINDOW_S)
    if w <= 0:
        return DEFAULT_WINDOW_S
    return min(w, MAX_WINDOW_S)


def max_file_bytes() -> int:
    """Bytes read from the end of each log (AVRONE_TELEMETRY_MAX_FILE_BYTES, default 8 MiB, clamped
    64 KiB..256 MiB). A runaway multi-GB run log used to be read whole into memory per request."""
    try:
        n = int(os.getenv("AVRONE_TELEMETRY_MAX_FILE_BYTES", "").strip() or DEFAULT_MAX_FILE_BYTES)
    except ValueError:
        n = DEFAULT_MAX_FILE_BYTES
    return max(64 << 10, min(HARD_MAX_FILE_BYTES, n))


def _read_tail(p: Path, limit: int) -> str | None:
    try:
        with open(p, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            cut = size > limit
            if cut:
                f.seek(size - limit)
            raw = f.read(limit)
    except OSError:
        return None
    if cut:  # drop the partial first line
        nl = raw.find(b"\n")
        raw = raw[nl + 1:] if nl >= 0 else b""
    return raw.decode("utf-8", errors="replace")


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:  # deleted / rotated between glob and stat
        return None


def _iter_events(log_dir: Path, since: float, max_files: int = 200):
    """Events (dicts with a finite float ts) newer than since. Never raises on bad files or lines:
    one non-UTF-8 byte, a log deleted mid-scan, or a line that was a JSON list used to 500 the HUD."""
    since = _finite(since, 0.0)
    try:
        found = [(m, p) for p in Path(log_dir).glob("*.jsonl") if (m := _mtime(p)) is not None]
    except OSError:
        return
    found.sort(key=lambda mp: mp[0], reverse=True)
    limit = max_file_bytes()
    for m, p in found[:max(1, int(max_files))]:
        if m < since:
            continue
        text = _read_tail(p, limit)
        if text is None:
            continue
        for line in text.splitlines():
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):  # JSONDecodeError, huge ints, deep nesting
                continue
            if not isinstance(ev, dict):
                continue
            ts = _finite(ev.get("ts"), 0.0)
            if ts >= since:
                ev["ts"] = ts
                yield ev


def _key(v: Any) -> str | None:
    """A run id usable as a dict key: str or int, else None (a list id raised TypeError)."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return None
    return str(v)


def _fields(ev: dict) -> tuple[str | None, str, str, dict, float]:
    data = ev.get("data")
    agent = ev.get("agent")
    kind = ev.get("kind")
    return (_key(ev.get("run_id")), kind if isinstance(kind, str) else "",
            agent if isinstance(agent, str) else "", data if isinstance(data, dict) else {},
            _finite(ev.get("ts"), 0.0))


def telemetry(core: "TrinityCore", window_s: float = 86400) -> dict[str, Any]:
    """Aggregate metrics over the last window_s seconds."""
    window_s = clamp_window(window_s)
    since = time.time() - window_s
    runs: dict[str, dict[str, Any]] = {}
    tool_calls: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    agent_roles: Counter[str] = Counter()
    errors = 0
    pending_calls: dict[tuple[str, str], tuple[str, float]] = {}
    tool_latency: dict[str, list[float]] = defaultdict(list)
    throttles: list[dict[str, Any]] = []
    resumes = 0
    net_place: Counter[str] = Counter()
    net_fallback = 0

    for ev in _iter_events(core.codex.log_dir, since):
        rid, kind, agent, data, ts = _fields(ev)
        if kind.startswith("run.") and rid is None:
            continue
        if kind == "run.start":
            mode = data.get("mode")
            runs[rid] = {"run_id": rid, "mode": mode if isinstance(mode, str) else None, "start": ts, "end": None,
                         "prompt": str(data.get("prompt", ""))[:120]}
        elif kind == "run.done" and rid in runs:
            runs[rid]["end"] = max(ts, runs[rid]["start"])  # a clock step back must not give a negative duration
        elif kind == "agent.spawn":
            agent_roles[str(data.get("role", "?"))] += 1
        elif kind == "tool.call":
            tool = str(data.get("tool"))
            tool_calls[tool] += 1
            pending_calls[(rid, agent)] = (tool, ts)
        elif kind == "tool.result":
            tool = str(data.get("tool"))
            if str(data.get("output", "")).startswith("ERROR"):
                tool_errors[tool] += 1
            started = pending_calls.pop((rid, agent), None)
            if started and started[0] == tool:
                tool_latency[tool].append(ts - started[1])
        elif kind == "error":
            errors += 1
        elif kind == "governor.throttle":
            throttles.append({"run_id": rid, "ts": ts, "workers": data.get("workers"),
                              "parallel": data.get("parallel"), "reason": data.get("reason")})
        elif kind == "checkpoint.resume":
            resumes += 1
        elif kind == "fleet.netsquad_place":
            net_place[str(data.get("node", "?"))] += 1
        elif kind == "fleet.netsquad_fallback":
            net_fallback += 1

    done = [r for r in runs.values() if r["end"] is not None]
    durations = sorted(r["end"] - r["start"] for r in done)

    def pct(xs: list[float], p: float) -> float | None:
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else None

    return {
        "window_s": window_s,
        "runs": {"total": len(runs), "completed": len(done), "running": len(runs) - len(done),
                 "by_mode": dict(Counter(r["mode"] for r in runs.values())),
                 "p50_s": pct(durations, 0.5), "p95_s": pct(durations, 0.95)},
        "agents": {"spawned": sum(agent_roles.values()), "by_role": dict(agent_roles.most_common(20)),
                   "active_now": _active_now(core)},
        "tools": {t: {"calls": c, "errors": tool_errors.get(t, 0),
                      "avg_s": round(sum(tool_latency[t]) / len(tool_latency[t]), 2) if tool_latency[t] else None}
                  for t, c in tool_calls.most_common()},
        "errors": errors,
        "recent_runs": sorted(runs.values(), key=lambda r: r["start"], reverse=True)[:10],
        "node": node_health(core),
        "fleet_nodes": fleet_health(core),
        "netsquad": {"placements": sum(net_place.values()), "by_node": dict(net_place.most_common()),
                     "fallbacks": net_fallback},
        "resilience": {"throttles": len(throttles), "checkpoint_resumes": resumes,
                       "recent_throttles": sorted(throttles, key=lambda t: t["ts"], reverse=True)[:5]},
    }


def _active_now(core: Any) -> list[str]:
    try:
        return [str(n) for n, a in dict(getattr(core, "active", {}) or {}).items()
                if isinstance(a, dict) and a.get("status") == "working"]
    except Exception:
        return []


def node_health(core: "TrinityCore") -> dict[str, Any]:
    """Governor state + this node's health (load, memory, disk, temperature). Never raises."""
    try:
        gov = getattr(core, "governor", None)
        if gov is None:
            from .governor import Governor
            gov = Governor.from_settings(core.settings)
        return gov.state()
    except Exception as e:
        return {"enabled": False, "error": f"{type(e).__name__}: {e}"[:200]}


def fleet_health(core: "TrinityCore") -> list[dict[str, Any]]:
    """Health of each fleet child (status, governor state, routing score, eligibility). Never raises."""
    try:
        fleet = getattr(core, "fleet", None)
        if fleet is None or not getattr(fleet, "enabled", False):
            return []
        rows = fleet.health_table()
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def system_map(core: "TrinityCore", window_s: float = 86400) -> dict[str, Any]:
    """Graph of AVRONE's own nodes and the traffic between them."""
    s = core.settings
    nodes: dict[str, dict[str, Any]] = {}
    edges: Counter[tuple[str, str]] = Counter()

    def node(nid: str, kind: str, label: str, **meta: Any) -> None:
        nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label, **meta})

    node("core", "core", "Trinity Core")
    node("codex", "store", "Eternal CodeX", path=str(core.codex.log_dir))
    node("runner", "runner", f"Sovereign OS ({core.runner.kind})", workspace=str(core.runner.workspace))
    model = getattr(s, "llm_model", "?")
    node("model:worker", "model", f"Worker model: {model}", base_url=getattr(s, "llm_base_url", ""))
    lead = getattr(s, "lead_model", "") or model
    node("model:lead", "model", f"Lead model: {lead}")
    edges[("core", "codex")] += 1
    edges[("core", "runner")] += 1

    sources: dict[str, str] = {}
    for t in core.tools.list():
        src = t["source"]
        sources[t["name"]] = src
        group = "tools:builtin" if src == "builtin" else f"tools:{src}"
        node(group, "toolgroup", src if src != "builtin" else "Built-in tools")
        node(f"tool:{t['name']}", "tool", t["name"], source=src)
        edges[(group, f"tool:{t['name']}")] += 0
        edges[("core", group)] += 0

    fleet = getattr(core, "fleet", None)
    if fleet is not None and fleet.enabled:
        node("fleet", "fleet", f"Fleet node: {fleet.node_id}")
        edges[("core", "fleet")] += 0
        if fleet.parent:
            node("fleet:parent", "node", f"Parent: {fleet.parent}")
            edges[("fleet", "fleet:parent")] += 1
        from .presence import flatten

        for n in fleet.list_nodes():
            hs = fleet.node_state(n) if hasattr(fleet, "node_state") else "unknown"
            hs = hs if isinstance(hs, str) else "unknown"
            label = f"{n['node_id']} ({n['status']}" + (f", {hs})" if hs not in ("unknown",) else ")")
            node(f"node:{n['node_id']}", "node", label, url=n["url"])
            edges[("fleet", f"node:{n['node_id']}")] += 1
            pres = n.get("presence") or {"node_id": n["node_id"], "children": {}, "agents": []}
            for row in flatten(pres):
                nid = row["node_id"]
                node(f"node:{nid}", "node", nid)
                parent_route = row["route"].rsplit("/", 1)[0] if "/" in row["route"] else ""
                if row["route"]:
                    parent = parent_route.rsplit("/", 1)[-1] if parent_route else n["node_id"]
                    edges[(f"node:{parent}", f"node:{nid}")] += 1
                for a in row["agents"]:
                    if a.get("status") == "working":
                        node(f"ragent:{nid}/{a['name']}", "agent", f"{a['name']}@{nid}")
                        edges[(f"node:{nid}", f"ragent:{nid}/{a['name']}")] += 1

    since = time.time() - clamp_window(window_s)
    for ev in _iter_events(core.codex.log_dir, since):
        _rid, kind, agent, data, _ts = _fields(ev)
        if kind == "agent.spawn":
            role = str(data.get("role", "Agent"))
            node(f"role:{role}", "agent", role)
            parent = data.get("parent")
            parent = str(parent) if isinstance(parent, (str, int)) and not isinstance(parent, bool) else ""
            edges[(f"role:{parent.rsplit('-', 1)[0]}" if parent else "core", f"role:{role}")] += 1
            edges[(f"role:{role}", "model:worker")] += 1
        elif kind == "tool.call":
            tool = str(data.get("tool"))
            role = agent.rsplit("-", 1)[0]
            node(f"role:{role}", "agent", role)
            node(f"tool:{tool}", "tool", tool, source=sources.get(tool, "unknown"))
            edges[(f"role:{role}", f"tool:{tool}")] += 1
            if tool in ("shell", "python", "git", "docker_run"):
                edges[(f"tool:{tool}", "runner")] += 1
        elif kind == "lead.merge":
            edges[("core", "model:lead")] += 1
        elif kind == "fleet.netsquad_place":  # a squad subtask placed on a remote node (netsquad)
            nid = str(data.get("node", "?"))
            node(f"node:{nid}", "node", nid)
            nodes[f"node:{nid}"]["netsquad"] = nodes[f"node:{nid}"].get("netsquad", 0) + 1
            edges[("fleet" if "fleet" in nodes else "core", f"node:{nid}")] += 1

    # fix parent edges that point at an unknown role node
    out_edges = []
    for (a, b), w in edges.items():
        if a not in nodes:
            node(a, "agent", a.split(":", 1)[-1])
        out_edges.append({"from": a, "to": b, "weight": w})
    return {"nodes": list(nodes.values()), "edges": out_edges}


def to_mermaid(graph: dict[str, Any], min_weight: int = 0) -> str:
    """Render the system map as a Mermaid flowchart (paste into GitHub/markdown)."""
    ids: dict[str, str] = {}

    def mid(x: Any) -> str:
        """Mermaid-safe id, unique per node id: "a-b" and "a_b" used to collapse into one node.
        Only ASCII alphanumerics survive (str.isalnum() let other scripts through)."""
        x = str(x)
        if x not in ids:
            base = "n_" + "".join(c if (c.isascii() and c.isalnum()) else "_" for c in x)[:60]
            ids[x] = base if base not in ids.values() else f"{base}_{hashlib.sha1(x.encode('utf-8', 'surrogatepass')).hexdigest()[:8]}"
        return ids[x]

    def label(v: Any) -> str:
        """Labels come from run logs (model-written role and tool names): quotes, brackets, pipes,
        backticks and newlines could close the node text and inject Mermaid statements (click)."""
        t = " ".join(str(v).split())[:120]
        for ch, ent in (("&", "#amp;"), ('"', "#quot;"), ("<", "#lt;"), (">", "#gt;"), ("[", "#91;"), ("]", "#93;"),
                        ("|", "#124;"), ("`", "#96;"), ("{", "#123;"), ("}", "#125;")):
            t = t.replace(ch, ent)
        return t or "?"

    lines = ["flowchart LR"]
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict) or "id" not in n:
            continue
        lines.append(f'  {mid(n["id"])}["{label(n.get("label", n["id"]))}"]')
    floor = _finite(min_weight, 0.0)
    for e in graph.get("edges") or []:
        if not isinstance(e, dict) or "from" not in e or "to" not in e:
            continue
        w = _finite(e.get("weight"), 0.0)
        if w >= floor:
            tag = f"|{int(w) if w == int(w) else w}|" if w else ""
            lines.append(f"  {mid(e['from'])} -->{tag} {mid(e['to'])}")
    return "\n".join(lines)
