"""Presence: where every agent is, down to the node, container, host, networks and services.

Each node builds a snapshot of itself (environment + live agents). Routers fold
their children's snapshots into their heartbeat, so the root ("Main AVRONE")
holds the whole tree and can answer "where is agent X and what can it reach?".
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .trinity import TrinityCore

MAX_AGENTS = 50
_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


def _host_interfaces() -> list[dict[str, Any]]:
    """Network interfaces + addresses of the host AVRONE runs on (no external probing)."""
    if shutil.which("ip"):
        try:
            out = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=3).stdout
            ifs = []
            for i in json.loads(out or "[]"):
                addrs = [f"{a['local']}/{a.get('prefixlen')}" for a in i.get("addr_info", []) if a.get("local")]
                if addrs:
                    ifs.append({"name": i.get("ifname"), "state": i.get("operstate"), "addrs": addrs})
            if ifs:
                return ifs
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(info[4][0])
    except OSError:
        pass
    try:  # primary outbound address (UDP connect sends no packets)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return [{"name": "host", "state": "UNKNOWN", "addrs": sorted(addrs)}]


async def environment(core: "TrinityCore", ttl: float = 60) -> dict[str, Any]:
    cached = _CACHE.get(id(core))
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    r = core.runner
    container: dict[str, Any] | None = None
    if getattr(r, "kind", "") == "docker":
        res = await r.run("hostname; hostname -I 2>/dev/null || true", timeout=10)
        lines = (getattr(res, "stdout", "") or "").split("\n")
        container = {"name": getattr(r, "container", None), "image": getattr(core.settings, "docker_image", None),
                     "hostname": lines[0].strip() if lines else None,
                     "addrs": lines[1].split() if len(lines) > 1 else []}
    ext = getattr(core, "extensions", {}) or {}
    mcp = (ext.get("mcp") or {}).get("servers") or {}
    fleet = getattr(core, "fleet", None)
    env = {
        "device": {"hostname": socket.gethostname(), "os": f"{platform.system()} {platform.release()}",
                   "arch": platform.machine(), "python": platform.python_version(), "pid": os.getpid()},
        "runner": {"kind": getattr(r, "kind", "?"), "workspace": str(getattr(r, "workspace", "")), "container": container},
        "networks": _host_interfaces(),
        "services": {"public_url": getattr(fleet, "public_url", None) or None,
                     "tools": len(core.tools.names()),
                     "mcp_servers": sorted(mcp) if isinstance(mcp, dict) else mcp,
                     "model": getattr(core.settings, "llm_model", None),
                     "provider_url": getattr(core.settings, "llm_base_url", None)},
    }
    _CACHE[id(core)] = (time.time(), env)
    return env


def agents(core: "TrinityCore") -> list[dict[str, Any]]:
    items = sorted(core.active.items(), key=lambda kv: (kv[1].get("status") != "working", -kv[1].get("started", 0)))
    return [{"name": n, **{k: a.get(k) for k in ("role", "status", "parent", "run_id", "task", "depth", "started")}}
            for n, a in items[:MAX_AGENTS]]


async def snapshot(core: "TrinityCore") -> dict[str, Any]:
    fleet = getattr(core, "fleet", None)
    snap: dict[str, Any] = {"node_id": getattr(fleet, "node_id", "local"), "ts": time.time(),
                            "env": await environment(core), "agents": agents(core), "children": {}}
    if fleet is not None:
        for nid, n in fleet.nodes.items():
            child = n.get("presence") or {"node_id": nid, "agents": [], "children": {}}
            snap["children"][nid] = {**child, "status": fleet.status_of(n), "url": n.get("url")}
    return snap


MAX_TREE_DEPTH = 16
MAX_ROWS = 2000


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _s(v: Any, n: int = 200) -> str | None:
    return None if v is None else str(v)[:n]


def _clean_env(env: Any) -> dict[str, Any]:
    env = _d(env)
    nets = []
    for i in env.get("networks") if isinstance(env.get("networks"), list) else []:
        if isinstance(i, dict):
            addrs = i.get("addrs") if isinstance(i.get("addrs"), list) else []
            nets.append({"name": _s(i.get("name"), 64) or "?", "state": _s(i.get("state"), 32),
                         "addrs": [str(a)[:64] for a in addrs[:32]]})
    run = dict(_d(env.get("runner")))
    cont = run.get("container")
    if cont is not None:
        cont = _d(cont)
        run["container"] = {**cont, "addrs": [str(a)[:64] for a in cont.get("addrs", [])[:32]]
                            if isinstance(cont.get("addrs"), list) else []}
    return {**env, "device": _d(env.get("device")), "runner": run, "services": _d(env.get("services")),
            "networks": nets[:32]}


def _clean_agents(v: Any) -> list[dict[str, Any]]:
    out = []
    for a in v if isinstance(v, list) else []:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]:
            out.append({**a, "name": a["name"][:120]})
    return out[:MAX_AGENTS]


def flatten(snap: dict[str, Any], path: tuple[str, ...] = (), _rows: list | None = None) -> list[dict[str, Any]]:
    """Tree -> one row per node with its route from here ('' = this node).

    Children's snapshots come from peers. A peer whose presence nested `children` a few hundred
    levels deep (RecursionError), sent `children` as a list or `agents` without names (KeyError in
    find_agent/render), or used "/" in a child id (a route that could not be resolved) used to break
    /api/fleet/directory, fleet_agents and agent lookup for the whole fleet. Rows are now cleaned,
    depth is capped at MAX_TREE_DEPTH and the tree at MAX_ROWS."""
    rows = [] if _rows is None else _rows
    snap = _d(snap)
    if len(rows) >= MAX_ROWS:
        return rows
    rows.append({"route": "/".join(path), "node_id": _s(snap.get("node_id"), 120),
                 "status": _s(snap.get("status", "online"), 32) or "online", "url": _s(snap.get("url"), 300),
                 "env": _clean_env(snap.get("env")), "agents": _clean_agents(snap.get("agents"))})
    if len(path) >= MAX_TREE_DEPTH:
        return rows
    for nid, child in list(_d(snap.get("children")).items())[:MAX_ROWS]:
        nid = str(nid)[:120]
        if not nid or "/" in nid or nid in path:
            continue
        flatten(child, path + (nid,), rows)
    return rows


async def directory(core: "TrinityCore") -> list[dict[str, Any]]:
    return flatten(await snapshot(core))


def find_agent(rows: list[dict[str, Any]], ref: str) -> tuple[str, str]:
    """Resolve 'Builder-3' or 'router/edge/Builder-3' to (route, agent_name)."""
    if "/" in ref:
        route, name = ref.rsplit("/", 1)
        if not name:  # "edge/" or "/" resolved to an agent named "" (a tell to nobody)
            raise LookupError(f"no agent name in {ref!r}; give route/name")
        if not any(r["route"] == route for r in rows):
            raise LookupError(f"no node at route {route!r}")
        return route, name
    hits = [(r["route"], a["name"]) for r in rows for a in r["agents"] if a["name"] == ref]
    if not hits:
        raise LookupError(f"no agent named {ref!r}; use fleet_agents to list them, or give route/name")
    live = [h for h in hits if any(a["name"] == ref and a["status"] == "working"
                                    for r in rows if r["route"] == h[0] for a in r["agents"])]
    pick = live or hits
    if len(pick) > 1:
        raise LookupError(f"{ref!r} is ambiguous: " + ", ".join(f"{r or '(this node)'}/{n}" for r, n in pick))
    return pick[0]


_CTRL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def _one(v: Any, n: int = 120) -> str:
    """A peer-supplied field as one short line. Agent names, hostnames and service values come
    from peers' presence: a name with a newline forged extra [node] rows in fleet_agents output,
    and a 10 MB `tools` value was rendered whole."""
    text = _CTRL.sub(" ", "" if v is None else str(v))
    return text[:n]


def render(rows: list[dict[str, Any]], only_live: bool = False) -> str:
    out = []
    for r in rows:
        env = _d(r.get("env"))
        dev, run, svc = _d(env.get("device")), _d(env.get("runner")), _d(env.get("services"))
        nets = "; ".join(f"{_one(i.get('name'), 64)} {','.join(_one(x, 64) for x in i.get('addrs', [])[:32])}"
                         for i in (env.get("networks") or [])[:32] if isinstance(i, dict) and i.get("addrs"))
        cont = _d(run.get("container"))
        out.append(f"[{_one(r.get('route'), 300) or '(this node)'}] node={_one(r.get('node_id'))} {_one(r.get('status'), 32)} "
                   f"device={_one(dev.get('hostname'), 64)} ({_one(dev.get('os'), 64)}) runner={_one(run.get('kind'), 32)}"
                   f"{' container=' + _one(cont.get('name'), 64) if cont else ''}")
        if nets:
            caddrs = cont.get("addrs") if isinstance(cont.get("addrs"), list) else []
            out.append(f"   networks: {nets}" + (f"; container {','.join(_one(x, 64) for x in caddrs[:32])}" if caddrs else ""))
        if svc:
            out.append(f"   services: url={_one(svc.get('public_url'), 300)} tools={_one(svc.get('tools'), 32)} "
                       f"mcp={_one(svc.get('mcp_servers'), 32)} model={_one(svc.get('model'), 80)}")
        ags = [a for a in (r.get("agents") or []) if isinstance(a, dict)
               and (a.get("status") == "working" or not only_live)]
        for a in ags:
            out.append(f"   - {_one(a.get('name'))} ({_one(a.get('role'), 60)}) {_one(a.get('status'), 32)} "
                       f"run={_one(a.get('run_id'), 80)}: {_one(a.get('task'), 80)}")
    return "\n".join(out)
