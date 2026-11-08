"""Fleet: link AVRONE servers into a network of nodes that spawn agents on each other.

Every AVRONE server can be a node, a router (a node with children), or both.
  - A node joins a parent with AVRONE_FLEET_PARENT=https://parent:8000 and keeps a heartbeat.
  - Agents get tools to list nodes and spawn squads/chats/deep runs on a remote node.
  - The remote node's agents have the same tools, so they spawn and link further down.
  - Routing: a target like "router1/edge7" is forwarded hop by hop through router1.

Security (required, no token = fleet off):
  AVRONE_FLEET_TOKEN   shared secret. Every fleet request is HMAC-SHA256 signed over
                       method, path, timestamp, nonce and body hash; 60 s window; nonces
                       are remembered so a captured request can't be replayed.
  AVRONE_FLEET_MAX_HOPS  loop/recursion guard across the fleet (default 3)
  AVRONE_FLEET_ALLOW     optional comma list of node ids this server will accept / spawn on
  AVRONE_FLEET_REQUIRE_ALLOW  when truthy (1/true/yes/on), an EMPTY allowlist means refuse-all
                       instead of allow-all (default off, for backward compatibility)

Dispatch gateway: every inbound remote spawn (/api/fleet/run) and message (/api/fleet/message)
goes through FleetManager.dispatch_gateway(), which enforces, in this order:
  1 FLEET_TOKEN present   2 HMAC valid   3 timestamp in window   4 nonce not replayed
  5 sender in FLEET_ALLOW 6 hops within MAX_HOPS   7 per-request capability check
and records every accept as a Codex `fleet.dispatch` event and every refusal as `fleet.refused`.
Outbound spawns (dispatch / remote_spawn / queue_submit) run the matching local checks first.
Memory sync: POST /api/fleet/memory carries an agent's/node's memory entries UP to the parent. It goes
through the same dispatch_gateway (kind "memory", capability "memory:sync", all seven steps); the
receiver files them under "fleet:<sender>/<ns>" where <sender> is the HMAC-signed `from` node id, so
a push can never overwrite the receiver's own (local) namespaces. Accepts are audited as
`fleet.memory.merge`. Caps: MEMORY_SYNC_MAX_ENTRIES per push plus the store's per-namespace caps.
Agent migration (edgepack): POST /api/fleet/agentpack carries one agent's HOME (its dir + memory
namespace, an avrone.agentpack bundle) to a peer. Same dispatch_gateway (kind "agentpack", capability
"agent:migrate"), plus: the sender MUST be listed in a non-empty AVRONE_FLEET_ALLOW (an open allowlist
refuses), the privileged flag is always refused, and the receiver must itself run AVRONE_AGENT_MIGRATE=on.
The pack's origin must equal the signed sender; import verifies hashes/paths/quota and never overwrites
another agent's home. FleetManager.migrate_agent() sends one; thermal_migrate() moves idle agents off a hot
node (governor temp over AVRONE_THERMAL_MAX_C). Audited as agent.migrate / agent.migrate_refused /
agent.migrate_failed; on any failure the agent stays put.
Privileged ("root-enabled") tier -- OFF by default, deny by default:
  AVRONE_FLEET_PRIVILEGED      must be exactly "allow" (case-insensitive) to enable; anything else = deny
  AVRONE_PRIVILEGED_NODES      comma list of SENDER node ids this node grants elevated capabilities to
                               (default empty = nobody, even with the toggle on)
  AVRONE_FLEET_PRIVILEGED_MAX_HOPS  privileged requests are accepted only within this many hops
                               (default 1 = direct parent->child only; never above AVRONE_FLEET_MAX_HOPS)
A request asks for an elevated capability (priv:shell / priv:admin) with "privileged": true AND that
capability INSIDE the HMAC-signed JSON body, so neither can be added or flipped in transit; an
x-avrone-privileged header is refused outright. The receiving node accepts it only when its toggle is
on AND the sender is in its AVRONE_PRIVILEGED_NODES (and in FLEET_ALLOW, and within the hop bounds);
otherwise it refuses with a `fleet.privileged.refused` audit event. Accepted privileged runs are
audited as `fleet.privileged.accept` and run with core.fleet_privileged set to the granted capability
AND the run-scoped tools_extra.PRIV_GRANT context set to it; the priv_shell / priv_admin tools require
both to agree (so a concurrent unrelated run never inherits it). The originating node also needs its
own toggle on to send one.
The sender id is asserted inside the HMAC-signed body: it is tamper-proof in transit, but any
holder of the shared token can claim any id, so the allowlist limits blast radius, not trust.
Other env:
  AVRONE_NODE_ID        stable id for this server (default: hostname-derived, persisted)
  AVRONE_PUBLIC_URL     URL others use to reach this node (sent when registering)
  AVRONE_FLEET_PARENT   parent/router to join
  AVRONE_FLEET_HEARTBEAT seconds between heartbeats (default 15)
Governor-aware routing (edgeroute):
  Every registration/heartbeat carries a compact `health` block from the sender's governor
  (load ratio, mem %, disk free %, cpu temp, headroom, throttle state, active agents). The
  receiver sanitizes it and stores it on the node record; it is only trusted while the node is
  "online" (same ageing as status). select_nodes(k, need) ranks online, allowlisted children by
  health_score() (cool/idle first, deterministic tiebreak) and drops hot/throttled ones;
  pick("auto") uses it.
  AVRONE_FLEET_OFFLOAD  off|spill|spread (default off). When the local governor caps a squad below
                       its size, the overflow workers are sent (signed dispatch, mode chat, the worker's
                       focus) to the best healthy child (spill) or round-robin across the healthy children
                       (spread). A remote failure/timeout (AVRONE_FLEET_OFFLOAD_TIMEOUT, default 300 s) runs
                       that worker locally instead. Privileged runs are never offloaded; hops, allowlist and
                       the outbound gateway checks all apply. Audited as fleet.offload / fleet.offload_fallback.
netsquad (squad mode dispatched into the node network):
  AVRONE_SQUAD_DISPATCH  local|network|hybrid (default local = unchanged). network places EVERY squad
                       subtask on a node from select_nodes (healthy, allowlisted, advertising capability
                       squad:net), round-robin by score; hybrid keeps the governor's local limit here and
                       places the rest. Each placement runs on that node as a sub-squad of the same subtask
                       (AVRONE_NET_SUBSQUAD, default 2, max 4; AVRONE_NET_WORKER_MODE=chat runs one agent).
                       The receiving node may place further down its own children while hops < MAX_HOPS,
                       never back to a node already on the route (fleet.cycle_refused). Caps:
                       AVRONE_NET_MAX_NODES (8) per squad, AVRONE_NET_MAX_REMOTE_AGENTS (32) per run (the
                       budget rides down the tree). Remote error/timeout/empty answer runs that subtask
                       locally (fleet.netsquad_fallback). Privileged runs never leave the node; the
                       outbound gateway checks and allowlist always apply. Placements are audited as
                       fleet.netsquad_place and counted per node. An optional core.bridge (export(since),
                       merge_remote(posts, origin_node)) relays bridge posts both ways (50 posts / 64 KB).
Run nodes over HTTPS or a private mesh (Tailscale/WireGuard); the HMAC protects
integrity and auth, not confidentiality.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .tools import Tool
from .tools_extra import priv_grant
from .checkpoint import CheckpointError, check_compatible, fleet_key, managed_path, stamp_owner
from .checkpoint import claim as ckpt_claim
from .checkpoint import sanitize_id as sanitize_ckpt_id
from .trinity import clamp_deep_rounds

if TYPE_CHECKING:
    from .trinity import AgentContext, TrinityCore

log = logging.getLogger("avrone.fleet")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
WINDOW_S = 60
# Signed-header shape (checked before the HMAC is computed, so junk never reaches the cache):
# ts is 1-12 ASCII digits (int() alone also took "+5", " 5", "1_0" and non-ASCII digits like "٥");
# a nonce is 1-128 chars of [A-Za-z0-9_-] (an unbounded nonce was stored verbatim in the replay cache).
TS_RE = re.compile(r"^[0-9]{1,12}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Replay-cache bound: past this many live nonces new requests are refused (fail closed) rather than
# evicting a live nonce, which would re-open replay of that request.
NONCE_CACHE_MAX = 100_000
# Past this many live nonces each accepted request appends one line to fleet_nonces.json.journal
# instead of rewriting the whole snapshot, and expired nonces are pruned at most once a second.
# Before (round 68): every request pruned the dict and rewrote the JSON on the event loop, O(n):
# 8 ms per request at 10k live nonces, 85 ms near the cap, so the gateway saturated at ~100 rps.
NONCE_JOURNAL_MIN = 1024
# Peer registry bounds: a token holder (or allow-all fleet) could register unlimited node ids, each
# rewriting fleet.json (quadratic), with megabytes of capabilities/presence per record.
DEFAULT_MAX_NODES, HARD_MAX_NODES = 256, 4096
MAX_NODE_RECORD_BYTES = 64 * 1024
# A heartbeat refresh (known node, same url) rewrites fleet.json at most this often; a new node, a
# url change and forget() still save at once (membership is durable), and stop() flushes. A peer
# resends its refresh every heartbeat, so a crash inside the window loses nothing. Before (round 69) every heartbeat rewrote the
# whole registry on the event loop: 145 ms per heartbeat at 256 peers x 60 KB, so 256 peers on the
# default 15 s heartbeat kept the loop busy ~2.5 s of every second.
FLEET_SAVE_DEBOUNCE_S = 5.0


def max_nodes() -> int:
    try:
        n = int(os.environ.get("AVRONE_FLEET_MAX_NODES", "").strip() or DEFAULT_MAX_NODES)
    except ValueError:
        n = DEFAULT_MAX_NODES
    return max(1, min(HARD_MAX_NODES, n))

# Tests may set this to an httpx transport (e.g. ASGITransport routing to in-process apps).
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


# Cloud instance-metadata endpoints: never a fleet peer. A token holder registering one of these as
# its url made this node POST to it and echo the reply (up to 400 chars) back in dispatch errors.
_METADATA_HOSTS = {"169.254.169.254", "fd00:ec2::254", "metadata.google.internal", "metadata",
                   "100.100.100.200", "metadata.azure.com"}


def check_node_url(url: Any) -> str:
    """A peer url: http(s), a host, optional port and path; no credentials, query, fragment, control
    chars or whitespace; not link-local / a cloud metadata endpoint. Private and loopback hosts stay
    allowed (LAN and single-host fleets are normal). Returns the url without a trailing slash."""
    import ipaddress
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not url or len(url) > 512:
        raise ValueError("url must be an http(s) url of 1-512 chars")
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or not url.isascii():
        raise ValueError("url has whitespace, control or non-ASCII characters")
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError("url is malformed") from None
    if u.scheme not in ("http", "https"):
        raise ValueError("url must be http(s)")
    if not u.hostname:
        raise ValueError("url has no host")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise ValueError("url must not carry credentials")
    if u.query or u.fragment or "?" in url or "#" in url:
        raise ValueError("url must not carry a query or fragment")
    if port == 0:
        raise ValueError("url port must be 1-65535")
    host = u.hostname.rstrip(".").lower()
    if host in _METADATA_HOSTS:
        raise ValueError("url points at a cloud metadata endpoint")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or str(ip) in _METADATA_HOSTS:
            raise ValueError("url points at a link-local, multicast or unspecified address")
    return url.rstrip("/")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


TRUTHY = {"1", "true", "yes", "on"}
# capability a request may ask for, per endpoint kind
CAPABILITIES: dict[str, set[str]] = {"run": {"spawn:squad", "spawn:chat", "spawn:deep"}, "message": {"message"}}
SPAWN_MODES = ("squad", "chat", "deep")
# elevated capabilities: only on the run endpoint, only through the privileged gate
PRIVILEGED_CAPABILITIES = {"priv:shell", "priv:admin"}
PRIVILEGED_HEADER = "x-avrone-privileged"
# non-spawn capabilities, kept apart from the (minimal) spawn/message table above
SYNC_CAPABILITIES: dict[str, set[str]] = {"memory": {"memory:sync"}, "agentpack": {"agent:migrate"}, "trial": {"trials:ingest"}}
# netsquad: a squad subtask placed on this node (runs as a sub-squad/chat here, may recurse further down)
NET_CAPABILITIES: dict[str, set[str]] = {"run": {"squad:net"}}
NETSQUAD_CAP = "squad:net"
MEMORY_PATH = "/api/fleet/memory"
MEMORY_SYNC_MAX_ENTRIES = 200
AGENTPACK_PATH = "/api/fleet/agentpack"
DEFAULT_MIGRATE_INTERVAL_S, MIN_MIGRATE_INTERVAL_S = 60.0, 10.0


def migrate_enabled() -> bool:
    """AVRONE_AGENT_MIGRATE=on (default off): send/accept agent homes between nodes."""
    return _env("AVRONE_AGENT_MIGRATE").lower() in TRUTHY


def migrate_interval() -> float:
    try:
        v = float(_env("AVRONE_AGENT_MIGRATE_INTERVAL") or DEFAULT_MIGRATE_INTERVAL_S)
    except ValueError:
        v = DEFAULT_MIGRATE_INTERVAL_S
    return max(MIN_MIGRATE_INTERVAL_S, v if v == v else DEFAULT_MIGRATE_INTERVAL_S)


# ------------------------------------------------------------ health (edgeroute)
HEALTH_STATES = ("ok", "throttled", "hot")
EXCLUDED_STATES = {"throttled", "hot"}
HOT_HEADROOM = 0.25  # min governor factor at/below which a node counts as hot
_HEALTH_NUM = {"load": (0.0, 1000.0), "mem_pct": (0.0, 100.0), "disk_free_pct": (0.0, 100.0),
               "temp_c": (-40.0, 150.0), "headroom": (0.0, 1.0)}


OFFLOAD_MODES = ("off", "spill", "spread")


def offload_mode() -> str:
    v = _env("AVRONE_FLEET_OFFLOAD", "off").lower()
    return v if v in OFFLOAD_MODES else "off"


def offload_timeout() -> float:
    try:
        t = float(_env("AVRONE_FLEET_OFFLOAD_TIMEOUT", "300") or 300)
    except ValueError:
        t = 300.0
    return max(5.0, min(t, 900.0))


# ------------------------------------------------------------ netsquad (squad -> node network)
SQUAD_DISPATCH_MODES = ("local", "network", "hybrid")
NET_WORKER_MODES = ("squad", "chat")
NET_SUBSQUAD_MAX = 4
BRIDGE_MAX_POSTS, BRIDGE_MAX_BYTES = 50, 64 * 1024


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(_env(name, str(default)) or default)
    except ValueError:
        v = default
    return max(lo, min(v, hi))


def squad_dispatch(override: str | None = None) -> str:
    """AVRONE_SQUAD_DISPATCH=local|network|hybrid (default local); a valid per-request override wins."""
    if override in SQUAD_DISPATCH_MODES:
        return str(override)
    v = _env("AVRONE_SQUAD_DISPATCH", "local").lower()
    return v if v in SQUAD_DISPATCH_MODES else "local"


def net_worker_mode() -> str:
    v = _env("AVRONE_NET_WORKER_MODE", "squad").lower()
    return v if v in NET_WORKER_MODES else "squad"


def net_subsquad() -> int:
    return _env_int("AVRONE_NET_SUBSQUAD", 2, 1, NET_SUBSQUAD_MAX)


def net_max_nodes() -> int:
    return _env_int("AVRONE_NET_MAX_NODES", 8, 1, 64)


def net_max_remote_agents() -> int:
    return _env_int("AVRONE_NET_MAX_REMOTE_AGENTS", 32, 1, 1024)


def cap_bridge_posts(posts: Any) -> list[dict[str, Any]]:
    """Newest-last list of JSON-able dict posts, at most BRIDGE_MAX_POSTS and BRIDGE_MAX_BYTES."""
    if not isinstance(posts, (list, tuple)):
        return []
    out = []
    for p in list(posts)[-BRIDGE_MAX_POSTS:]:
        if not isinstance(p, dict):
            continue
        try:
            json.dumps(p)
        except (TypeError, ValueError):
            continue
        out.append(p)
    while out and len(json.dumps(out).encode()) > BRIDGE_MAX_BYTES:
        out.pop(0)  # drop the oldest first
    return out


def bridge_of(core: Any, run_id: Any = None) -> Any:
    """The run's Bridge (avrone.bridge.for_core) when run_id is given and the bridge module is
    available, else core.bridge when it offers export + merge_remote, else None (optional)."""
    if run_id:
        try:
            from . import bridge as bridgemod
            return bridgemod.for_core(core, str(run_id))
        except Exception as e:  # noqa: BLE001  -- the relay is best-effort
            log.debug("run bridge unavailable: %s", e)
    b = getattr(core, "bridge", None)
    if b is None or not callable(getattr(b, "export", None)) or not callable(getattr(b, "merge_remote", None)):
        return None
    return b


def bridge_export(core: Any, since: Any = None, run_id: Any = None,
                  only_origin: str | None = None) -> list[dict[str, Any]]:
    b = bridge_of(core, run_id)
    if b is None:
        return []
    try:
        posts = b.export(since)
        if only_origin is not None:  # send back only what this node's agents wrote
            posts = [p for p in posts if isinstance(p, dict) and p.get("origin_node") == only_origin]
        return cap_bridge_posts(posts)
    except Exception as e:  # noqa: BLE001  -- the relay is best-effort
        log.debug("bridge export failed: %s", e)
        return []


def bridge_merge(core: Any, posts: Any, origin: str, run_id: Any = None) -> int:
    b = bridge_of(core, run_id)
    posts = cap_bridge_posts(posts)
    if b is None or not posts:
        return 0
    try:
        b.merge_remote(posts, origin)
        return len(posts)
    except Exception as e:  # noqa: BLE001
        log.debug("bridge merge from %s failed: %s", origin, e)
        return 0


def _finite(v: Any, default: float) -> float:
    """A float from a peer- or file-supplied value; `default` for junk, bools, NaN and inf."""
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return f if math.isfinite(f) else default


def node_load(n: Any) -> int:
    """In-flight load of a node record, always an int 0..10000. The peer sends it in its heartbeat:
    a string there made select_nodes' sort raise TypeError (every auto dispatch failed) and a
    negative one made that peer look idle forever."""
    if not isinstance(n, dict):
        return 0
    return int(max(0.0, min(10_000.0, _finite(n.get("load", 0), 0.0))))


def heartbeat_env() -> float:
    """AVRONE_FLEET_HEARTBEAT in seconds, clamped 1..3600. "abc" used to crash FleetManager at boot,
    and 0 made every peer read as offline forever."""
    v = _finite(_env("AVRONE_FLEET_HEARTBEAT", "15") or 15, 15.0)
    return 15.0 if v <= 0 else max(1.0, min(3600.0, v))


FAILOVER_MAX = 3  # auto dispatch tries at most this many peers when the first can't be reached


def _unreachable(e: BaseException) -> bool:
    """True only for errors raised before the request reached the peer, so a retry elsewhere can't
    run the job twice. A read timeout or a 5xx may mean the peer already started it: no failover."""
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def local_health(core: Any) -> dict[str, Any] | None:
    """Compact health block from this node's governor, for heartbeats. Never raises; None if unknown."""
    from .governor import Governor
    gov = getattr(core, "governor", None)
    if not isinstance(gov, Governor):
        return None
    try:
        h = gov.read()
        fs = gov.factors(h)
        headroom = min(fs.values()) if fs else 1.0
        hot = headroom <= HOT_HEADROOM or (h.temp_c is not None and h.temp_c >= gov.thermal_max_c)
        state = "hot" if hot else ("throttled" if headroom < 1.0 else "ok")
        active = sum(1 for a in dict(getattr(core, "active", {}) or {}).values()
                     if isinstance(a, dict) and a.get("status") == "working")
        out: dict[str, Any] = {"state": state, "headroom": round(headroom, 3), "active_agents": active}
        for k in ("load", "mem_pct", "disk_free_pct", "temp_c"):
            v = getattr(h, k)
            if v is not None:
                out[k] = round(float(v), 2)
        return out
    except Exception as e:  # noqa: BLE001  -- health is advisory
        log.debug("local health unavailable: %s", e)
        return None


def sanitize_health(raw: Any) -> dict[str, Any] | None:
    """Keep only well-typed, range-clamped fields of a peer's health block (it is peer-asserted)."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for k, (lo, hi) in _HEALTH_NUM.items():
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
            continue
        try:
            f = float(v)
        except OverflowError:  # a peer's heartbeat with a 400-digit int: raised from sanitize_health
            f = float("inf") if v > 0 else float("-inf")
        out[k] = max(lo, min(hi, f))
    st = raw.get("state")
    if st in HEALTH_STATES:
        out["state"] = st
    aa = raw.get("active_agents")
    if isinstance(aa, int) and not isinstance(aa, bool):
        out["active_agents"] = max(0, min(aa, 10_000))
    return out or None


def health_score(n: dict[str, Any]) -> float:
    """0..100, higher = better target. Unknown health is neutral, never best.

      60 * headroom (governor min factor; unknown 0.5)
    + 20 * (1 - min(load ratio, 2) / 2)            (unknown load ratio = 1.0)
    + 20 * (1 - min(busy, 10) / 10)  where busy = max(in-flight load, active agents)
    """
    h = n.get("health") if isinstance(n.get("health"), dict) else {}
    # every field through _finite and clamped: records come from fleet.json too (older versions, a
    # hand edit), where "abc" raised ValueError out of select_nodes (every auto dispatch failed) and
    # health_table, NaN poisoned the sort and headroom 1e308 scored inf and won every pick (round 75)
    headroom = min(1.0, max(0.0, _finite(h.get("headroom", 0.5), 0.5)))
    ratio = min(2.0, max(0.0, _finite(h.get("load", 1.0), 1.0)))
    busy = max(_finite(n.get("load", 0) or 0, 10.0), _finite(h.get("active_agents", 0) or 0, 10.0))
    return round(60 * headroom + 20 * (1 - ratio / 2) + 20 * (1 - min(max(busy, 0.0), 10.0) / 10), 3)


NONCE_EXP_SLACK_S = 5.0
NONCE_JOURNAL_MAX_BYTES = 64 * 1024 * 1024


def nonce_expiry(v: Any, now: float) -> float | None:
    """A saved nonce expiry, or None to drop it. The newest a real one can be is
    max(ts + WINDOW_S, now + 2 * WINDOW_S) with ts inside the window, so anything later is clamped
    to now + 2 * WINDOW_S (+ slack). Saved files were trusted (round 77): `Infinity` entries never
    expired, so a file holding NONCE_CACHE_MAX of them refused every fleet request forever with
    "replay cache full", and a 400-digit integer raised OverflowError out of FleetManager() and
    left the node with no fleet at all."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        f = float("inf") if v > 0 else float("-inf")
    if f != f or f < now:
        return None
    return min(f, now + 2 * WINDOW_S + NONCE_EXP_SLACK_S)


def clean_saved_node(key: Any, v: Any, self_id: str) -> dict[str, Any] | None:
    """A node record from fleet.json, rebuilt with the checks register() applies, or None to drop it.

    The file outlives versions and can be edited by hand, but its records were loaded as is
    (round 76): a url at a metadata endpoint or with credentials was dispatched to without
    check_node_url, a key like "../x" or this node's own id became a target, a record without
    node_id raised KeyError in pick(), and capabilities/children/health of the wrong type or
    unclamped reached node_has, fleet_nodes and health_score."""
    if not isinstance(key, str) or not ID_RE.match(key) or key == self_id or not isinstance(v, dict):
        return None
    try:
        url = check_node_url(v.get("url"))
    except ValueError:
        return None
    caps = v.get("capabilities")
    kids = v.get("children")
    pres = v.get("presence")
    return {"node_id": key, "url": url, "last_seen": _finite(v.get("last_seen"), 0.0),
            "capabilities": caps if isinstance(caps, dict) else {},
            "children": [c for c in kids if isinstance(c, str)][:1000] if isinstance(kids, list) else [],
            "load": node_load(v), "presence": pres if isinstance(pres, dict) else None,
            "health": sanitize_health(v.get("health"))}


def node_has(n: dict[str, Any], need: str | None) -> bool:
    """Capability match: spawn:* (every node), a tool name, the runner kind or a truthy capability key."""
    if not need or need.startswith("spawn:"):
        return True
    caps = n.get("capabilities") if isinstance(n.get("capabilities"), dict) else {}
    tools = caps.get("tools") if isinstance(caps.get("tools"), list) else []
    return need in tools or caps.get("runner") == need or bool(caps.get(need)) and need not in ("tools", "runner")


def kind_capabilities(kind: str) -> set[str]:
    return set(CAPABILITIES.get(kind) or SYNC_CAPABILITIES.get(kind) or set()) | NET_CAPABILITIES.get(kind, set())



FLEET_JSON_MAX_BYTES = 64 * 1024 * 1024
NONCE_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024


def _load_nonce_snapshot(p: Path) -> Any:
    """fleet_nonces.json parsed, read with O_NOFOLLOW|O_NONBLOCK (round 85: a FIFO there blocked
    every verify() that reloaded it). A symlink/special file or one over 64 MB raises OSError, which
    the callers treat as unreadable (journal only; our next save rewrites it)."""
    from .safeio import read_capped
    raw = read_capped(p, NONCE_SNAPSHOT_MAX_BYTES)
    if raw is None:
        raise OSError("fleet_nonces.json is not a regular file")
    return json.loads(raw.decode("utf-8"))

class GatewayRefused(PermissionError):
    """A fleet request refused by the dispatch gateway. `code` is a stable machine-readable reason;
    `auth` is True for authentication failures (HTTP 401) and False for policy refusals (HTTP 403)."""

    AUTH_CODES = {"no_token", "bad_timestamp", "bad_nonce", "bad_signature", "stale", "replay",
                  "privileged_unsigned"}

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    @property
    def auth(self) -> bool:
        return self.code in self.AUTH_CODES


def sender_node(frm: Any) -> str:
    """Node id part of a `from` field ("node" or "node/agent")."""
    return str(frm or "").split("/", 1)[0].strip()


def _parse_hops(raw: Any) -> int | None:
    """A hop count from a signed payload, or None when it is not a plain integer.
    Refuses bools, non-integral/non-finite floats (int(Infinity) raised OverflowError -> 500, and
    int(1.9) silently truncated) and strings that are not ASCII digits (int() took "٢", " 2")."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if math.isfinite(raw) and raw.is_integer() else None
    if isinstance(raw, str) and re.fullmatch(r"-?[0-9]{1,6}", raw):
        return int(raw)
    return None


MAX_STRICT_JSON_DEPTH = 64


def strict_json_loads(raw: bytes) -> Any:
    """Reject non-UTF-8, duplicate keys, non-finite numbers, and excessive nesting."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("JSON body must be UTF-8") from None
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > MAX_STRICT_JSON_DEPTH:
                raise ValueError("JSON nesting depth exceeded")
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced JSON structure")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(text or "{}", object_pairs_hook=pairs, parse_constant=reject_constant)

def request_capability(kind: str, payload: dict[str, Any]) -> str:
    if payload.get("capability") not in (None, ""):
        return str(payload["capability"])
    if kind == "run":
        return f"spawn:{payload.get('mode', 'squad')}"
    return kind


SIG_RE = re.compile(r"^[0-9a-f]{64}$")
WEAK_TOKEN_CHARS = 16
FLEET_PATH_MARK = "/api/fleet/"


def canonical_path(path: Any) -> str:
    """The part of a request path that is signed: from "/api/fleet/" on. Behind a reverse proxy
    or tunnel that mounts AVRONE under a prefix (/avrone/api/fleet/run), the receiver saw the
    prefixed path while the sender signed the bare one, so every request failed as "bad
    signature"; a peer url with a path (http://host/avrone) did the same in the other direction.
    The endpoint itself stays bound into the signature."""
    p = str(path or "")
    i = p.find(FLEET_PATH_MARK)
    return p[i:] if i >= 0 else p


def clean_token(raw: Any) -> str:
    """AVRONE_FLEET_TOKEN as both sides should see it: whitespace trimmed and one pair of matching
    surrounding quotes removed. A .env read by docker compose keeps "quotes" that a shell export
    drops, so two nodes with the same line in their .env computed different signatures."""
    t = str(raw or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        t = t[1:-1].strip()
    return t


def skew_text(ts_i: int, now: float) -> str:
    d = int(round(ts_i - now))
    side = "ahead of" if d > 0 else "behind"
    return f"the sender's clock is {abs(d)}s {side} this node's (window ±{WINDOW_S}s); sync clocks (NTP)"


def _mac(token: str, method: str, path: str, ts: str, nonce: str, body_hash: str) -> str:
    msg = "\n".join([method.upper(), canonical_path(path), ts, nonce, body_hash]).encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def sign(token: str, method: str, path: str, body: bytes, ts: str | None = None, nonce: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    nonce = nonce or secrets.token_hex(12)
    sig = _mac(token, method, path, ts, nonce, hashlib.sha256(body).hexdigest())
    return {"x-avrone-ts": ts, "x-avrone-nonce": nonce, "x-avrone-sig": sig}


# Refusals that happen before the HMAC is checked (no token, malformed headers, bad signature) can
# come from anyone who can reach the port. Each one used to write a full audit event to the codex
# log (fleet.jsonl has no rotation) and fan out to every dashboard: a flood of 1M junk requests left
# ~230 MB of identical "bad signature" lines. Above this many per minute they are counted instead,
# and one fleet.refused.suppressed event per window reports how many and which codes.
UNAUTH_CODES = frozenset({"no_token", "bad_timestamp", "bad_nonce", "bad_signature"})
UNAUTH_AUDIT_WINDOW_S = 60.0


def unauth_audit_per_min() -> int:
    return _env_int("AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN", 60, 0, 1_000_000)


_LAZY_LOCK_GUARD = threading.Lock()


class FleetManager:
    def __init__(self, core: "TrinityCore", data_dir: Path):
        self.core = core
        self.token = clean_token(_env("AVRONE_FLEET_TOKEN"))
        # Rotation: during a token change, peers still signing with the old token keep working
        # while AVRONE_FLEET_TOKEN_PREV holds it. Outbound requests always sign with the new one.
        prev = clean_token(_env("AVRONE_FLEET_TOKEN_PREV"))
        self.prev_token = prev if prev and prev != self.token else ""
        if self.token and len(self.token) < WEAK_TOKEN_CHARS:
            log.warning("AVRONE_FLEET_TOKEN is %d chars; use at least %d random chars "
                        "(python -c 'import secrets; print(secrets.token_hex(32))')",
                        len(self.token), WEAK_TOKEN_CHARS)
        self.max_hops = int(_env("AVRONE_FLEET_MAX_HOPS", "3") or 3)
        self.allow = {a.strip() for a in _env("AVRONE_FLEET_ALLOW").split(",") if a.strip()}
        self.require_allow = _env("AVRONE_FLEET_REQUIRE_ALLOW").lower() in TRUTHY
        self.privileged = _env("AVRONE_FLEET_PRIVILEGED").lower() == "allow"  # deny unless exactly "allow"
        self.privileged_nodes = {a.strip() for a in _env("AVRONE_PRIVILEGED_NODES").split(",") if a.strip()}
        try:
            ph = int(_env("AVRONE_FLEET_PRIVILEGED_MAX_HOPS", "1") or 1)
        except ValueError:
            ph = 1
        self.privileged_max_hops = max(1, min(ph, self.max_hops))
        self.parent = _env("AVRONE_FLEET_PARENT").rstrip("/")
        self.public_url = _env("AVRONE_PUBLIC_URL").rstrip("/")
        self.heartbeat_s = heartbeat_env()
        self.data_dir = Path(data_dir)
        self.path = Path(data_dir) / "fleet.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        saved = self._load_saved()
        nid = _env("AVRONE_NODE_ID") or (saved.get("node_id") if isinstance(saved.get("node_id"), str) else None)
        if not nid:
            base = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower()).strip("-")[:40]
            nid = f"{base or 'node'}-{secrets.token_hex(3)}"
        if not ID_RE.match(nid):
            raise ValueError(f"AVRONE_NODE_ID {nid!r} must match {ID_RE.pattern}")
        self.node_id = nid
        raw_nodes = saved.get("nodes")
        self.nodes: dict[str, dict[str, Any]] = {}
        dropped = 0
        for k, v in (raw_nodes.items() if isinstance(raw_nodes, dict) else ()):
            rec = clean_saved_node(k, v, nid)
            if rec is None:
                dropped += 1
            else:
                self.nodes[k] = rec
        if dropped:
            log.warning("fleet.json: dropped %d unusable node record(s) (bad id or url)", dropped)
        # The replay cache survives a restart: a request captured within its window must not run again
        # just because the node rebooted. If the saved cache cannot be read, nothing signed before this
        # boot is accepted (boot guard) - the same guarantee without the cache.
        self._nonce_path = self.data_dir / "fleet_nonces.json"
        self._boot_guard: float | None = None
        self._nonces: dict[str, float] = self._load_nonces()
        self._task: asyncio.Task | None = None
        self._thermal_task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self.queue = None  # JobQueue, attached by start_queue()
        self.worker = None
        self.netsquad_counts: dict[str, int] = {}  # node id -> netsquad placements (telemetry)
        self._migrating: set[str] = set()  # agent ids with an outbound migration in flight
        # Guards every check-then-act on shared gateway state (nonce cache, node registry, the
        # in-flight migration set, fleet.json). verify() is sync and may run on several threads
        # (sync handlers, to_thread callers): its "nonce seen?" check and the insert, and the
        # rebinding prune, used to interleave so a replayed request was accepted twice.
        self.__dict__.setdefault("_state_lock", threading.RLock())
        self._save()

    # ------------------------------------------------------------ state
    @property
    def _lock(self) -> threading.RLock:
        """The state lock (created on first use for instances built without __init__)."""
        lk = self.__dict__.get("_state_lock")
        if lk is None:
            with _LAZY_LOCK_GUARD:
                lk = self.__dict__.setdefault("_state_lock", threading.RLock())
        return lk

    @contextlib.contextmanager
    def _run_scope(self, hops: int, priv: str, netsquad: "tuple[list[str], int | None] | None" = None):
        """Register one inbound run as live on the core for its duration and publish the core-wide
        view of ALL live runs (one critical section per enter/exit):

          core.fleet_hops        = the DEEPEST live run's hop count (never lower than any live run's),
                                   back to its pre-fleet value once no run is live;
          core.fleet_privileged  = the granted capability only while exactly one distinct privileged
                                   capability is live; "" otherwise (none live, or priv:shell and
                                   priv:admin overlapping = fail closed), and "" once none is live;
          core.netsquad_path     = ordered union of every live netsquad run's route (cycle checks see
                                   at least their own route; a too-long union is refused by the next
                                   node = runs locally), core.netsquad_budget = the smallest live
                                   budget; both back to their pre-fleet values once no netsquad run is live.

        The old save/restore-per-run (prev = core.x; core.x = mine; finally core.x = prev) broke when
        overlapping runs ended in crossed order (A starts, B starts, A ends, B ends): B restored A's
        value, so a privileged run's core flag outlived every run, an unprivileged sibling wiped a live
        privileged run's grant, and a shallow run lowered a deep run's hop count (hop-bound bypass)."""
        c = self.core
        entry = [int(hops), str(priv or ""), netsquad]
        with self._lock:
            live = self.__dict__.setdefault("_live_runs", [])
            if not live:
                self._idle_hops = getattr(c, "fleet_hops", 0)
            if netsquad is not None and not any(e[2] is not None for e in live):
                self._idle_netsquad = (getattr(c, "netsquad_path", None), getattr(c, "netsquad_budget", None))
            live.append(entry)
            self._publish_run_state()
        try:
            yield
        finally:
            with self._lock:
                live = self.__dict__.setdefault("_live_runs", [])
                for i, e in enumerate(live):
                    if e is entry:
                        del live[i]
                        break
                self._publish_run_state()

    def _publish_run_state(self) -> None:
        c = self.core
        live = self.__dict__.get("_live_runs") or []
        nets = [e[2] for e in live if e[2] is not None]
        if nets:
            route: list[str] = []
            for p, _ in nets:
                route += [x for x in p if x not in route]
            budgets = [b for _, b in nets if isinstance(b, int) and not isinstance(b, bool)]
            c.netsquad_path, c.netsquad_budget = route, (min(budgets) if budgets else None)
        elif "_idle_netsquad" in self.__dict__:
            c.netsquad_path, c.netsquad_budget = self.__dict__.pop("_idle_netsquad")
        if not live:
            c.fleet_hops = self.__dict__.get("_idle_hops", 0)
            c.fleet_privileged = ""
            return
        c.fleet_hops = max(e[0] for e in live)
        caps = {e[1] for e in live if e[1]}
        c.fleet_privileged = next(iter(caps)) if len(caps) == 1 else ""

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _load_saved(self) -> dict[str, Any]:
        """fleet.json, or {} when missing. A corrupt file (disk error, manual edit) must not stop the
        node from booting: it is moved aside as fleet.json.corrupt-<ts> and the node starts fresh."""
        if not self.path.exists():
            return {}
        from .safeio import read_capped
        try:  # round 85: a FIFO here blocked FleetManager() (node boot) forever in read_text()
            raw = read_capped(self.path, FLEET_JSON_MAX_BYTES)
            if raw is None:  # a symlink or special file: never read, never renamed
                log.warning("fleet.json is not a regular file; starting with an empty registry")
                return {}
            saved = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            saved = None
        if isinstance(saved, dict):
            return saved
        aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(self.path, aside)
        except OSError:
            pass
        log.warning("fleet.json was unreadable; moved to %s and starting with an empty registry", aside.name)
        return {}

    def _save(self) -> None:
        """Write the registry. Group commit: concurrent savers (a join storm of to_thread
        registrations) wait for one leader, which keeps writing the newest snapshot until it has
        covered every request; a saver returns only once a snapshot that includes its change is
        on disk. Before (round 70) every registration serialized and rewrote the whole growing
        file itself (indent=2, 3x slower than compact): 256 joins of 60 KB records took ~18 s.
        temp file + os.replace: a concurrent or interrupted save never leaves a torn fleet.json."""
        lock = self._lock
        cond = self.__dict__.get("_save_cond")
        if cond is None:
            cond = self.__dict__.setdefault("_save_cond", threading.Condition(lock))
        with lock:
            self._save_ver = v = getattr(self, "_save_ver", 0) + 1
            while getattr(self, "_save_busy", False) and getattr(self, "_saved_ver", 0) < v:
                cond.wait()  # releases the (re-entrant) lock fully while the leader writes
            if getattr(self, "_saved_ver", 0) >= v:
                return
            self._save_busy = True
        try:
            while True:
                with lock:
                    target = self._save_ver
                    text = json.dumps({"node_id": self.node_id, "nodes": self.nodes}, separators=(",", ":"))
                self._write_registry(text)
                with lock:
                    self._saved_ver = target
                    self._saved_at, self._dirty = time.monotonic(), False
                    cond.notify_all()
                    if self._save_ver == target:
                        return
        finally:
            with lock:
                self._save_busy = False
                cond.notify_all()  # on failure a waiter becomes the next leader and retries

    def _write_registry(self, text: str) -> None:
        from .safeio import atomic_write
        atomic_write(self.path, text)

    def flush(self) -> None:
        """Write heartbeat refreshes still held back by FLEET_SAVE_DEBOUNCE_S."""
        if getattr(self, "_dirty", False):
            self._save()

    def status_of(self, n: dict[str, Any]) -> str:
        age = time.time() - _finite(n.get("last_seen", 0) if isinstance(n, dict) else 0, 0.0)
        if age < -3 * self.heartbeat_s:
            # last_seen far in the future (the clock stepped back, or a hand-edited fleet.json): it
            # used to read as online until the clock caught up, even for a dead peer. A live peer
            # re-registers within one heartbeat and is online again.
            return "offline"
        if age < 3 * self.heartbeat_s:
            return "online"
        if age < 10 * self.heartbeat_s:
            return "degraded"
        return "offline"

    def list_nodes(self) -> list[dict[str, Any]]:
        out = []
        for n in self.nodes.values():
            st = self.status_of(n)
            row = {**{k: v for k, v in n.items()}, "status": st}
            if st != "online" and row.get("health"):
                row["health"] = {**row["health"], "stale": True}  # aged with the status
            out.append(row)
        return out

    def node_state(self, n: dict[str, Any]) -> str:
        """Health state of a node record: ok/throttled/hot, "unknown", or "stale" once not online."""
        if self.status_of(n) != "online":
            return "stale" if n.get("health") else "unknown"
        h = n.get("health")
        return str(h.get("state", "unknown")) if isinstance(h, dict) else "unknown"

    def select_nodes(self, k: int = 1, need: str | None = None) -> list[dict[str, Any]]:
        """Up to k online, allowlisted children that have `need`, best health first.
        Hot/throttled nodes are excluded; ties break on in-flight load, then node id."""
        cands = []
        for n in self.nodes.values():
            nid = str(n.get("node_id", ""))
            if self.status_of(n) != "online" or not self.allowed(nid) or not node_has(n, need):
                continue
            if self.node_state(n) in EXCLUDED_STATES:
                continue
            if _finite(n.get("cooldown_until"), 0.0) > time.time():
                continue  # just failed to connect: skip it until the cooldown ends or it re-registers
            cands.append(n)
        cands.sort(key=lambda n: (-health_score(n), node_load(n), str(n.get("node_id"))))
        try:
            k = int(k)
        except (TypeError, ValueError, OverflowError):
            k = 1
        return cands[:max(0, k)]

    def mark_unreachable(self, n: dict[str, Any], err: BaseException) -> None:
        """Connection to a peer failed: keep auto picks off it for two heartbeats. Its next
        successful register() builds a fresh record without the cooldown."""
        n["cooldown_until"] = time.time() + 2 * self.heartbeat_s
        self._audit("fleet.unreachable", node=str(n.get("node_id")), error=f"{type(err).__name__}: {err}"[:200])

    def health_table(self) -> list[dict[str, Any]]:
        """Per-child health for telemetry / tools: status, state, score, eligibility."""
        rows = []
        for n in sorted(self.nodes.values(), key=lambda n: str(n.get("node_id"))):
            st, hs = self.status_of(n), self.node_state(n)
            rows.append({"node_id": n.get("node_id"), "status": st, "state": hs,
                         "score": health_score(n) if st == "online" else None,
                         "eligible": st == "online" and hs not in EXCLUDED_STATES and self.allowed(str(n.get("node_id"))),
                         "load": node_load(n), "health": n.get("health"),
                         "netsquad": self.netsquad_counts.get(str(n.get("node_id")), 0)})
        return rows

    def info(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "enabled": self.enabled, "parent": self.parent or None,
                "public_url": self.public_url or None, "max_hops": self.max_hops,
                "privileged": self.privileged,
                "token_weak": bool(self.token) and len(self.token) < WEAK_TOKEN_CHARS,
                "token_rotating": bool(getattr(self, "prev_token", "")),
                "children": len(self.nodes), "active_agents": sum(
                    1 for a in self.core.active.values() if a.get("status") == "working")}

    # ------------------------------------------------------------ replay cache persistence
    def _load_nonces(self) -> dict[str, float]:
        p = self._nonce_path
        now = time.time()
        if not p.exists():
            out: dict[str, float] = {}
            self._read_journal(out, now, from_start=True)
            return out
        try:
            raw = _load_nonce_snapshot(p)
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            out = {}
            for k, v in raw.items():
                exp = nonce_expiry(v, now) if isinstance(k, str) and NONCE_RE.fullmatch(k) else None
                if exp is not None:
                    out[k] = exp
            self._read_journal(out, now, from_start=True)
            return dict(list(out.items())[:NONCE_CACHE_MAX])
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self._boot_guard = now
            log.warning("fleet_nonces.json unreadable; refusing requests signed before this boot")
            return {}

    @contextlib.contextmanager
    def _nonce_file_lock(self):
        p = getattr(self, "_nonce_path", None)
        if p is None:
            yield
            return
        fd = None
        try:
            from .safeio import _open_regular  # round 102: never follow a symlink planted at the lock
            fd = _open_regular(str(p) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (OSError, ImportError):  # no flock here: thread lock only, as before
            pass
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)

    def _merge_disk_nonces(self, now: float) -> None:
        """Fold in nonces another process saved since we last looked (cheap: skipped while the
        file's (mtime, size) is the one we last wrote or read)."""
        p = getattr(self, "_nonce_path", None)
        if p is None:
            return
        try:
            st = p.stat()
        except OSError:
            self._read_journal(self._nonces, now)
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == getattr(self, "_nonce_sig", None):
            self._read_journal(self._nonces, now)
            return
        try:
            raw = _load_nonce_snapshot(p)
        except (OSError, ValueError, RecursionError):
            self._read_journal(self._nonces, now)
            return  # unreadable now: keep ours; our save rewrites it whole
        if isinstance(raw, dict):
            for k, v in raw.items():
                exp = nonce_expiry(v, now) if isinstance(k, str) and NONCE_RE.fullmatch(k) else None
                if exp is not None:
                    self._nonces[k] = max(exp, self._nonces.get(k, 0.0))
        self._nonce_sig = sig
        self._read_journal(self._nonces, now, from_start=True)  # a new snapshot: re-read what follows it

    def _journal_path(self) -> Path | None:
        p = getattr(self, "_nonce_path", None)
        return None if p is None else p.with_name(p.name + ".journal")

    def _read_journal(self, into: dict[str, float], now: float, from_start: bool = False) -> None:
        """Fold journal lines ("<nonce> <expiry>") appended since our last read into `into`."""
        jp = self._journal_path()
        if jp is None:
            return
        off = 0 if from_start else getattr(self, "_journal_off", 0)
        try:
            size = jp.stat().st_size
            if size < off:  # someone compacted (truncated) it: read it whole again
                off = 0
            if size == off:
                self._journal_off = off
                return
            if size - off > NONCE_JOURNAL_MAX_BYTES:
                # a journal no live set can produce (compaction keeps it near the live size):
                # read whole, a hand-made multi-GB file exhausted memory at boot. Skip it and
                # refuse anything signed before now, the same guarantee as an unreadable snapshot.
                self._boot_guard = now
                self._journal_off, self._journal_lines = size, 0
                log.warning("fleet nonce journal is %d bytes; skipped, refusing requests signed before now",
                            size - off)
                return
            from .safeio import open_read
            with open_read(jp) as fh:  # O_NOFOLLOW: a symlinked journal is not read
                fh.seek(off)
                chunk = fh.read(size - off)
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1  # only whole lines; a torn tail is re-read next time
        for line in chunk[:end].split(b"\n"):
            k, _, v = line.decode("ascii", "replace").partition(" ")
            if not NONCE_RE.fullmatch(k):
                continue
            try:
                exp = nonce_expiry(float(v), now)
            except ValueError:
                continue
            if exp is not None:
                into[k] = max(exp, into.get(k, 0.0))
        self._journal_off = off + end
        self._journal_lines = (0 if off == 0 else getattr(self, "_journal_lines", 0)) + chunk[:end].count(b"\n")

    def _persist_nonce(self, nonce: str, exp: float) -> None:
        """Small cache: rewrite the snapshot (as before). Big cache: append one journal line, and
        compact into the snapshot once the journal outgrows the live set. Caller holds the locks."""
        jp = self._journal_path()
        if jp is None or len(self._nonces) <= NONCE_JOURNAL_MIN:
            self._save_nonces()
            return
        if getattr(self, "_journal_lines", 0) >= max(NONCE_JOURNAL_MIN, len(self._nonces) // 2):
            self._save_nonces()
            return
        try:
            from .safeio import open_append  # a symlinked journal no longer appends to its target
            with open_append(jp, "ab") as fh:
                fh.write(f"{nonce} {exp!r}\n".encode("ascii"))
                self._journal_off = fh.tell()
            self._journal_lines = getattr(self, "_journal_lines", 0) + 1
        except OSError as e:
            log.debug("could not append to replay journal: %s", e)
            self._save_nonces()

    def _save_nonces(self) -> None:
        """Atomic, best effort: a failed write never refuses a valid request (the boot guard covers
        the next restart if the file is then unreadable)."""
        p = getattr(self, "_nonce_path", None)
        if p is None:  # a bare instance (no data dir): memory-only cache
            return
        from .safeio import atomic_write, open_nofollow
        try:
            atomic_write(p, json.dumps(self._nonces, separators=(",", ":")))
            st = p.stat()
            self._nonce_sig = (st.st_mtime_ns, st.st_size)
            jp = self._journal_path()
            if jp is not None and getattr(self, "_journal_off", 0):
                fd = open_nofollow(jp, os.O_RDWR)  # a symlinked journal must not truncate its target
                try:
                    os.ftruncate(fd, 0)  # everything in it was merged into the snapshot we wrote
                finally:
                    os.close(fd)
            self._journal_off, self._journal_lines = 0, 0
        except OSError as e:
            log.debug("could not persist replay cache: %s", e)

    # ------------------------------------------------------------ auth
    def verify(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        """Authenticate a fleet request: token present, HMAC, timestamp window, nonce (in that order).
        Raises GatewayRefused (a PermissionError) with a distinct code/message per failure."""
        if not self.enabled:
            raise GatewayRefused("no_token", "fleet disabled: set AVRONE_FLEET_TOKEN")
        ts, nonce, sig = (headers.get("x-avrone-ts", ""), headers.get("x-avrone-nonce", ""),
                          headers.get("x-avrone-sig", ""))
        if not isinstance(ts, str) or not TS_RE.fullmatch(ts):  # format check only; window enforced after HMAC
            raise GatewayRefused("bad_timestamp", "bad timestamp")
        ts_i = int(ts)
        if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
            raise GatewayRefused("bad_nonce", "bad nonce (1-128 chars of [A-Za-z0-9_-])")
        if not isinstance(sig, str) or not SIG_RE.fullmatch(sig.lower()):
            raise GatewayRefused("bad_signature", "bad signature")
        sig = sig.lower()  # hex is case-insensitive; some proxies and clients upper-case it
        body_hash = hashlib.sha256(body).hexdigest()  # once: rotation used to hash a 16 MB body twice
        ok = hmac.compare_digest(_mac(self.token, method, path, ts, nonce, body_hash), sig)
        prev = getattr(self, "prev_token", "")
        if not ok and prev and hmac.compare_digest(_mac(prev, method, path, ts, nonce, body_hash), sig):
            ok = True
            self._audit("fleet.token.prev", path=path)  # someone still signs with the old token
        if not ok:
            raise GatewayRefused("bad_signature", "bad signature")
        now = time.time()
        if abs(now - ts_i) > WINDOW_S:
            raise GatewayRefused("stale", f"stale request (timestamp outside window): {skew_text(ts_i, now)}")
        # A nonce must outlive every instant its request is still inside the window (ts + WINDOW_S);
        # keep it at least 2*WINDOW_S past first sight too, in case the clock steps backwards.
        # (The old "now - seen < 2*WINDOW_S" prune dropped a future-skewed nonce at exactly the
        # instant its request became valid again: replay at the window edge.)
        guard = getattr(self, "_boot_guard", None)
        if guard is not None:
            if now - guard > 2 * WINDOW_S:
                self._boot_guard = None  # every pre-boot request is outside the window by now
            elif ts_i < guard:
                raise GatewayRefused("replay", "request signed before this node restarted (replay cache "
                                               "was lost); re-sign and retry")
        # prune + "seen?" + insert are ONE step: a nonce is accepted at most once - across threads
        # (self._lock) AND across processes sharing this data dir (two uvicorn workers, a restart
        # overlapping the old process): each used to keep its own cache, so a captured request
        # replayed to the OTHER worker ran twice, and each save overwrote the other's nonces.
        with self._lock, self._nonce_file_lock():
            self._merge_disk_nonces(now)
            big = len(self._nonces) > NONCE_JOURNAL_MIN
            if not big or now >= getattr(self, "_next_prune", 0.0) or now < getattr(self, "_next_prune", 0.0) - 2:
                self._nonces = {k: v for k, v in self._nonces.items() if now <= v}
                self._next_prune = now + 1.0
            seen = self._nonces.get(nonce)
            if seen is not None and now <= seen:
                raise GatewayRefused("replay", "replayed request (nonce already seen)")
            if len(self._nonces) >= NONCE_CACHE_MAX:
                self._nonces = {k: v for k, v in self._nonces.items() if now <= v}
                if len(self._nonces) >= NONCE_CACHE_MAX:
                    raise GatewayRefused("replay", "replay cache full; refusing new requests until nonces expire")
            exp = max(ts_i + WINDOW_S, now + 2 * WINDOW_S)
            self._nonces[nonce] = exp
            self._persist_nonce(nonce, exp)

    def allowed(self, node_id: str) -> bool:
        if not self.allow:
            return not self.require_allow  # empty list: allow-all, or refuse-all when required
        return node_id in self.allow

    def _audit(self, event: str, **kw: Any) -> None:
        # Codex.emit(run_id, kind, agent, **data): a payload key with one of those names would
        # collide (TypeError, so the audit was silently lost); prefix it with "req_" instead.
        for k in ("run_id", "kind", "agent"):
            if k in kw:
                kw[f"req_{k}"] = kw.pop(k)
        try:
            self.core.codex.emit("fleet", event, self.node_id, **kw)
        except Exception as e:  # noqa: BLE001  -- auditing must never mask the decision
            log.warning("fleet audit %s failed: %s", event, e)

    def _audit_unauth(self, code: str, reason: str, **kw: Any) -> None:
        """Audit a pre-HMAC refusal, at most AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN per minute."""
        with _LAZY_LOCK_GUARD:
            lk = self.__dict__.get("_unauth_lock") or self.__dict__.setdefault("_unauth_lock", threading.Lock())
        summary = None
        with lk:
            now = time.monotonic()
            start = self.__dict__.get("_unauth_start")
            if start is None or now - start >= UNAUTH_AUDIT_WINDOW_S or now < start:
                summary = self._take_unauth_summary()
                self._unauth_start, self._unauth_n = now, 0
            self.refused_unauth = getattr(self, "refused_unauth", 0) + 1
            self._unauth_n += 1
            emit = self._unauth_n <= unauth_audit_per_min()
            if not emit:
                codes = self.__dict__.setdefault("_unauth_codes", {})
                codes[code] = codes.get(code, 0) + 1
        if summary:
            self._audit("fleet.refused.suppressed", **summary)
        if emit:
            self._audit("fleet.refused", code=code, reason=reason, **kw)
        elif self._unauth_n == unauth_audit_per_min() + 1:
            log.warning("fleet: unauthenticated refusals above AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN (%d); "
                        "counting the rest of this minute instead of auditing each", unauth_audit_per_min())

    def _take_unauth_summary(self) -> dict[str, Any] | None:
        codes = self.__dict__.pop("_unauth_codes", None)
        if not codes:
            return None
        return {"suppressed": sum(codes.values()), "codes": dict(sorted(codes.items())),
                "window_s": UNAUTH_AUDIT_WINDOW_S}

    def flush_unauth_audit(self) -> None:
        """Write the pending suppressed-refusal summary now (stop() calls it)."""
        lk = self.__dict__.get("_unauth_lock")
        if lk is None:
            return
        with lk:
            summary = self._take_unauth_summary()
        if summary:
            self._audit("fleet.refused.suppressed", **summary)

    def _refuse(self, code: str, message: str, **kw: Any) -> GatewayRefused:
        self._audit("fleet.refused", code=code, reason=message, **kw)
        return GatewayRefused(code, message)

    def check_policy(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Gateway steps 5-7 on an already-authenticated payload: sender allowlist, hop bound,
        per-request capability. Returns the decision record; raises GatewayRefused."""
        frm = payload.get("from")
        sender = sender_node(frm)
        ctx = {"kind": kind, "sender": sender or None}
        if self.allow or self.require_allow:
            if not sender:
                raise self._refuse("sender_missing", "unidentified sender: signed payload has no 'from'", **ctx)
            if not self.allowed(sender):
                why = "empty allowlist with AVRONE_FLEET_REQUIRE_ALLOW" if not self.allow else "AVRONE_FLEET_ALLOW"
                raise self._refuse("sender_not_allowed", f"sender {sender} not in AVRONE_FLEET_ALLOW ({why})", **ctx)
        if kind == "agentpack" and (not self.allow or sender not in self.allow):
            # moving a whole agent home is never open to "any token holder": explicit allowlist only
            raise self._refuse("sender_not_allowed", f"agent migration from {sender or '?'} refused: sender must be "
                               "listed in a non-empty AVRONE_FLEET_ALLOW", **ctx)
        hops = _parse_hops(payload.get("hops", 1))
        if hops is None:
            raise self._refuse("bad_hops", "bad hop count", **ctx)
        if hops < 0:
            raise self._refuse("bad_hops", "bad hop count (negative)", **ctx)
        if hops > self.max_hops:  # sender may only emit hops <= max_hops (it refuses at hops >= max before +1)
            raise self._refuse("hops", f"max fleet hops exceeded ({hops} > {self.max_hops})", hops=hops, **ctx)
        cap = request_capability(kind, payload)
        flag = payload.get("privileged", False)
        if not isinstance(flag, bool):
            raise self._refuse("capability", "privileged flag must be a JSON boolean", hops=hops, **ctx)
        if flag or cap in PRIVILEGED_CAPABILITIES:
            self._privileged_gate(kind, cap, flag, sender, hops)
            return {**ctx, "hops": hops, "capability": cap, "privileged": True}
        if cap not in kind_capabilities(kind):
            raise self._refuse("capability", f"capability {cap!r} not permitted for {kind}", capability=cap,
                               hops=hops, **ctx)
        return {**ctx, "hops": hops, "capability": cap}

    def _privileged_refuse(self, code: str, message: str, **kw: Any) -> GatewayRefused:
        self._audit("fleet.privileged.refused", code=code, reason=message, **kw)
        return self._refuse(code, message, privileged=True, **kw)

    def _privileged_gate(self, kind: str, cap: str, flag: bool, sender: str, hops: int) -> None:
        """Deny-by-default elevated tier. Every condition must hold; any miss is refused + audited."""
        ctx = {"kind": kind, "sender": sender or None, "capability": cap, "hops": hops}
        if kind != "run" or cap not in PRIVILEGED_CAPABILITIES:
            raise self._privileged_refuse("privileged_capability",
                                          f"privileged flag needs a privileged capability on run, got {cap!r}", **ctx)
        if not flag:
            raise self._privileged_refuse("privileged_capability",
                                          f"capability {cap!r} requires the signed privileged flag", **ctx)
        if not self.privileged:
            raise self._privileged_refuse("privileged_disabled",
                                          "privileged tier disabled on this node (AVRONE_FLEET_PRIVILEGED != allow)", **ctx)
        if not sender or sender not in self.privileged_nodes:
            raise self._privileged_refuse("privileged_not_listed",
                                          f"sender {sender or '?'} not in AVRONE_PRIVILEGED_NODES", **ctx)
        if hops > self.privileged_max_hops:
            raise self._privileged_refuse("privileged_hops", f"privileged request beyond "
                                          f"AVRONE_FLEET_PRIVILEGED_MAX_HOPS ({hops} > {self.privileged_max_hops})", **ctx)

    def dispatch_gateway(self, kind: str, method: str, path: str, body: bytes,
                         headers: dict[str, str], strict_json: bool = False) -> dict[str, Any]:
        """The single vetted inbound path for remote requests. Enforces, in order:
        token, HMAC, timestamp window, nonce, sender allowlist, hops, capability.
        strict_json enables duplicate-key/non-finite rejection for evidence ingress."""
        try:
            self.verify(method, path, body, headers)
        except GatewayRefused as e:
            if e.code in UNAUTH_CODES:
                self._audit_unauth(e.code, str(e), kind=kind, path=path)
            else:
                self._audit("fleet.refused", code=e.code, reason=str(e), kind=kind, path=path)
            raise
        if PRIVILEGED_HEADER in {k.lower() for k in headers}:
            msg = "privileged flag must be inside the signed payload, not a header"
            self._audit("fleet.privileged.refused", code="privileged_unsigned", reason=msg, kind=kind)
            raise self._refuse("privileged_unsigned", msg, kind=kind, privileged=True)
        try:
            payload = strict_json_loads(body) if strict_json else json.loads(body or b"{}")
        except (ValueError, RecursionError):
            message = "body must be strict JSON" if strict_json else "body must be JSON"
            raise self._refuse("bad_body", message, kind=kind) from None
        if not isinstance(payload, dict):
            raise self._refuse("bad_body", "body must be a JSON object", kind=kind)
        decision = self.check_policy(kind, payload)
        self._audit("fleet.dispatch", path=path, **decision)
        if decision.get("privileged"):
            self._audit("fleet.privileged.accept", path=path, **decision)
        return payload

    def check_outbound(self, kind: str, capability: str, hops: int, first_hop: str = "",
                       privileged: bool = False) -> None:
        """Local mirror of the gateway for requests this node originates or forwards."""
        ctx = {"kind": kind, "direction": "out", "target": first_hop or None}
        if not self.enabled:
            raise GatewayRefused("no_token", "fleet disabled: set AVRONE_FLEET_TOKEN")
        if hops >= self.max_hops:
            raise self._refuse("hops", f"max fleet hops ({self.max_hops}) reached", hops=hops, **ctx)
        if privileged or capability in PRIVILEGED_CAPABILITIES:
            pctx = {**ctx, "capability": capability, "hops": hops}
            if kind != "run" or capability not in PRIVILEGED_CAPABILITIES or not privileged:
                raise self._privileged_refuse("privileged_capability",
                                              f"bad privileged request {capability!r} for {kind}", **pctx)
            if not self.privileged:
                raise self._privileged_refuse("privileged_disabled", "privileged tier disabled on this node "
                                              "(AVRONE_FLEET_PRIVILEGED != allow)", **pctx)
            if hops + 1 > self.privileged_max_hops:
                raise self._privileged_refuse("privileged_hops", "privileged request would exceed "
                                              f"AVRONE_FLEET_PRIVILEGED_MAX_HOPS ({self.privileged_max_hops})", **pctx)
            if not first_hop or first_hop in ("auto", "local"):
                raise self._privileged_refuse("privileged_target", "privileged requests need an explicit node", **pctx)
            if not self.allowed(first_hop):
                raise self._refuse("target_not_allowed", f"node {first_hop} not in AVRONE_FLEET_ALLOW", **ctx)
            return
        if capability not in kind_capabilities(kind):
            raise self._refuse("capability", f"capability {capability!r} not permitted for {kind}",
                               capability=capability, **ctx)
        if first_hop and first_hop not in ("auto", "local") and not self.allowed(first_hop):
            raise self._refuse("target_not_allowed", f"node {first_hop} not in AVRONE_FLEET_ALLOW", **ctx)

    # ------------------------------------------------------------ offload (edgeroute)
    def offload_mode(self) -> str:
        return offload_mode() if self.enabled else "off"

    def offload_plan(self, overflow: int, hops: int = 0) -> list[str]:
        """Node id for each of `overflow` workers (may be shorter/empty: those stay local).
        spill = all to the single healthiest child; spread = round-robin over the healthy children."""
        mode = self.offload_mode()
        if mode == "off" or overflow <= 0 or hops >= self.max_hops:
            return []
        nodes = self.select_nodes(1 if mode == "spill" else overflow, need="spawn:chat")
        if not nodes:
            return []
        return [str(nodes[i % len(nodes)]["node_id"]) for i in range(overflow)]

    async def offload_worker(self, run_id: str, node_id: str, role: str, task: str, focus: str,
                             hops: int = 0) -> str:
        """Run one squad worker on `node_id` (signed dispatch, mode chat). Raises on any failure,
        timeout or empty answer after auditing fleet.offload_fallback; the caller runs it locally."""
        prompt = (f"You are {role}, one worker of a squad on another AVRONE node.\nTASK: {task}\n"
                  f"YOUR FOCUS: {focus}\nReport your findings for your focus only.")
        self._audit("fleet.offload", squad_run=run_id, node=node_id, role=role, hops=hops)
        try:
            res = await asyncio.wait_for(self.dispatch([node_id], "chat", prompt, hops, origin_run=run_id),
                                         offload_timeout())
            answer = str((res or {}).get("answer") or "").strip()
            if not answer:
                raise RuntimeError("empty answer from remote worker")
            return answer
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  -- includes TimeoutError and gateway refusals
            reason = f"{type(e).__name__}: {e}"[:300]
            self._audit("fleet.offload_fallback", squad_run=run_id, node=node_id, role=role, reason=reason)
            raise RuntimeError(f"offload to {node_id} failed: {reason}") from e

    # ------------------------------------------------------------ netsquad (squad -> node network)
    def squad_dispatch(self, override: str | None = None) -> str:
        return squad_dispatch(override) if self.enabled else "local"

    def netsquad_plan(self, n: int, local_limit: int, hops: int = 0, path: list[str] | None = None,
                      dispatch: str | None = None, budget: int | None = None) -> dict[int, str]:
        """{subtask index: node id} for the subtasks a squad of n places on the network.
        network = every subtask, hybrid = the ones past the local governor limit; nodes come from
        select_nodes (healthy, allowlisted, advertising squad:net), round-robin by score, never a
        node already on the route (audited fleet.cycle_refused), at most AVRONE_NET_MAX_NODES nodes
        and AVRONE_NET_MAX_REMOTE_AGENTS remote agents. Indices left out run locally."""
        mode = self.squad_dispatch(dispatch)
        if mode == "local" or n <= 0 or hops >= self.max_hops:
            return {}
        idx = list(range(n)) if mode == "network" else list(range(max(0, int(local_limit)), n))
        if not idx:
            return {}
        route = [str(x) for x in (path or [])]
        visited = set(route) | {self.node_id}
        nodes: list[str] = []
        for nd in self.select_nodes(len(self.nodes), need=NETSQUAD_CAP):
            nid = str(nd.get("node_id"))
            if nid in visited:
                self._audit("fleet.cycle_refused", node=nid, route=route + [self.node_id], direction="out")
                continue
            nodes.append(nid)
        nodes = nodes[:net_max_nodes()]
        if not nodes:
            return {}
        size = self.netsquad_size()
        cap = net_max_remote_agents() if budget is None else max(0, min(net_max_remote_agents(), int(budget)))
        fit = cap // size
        if fit < len(idx):
            self._audit("fleet.netsquad_capped", wanted=len(idx), placed=fit, size=size, cap=cap)
            idx = idx[:fit]
        return {i: nodes[j % len(nodes)] for j, i in enumerate(idx)}

    @staticmethod
    def netsquad_size() -> int:
        """Remote agents one placement spawns: the sub-squad size, or 1 in chat worker mode."""
        return net_subsquad() if net_worker_mode() == "squad" else 1

    def netsquad_share(self, placements: int, budget: int | None = None) -> int:
        """Remote-agent budget handed to each placement's subtree (its own sub-squad + any recursion)."""
        cap = net_max_remote_agents() if budget is None else max(1, min(net_max_remote_agents(), int(budget)))
        return max(self.netsquad_size(), cap // max(1, int(placements)))

    async def netsquad_worker(self, run_id: str, node_id: str, role: str, task: str, focus: str,
                              hops: int = 0, path: list[str] | None = None,
                              budget: int | None = None) -> dict[str, Any]:
        """Place one squad subtask on `node_id`: it runs there as a sub-squad (or chat) of the same
        subtask and may place further down. Returns {answer, node, route}. Raises after auditing
        fleet.netsquad_fallback on any failure/timeout/empty answer; the caller runs it locally."""
        mode = net_worker_mode()
        size = self.netsquad_size()
        route = [str(x) for x in (path or [])] + [self.node_id]
        prompt = (f"You are {role}, one placed worker of a squad on another AVRONE node.\nTASK: {task}\n"
                  f"YOUR SUBTASK: {focus}\nSolve this subtask and report your findings for it only.")
        self._audit("fleet.netsquad_place", squad_run=run_id, node=node_id, role=role, size=size, mode=mode,
                    hops=hops, route=route)
        self.netsquad_counts[node_id] = self.netsquad_counts.get(node_id, 0) + 1
        node: dict[str, Any] | None = None
        bumped = False
        try:
            self.check_outbound("run", NETSQUAD_CAP, hops, node_id)
            if node_id in route:
                self._audit("fleet.cycle_refused", node=node_id, route=route, direction="out")
                raise PermissionError(f"cycle: {node_id} already on route {'/'.join(route)}")
            node = self.pick(node_id)
            payload: dict[str, Any] = {
                "route": [], "mode": mode, "prompt": prompt, "n": size if mode == "squad" else None,
                "hops": hops + 1, "from": self.node_id, "origin_run": run_id, "capability": NETSQUAD_CAP,
                "netsquad": {"path": route, "role": str(role)[:60],
                             "budget": max(size, int(budget if budget is not None else size))}}
            posts = bridge_export(self.core, run_id=run_id)
            if posts:
                payload["bridge"] = posts
            node["load"] = node.get("load", 0) + 1
            bumped = True
            t = offload_timeout()
            res = await asyncio.wait_for(self._post(node["url"], "/api/fleet/run", payload, timeout=t), t)
            answer = str((res or {}).get("answer") or "").strip()
            if not answer:
                raise RuntimeError("empty answer from remote squad")
            bridge_merge(self.core, res.get("bridge"), node_id, run_id=run_id)
            sub = [str(x) for x in (res.get("path") or [node_id]) if isinstance(x, (str, int))][:16]
            return {"answer": answer, "node": node_id, "route": route + (sub or [node_id])}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  -- includes TimeoutError and gateway refusals
            reason = f"{type(e).__name__}: {e}"[:300]
            self._audit("fleet.netsquad_fallback", squad_run=run_id, node=node_id, role=role, reason=reason)
            raise RuntimeError(f"netsquad to {node_id} failed: {reason}") from e
        finally:
            if bumped and node is not None:
                node["load"] = max(0, node.get("load", 1) - 1)

    def _netsquad_refuse(self, code: str, message: str, **kw: Any) -> PermissionError:
        self._audit("fleet.refused", code=code, reason=message, endpoint="run", capability=NETSQUAD_CAP, **kw)
        return PermissionError(message)

    async def _handle_netsquad(self, payload: dict[str, Any], hops: int) -> dict[str, Any]:
        """Receiving side of a netsquad placement: run the subtask here as a sub-squad (or chat)."""
        sender = sender_node(payload.get("from"))
        if payload.get("capability") != NETSQUAD_CAP:
            raise self._netsquad_refuse("capability", "netsquad job requires capability 'squad:net'", sender=sender)
        if payload.get("privileged", False) is not False:
            raise self._netsquad_refuse("privileged_capability", "netsquad jobs are never privileged", sender=sender)
        if payload.get("route"):
            raise self._netsquad_refuse("bad_route", "netsquad jobs are placed on a direct child (empty route)",
                                        sender=sender)
        ns = payload.get("netsquad")
        path = ns.get("path") if isinstance(ns, dict) else None
        if (not isinstance(path, list) or not path or len(path) > self.max_hops + 1
                or not all(isinstance(x, str) and ID_RE.match(x) for x in path)):
            raise self._netsquad_refuse("bad_netsquad", "netsquad job needs a valid route path", sender=sender)
        if self.node_id in path:
            self._audit("fleet.cycle_refused", node=self.node_id, route=path, sender=sender, direction="in")
            raise PermissionError(f"cycle refused: {self.node_id} already on route {'/'.join(path)}")
        if sender and path[-1] != sender:
            raise self._netsquad_refuse("bad_netsquad", "netsquad route does not end at the signed sender",
                                        sender=sender)
        mode = str(payload.get("mode", "squad"))
        if mode not in NET_WORKER_MODES:
            raise self._netsquad_refuse("bad_netsquad", f"netsquad mode must be squad or chat, got {mode!r}",
                                        sender=sender)
        try:
            n = max(1, min(int(payload.get("n") or 2), NET_SUBSQUAD_MAX))
            budget = max(1, min(int(ns.get("budget") or n), net_max_remote_agents()))
        except (TypeError, ValueError):
            raise self._netsquad_refuse("bad_netsquad", "bad netsquad size/budget", sender=sender) from None
        prompt = str(payload.get("prompt", ""))
        c = self.core
        self._audit("fleet.run", mode=mode, frm=payload.get("from"), hops=hops, netsquad=True, size=n, route=path)
        # One bridge per (sender, origin run): the sender's posts are merged in first, the remote
        # squad runs under that id so its agents read them, and only posts written here go back.
        origin_run = re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("origin_run") or ""))[:40]
        rid = f"net-{sender or path[-1]}-{origin_run}" if origin_run else None
        bridge_merge(c, payload.get("bridge"), sender or path[-1], run_id=rid)
        # never privileged; hops / route / budget ref-counted with every live run (see _run_scope)
        with self._run_scope(hops, "", netsquad=(list(path), budget)):
            if rid:
                res = await (c.chat(prompt, run_id=rid) if mode == "chat" else c.squad(prompt, n, run_id=rid))
            else:
                res = await (c.chat(prompt) if mode == "chat" else c.squad(prompt, n))
        out = {"node": self.node_id, "path": [self.node_id], "run_id": res.get("run_id"),
               "answer": res.get("answer") or "", "netsquad": True}
        posts = bridge_export(c, run_id=rid or res.get("run_id"), only_origin=self.node_id)
        if posts:
            out["bridge"] = posts
        return out

    # ------------------------------------------------------------ registry (server side)
    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        nid = str(payload.get("node_id", ""))
        url_raw = payload.get("url", "")
        if not ID_RE.match(nid):
            raise ValueError("invalid node_id")
        if nid == self.node_id:
            raise ValueError("node_id collides with this server")
        if not self.allowed(nid):
            raise PermissionError(f"node {nid} not in AVRONE_FLEET_ALLOW")
        url = check_node_url(url_raw)
        caps = payload.get("capabilities", {})
        kids = payload.get("children", [])
        rec = {"node_id": nid, "url": url, "last_seen": time.time(),
               "capabilities": caps if isinstance(caps, dict) else {},
               "children": [c for c in kids if isinstance(c, str)][:1000] if isinstance(kids, list) else [],
               "load": node_load(payload),
               "presence": payload.get("presence"),
               "health": sanitize_health(payload.get("health"))}
        # strict JSON: a peer sending Infinity/NaN (Python's json accepts them) or bytes in
        # capabilities/presence was stored as is, and every GET /api/fleet after that answered 500
        try:
            json.dumps(rec, default=str)  # unencodable keys (a tuple) are still refused outright
        except (TypeError, ValueError, RecursionError):
            raise ValueError("node record is not JSON-serializable") from None
        from .codex import jsonable
        rec = jsonable(rec)
        size = len(json.dumps(rec, default=str, allow_nan=False))
        if size > MAX_NODE_RECORD_BYTES:
            raise ValueError(f"node record is {size} bytes (max {MAX_NODE_RECORD_BYTES}): trim capabilities/presence")
        with self._lock:
            # the "already online at another url" check and the write are one step: two racing
            # registrations of one id from different urls could otherwise both be accepted
            existing = self.nodes.get(nid)
            if existing and existing.get("url") != url and self.status_of(existing) == "online":
                raise ValueError(f"node_id {nid} already online at another url")
            nodes = dict(self.nodes)
            cap = max_nodes()
            if nid not in nodes and len(nodes) >= cap:
                # make room from offline peers only (oldest first); never evict a live one
                for old_id in sorted((k for k, v in nodes.items() if self.status_of(v) == "offline"),
                                     key=lambda k: nodes[k].get("last_seen", 0)):
                    if len(nodes) < cap:
                        break
                    del nodes[old_id]
                if len(nodes) >= cap:
                    raise PermissionError(f"fleet registry full ({cap} peers; AVRONE_FLEET_MAX_NODES)")
            # copy-on-write: readers iterating self.nodes.values() never see it change size mid-loop
            self.nodes = {**nodes, nid: rec}
            refresh = existing is not None and existing.get("url") == url  # a plain heartbeat
            save = not (refresh and time.monotonic() - getattr(self, "_saved_at", float("-inf"))
                        < FLEET_SAVE_DEBOUNCE_S)
            if not save:
                self._dirty = True  # flushed by the next save past the window, or by stop()
        if save:
            self._save()  # outside the state lock, so concurrent joins share one write
        self.core.codex.emit("fleet", "fleet.join", self.node_id, node=nid, url=url)
        return {"ok": True, "parent": self.node_id}

    def forget(self, nid: str, *, allow_online: bool = False) -> dict[str, Any]:
        """Remove a peer from the registry. An online peer is refused unless allow_online: it would
        simply re-register on its next heartbeat (revoke it in AVRONE_FLEET_ALLOW first)."""
        if not isinstance(nid, str) or not ID_RE.match(nid):
            raise ValueError("invalid node_id")
        with self._lock:
            rec = self.nodes.get(nid)
            if rec is None:
                raise KeyError(nid)
            status = self.status_of(rec)
            if status == "online" and not allow_online:
                raise PermissionError(f"{nid} is online and would rejoin on its next heartbeat; "
                                      "remove it from AVRONE_FLEET_ALLOW first")
            self.nodes = {k: v for k, v in self.nodes.items() if k != nid}
        self._save()
        self._audit("fleet.forget", node=nid, status=status)
        return {"node": nid, "status": status}

    # ------------------------------------------------------------ client side
    def _client(self, timeout: float) -> httpx.AsyncClient:
        kw: dict[str, Any] = {"timeout": timeout}
        if HTTP_TRANSPORT is not None:
            kw["transport"] = HTTP_TRANSPORT
        return httpx.AsyncClient(**kw)

    async def _post(self, base: str, path: str, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        try:  # a peer saved before v0.31 (or a hand-edited fleet.json) may hold a url we must not call
            check_node_url(base)
        except ValueError as e:
            raise RuntimeError(f"refusing to call peer url: {e}") from None
        body = json.dumps(payload).encode()
        headers = {"content-type": "application/json", **sign(self.token, "POST", path, body)}
        async with self._client(timeout) as c:
            r = await c.post(base + path, content=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"{base}{path} -> {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError:
            # a captive portal, proxy or wrong service answering 200 with HTML: name it, instead of
            # surfacing a bare JSONDecodeError
            ctype = r.headers.get("content-type", "?")
            raise RuntimeError(f"{base}{path} -> {r.status_code} but not JSON ({ctype}): "
                               f"{' '.join(r.text[:200].split())}") from None
        if not isinstance(data, dict):  # every caller does .get() on it
            raise RuntimeError(f"{base}{path} -> {r.status_code} but JSON {type(data).__name__}, not an object")
        return data

    # ------------------------------------------------------------ memory sync
    async def push_memory(self, entries: list[dict[str, Any]], origin: str) -> dict[str, Any]:
        """Send memory entries ({ns, key, value, ts, agent}) up to the parent over the signed gateway."""
        if not self.enabled:
            raise GatewayRefused("no_token", "fleet disabled: set AVRONE_FLEET_TOKEN")
        if not self.parent:
            raise RuntimeError("no parent node: set AVRONE_FLEET_PARENT to sync memory upward")
        self.check_outbound("memory", "memory:sync", 0)
        if len(entries) > MEMORY_SYNC_MAX_ENTRIES:
            raise ValueError(f"too many entries ({len(entries)} > {MEMORY_SYNC_MAX_ENTRIES})")
        payload = {"from": origin or self.node_id, "hops": 1, "capability": "memory:sync", "entries": entries}
        res = await self._post(self.parent, MEMORY_PATH, payload, timeout=30)
        self._audit("fleet.memory.push", entries=len(entries), parent=self.parent)
        return res

    def receive_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Server side of /api/fleet/memory. Call ONLY with a payload returned by dispatch_gateway.
        Merges into fleet:<sender>/<ns> (sender = signed `from` node id); never into a local namespace."""
        from . import memstore
        if request_capability("memory", payload) != "memory:sync":  # defense in depth
            raise PermissionError("capability memory:sync required")
        sender = sender_node(payload.get("from"))
        if not ID_RE.match(sender):
            raise ValueError("invalid sender node id")
        if sender == self.node_id:
            raise ValueError("refusing memory sync from this node's own id")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("entries must be a list")
        if len(entries) > MEMORY_SYNC_MAX_ENTRIES:
            raise ValueError(f"too many entries ({len(entries)} > {MEMORY_SYNC_MAX_ENTRIES})")
        groups: dict[str, list[dict[str, Any]]] = {}
        bad = 0
        store = memstore.MemoryStore(self.data_dir / "memory.jsonl")
        existing = set(store.namespaces())
        mine = {n for n in existing if memstore.remote_sender(n) == sender}
        remote_all = {n for n in existing if n.startswith(memstore.REMOTE_PREFIX)}
        per_sender, total = memstore.remote_ns_per_sender(), memstore.remote_ns_total()
        over_quota = 0
        for e in entries:
            ns = e.get("ns") if isinstance(e, dict) else None
            if not isinstance(ns, str) or not memstore.is_local(ns):  # sender may only name its own local ns
                bad += 1
                continue
            target = memstore.remote_namespace(sender, ns)
            if target not in existing and target not in groups:
                # a peer's namespaces are capped per sender and in total (half the store's cap)
                if len(mine) >= per_sender or len(remote_all) >= total:
                    bad += 1
                    over_quota += 1
                    continue
                mine.add(target)
                remote_all.add(target)
            groups.setdefault(target, []).append(e)
        stats = {"merged": 0, "skipped": 0, "rejected": bad}
        if groups:
            for k, v in store.merge_many(groups, source=sender).items():
                stats[k] += v
        self._audit("fleet.memory.merge", sender=sender, namespaces=sorted(groups), over_quota=over_quota, **stats)
        out = {"ok": True, "node": self.node_id, "namespaces": sorted(groups), **stats}
        if over_quota:
            out["over_quota"] = over_quota
            out["note"] = (f"{over_quota} entries refused: namespace quota ({per_sender} per sender, {total} "
                           f"for all peers; AVRONE_MEMORY_MAX_REMOTE_NS / AVRONE_MEMORY_MAX_NAMESPACES)")
        return out

    # ------------------------------------------------------------ agent migration (edgepack)
    def _workspace(self) -> Path:
        ws = getattr(getattr(self.core, "settings", None), "workspace", None)
        if not isinstance(ws, (str, Path)):
            raise RuntimeError("no workspace configured for agent homes")
        return Path(ws)

    def pick_migration_target(self, target: str = "auto") -> dict[str, Any]:
        """A healthy peer for an agent home: an explicit online node, or (auto) the coolest /
        least-loaded online allowed node by its governor readings, else any online node."""
        if target not in ("", "auto"):
            n = self.pick(target)
            if self.status_of(n) != "online":
                raise RuntimeError(f"node {target} is not online")
            return n
        thermal_max = getattr(getattr(self.core, "governor", None), "thermal_max_c", None)
        live = [n for n in self.nodes.values() if self.status_of(n) == "online"
                and ID_RE.match(str(n.get("node_id", ""))) and self.allowed(str(n["node_id"]))
                and n["node_id"] != self.node_id]
        if not live:
            raise RuntimeError("no online nodes to migrate to")

        def h(n: dict[str, Any], k: str) -> float | None:
            v = (n.get("health") if isinstance(n.get("health"), dict) else {}).get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            try:
                f = float(v)  # math.isfinite(10**400) itself raised OverflowError
            except OverflowError:
                return None
            return f if math.isfinite(f) else None
        # Never pick a peer that is itself hot: one that reports state hot/throttled, or a temp at
        # or over this node's thermal max. (The old code fell back to the hot peers when no cool
        # one existed, bouncing agents from one overheating node to another.)
        healthy = [n for n in live if self.node_state(n) not in EXCLUDED_STATES]
        if isinstance(thermal_max, (int, float)) and not isinstance(thermal_max, bool) and math.isfinite(thermal_max):
            healthy = [n for n in healthy if h(n, "temp_c") is None or h(n, "temp_c") < thermal_max]
        if not healthy:
            raise RuntimeError("no online nodes to migrate to (every online peer is hot or throttled)")
        live = healthy
        measured = [n for n in live if h(n, "temp_c") is not None or h(n, "load") is not None]

        def nload(n: dict[str, Any]) -> float:
            v = n.get("load", 0)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 1e9
        if measured:
            return min(measured, key=lambda n: (h(n, "temp_c") if h(n, "temp_c") is not None else 0.0,
                                                h(n, "load") or 0.0, nload(n), n["node_id"]))
        return min(live, key=lambda n: (nload(n), n["node_id"]))

    async def migrate_agent(self, agent_id: str, target: str = "auto") -> dict[str, Any]:
        """Send one agent's home (dir + memory) to a peer over the signed gateway. Raises on
        refusal/failure (audited as agent.migrate_refused / agent.migrate_failed); the local home
        is never touched, so a failed migration leaves the agent where it is."""
        aid = str(agent_id or "")
        inflight = self._migrating
        with self._lock:  # "in flight?" + add as one step, even across threads/event loops
            busy = aid in inflight
            if not busy:
                inflight.add(aid)
        if busy:
            # a second concurrent migration of the same agent would export/post twice and race
            # the two imports on the peer; refuse it (the first one is still in flight)
            self._audit("agent.migrate_refused", reason="migration of this agent already in progress",
                        agent_id=aid[:64], target=target or "auto")
            raise PermissionError(f"migration of agent {aid[:64]!r} already in progress")
        try:
            return await self._migrate_agent(aid, target)
        finally:
            with self._lock:
                inflight.discard(aid)

    async def _migrate_agent(self, aid: str, target: str) -> dict[str, Any]:
        from . import agentfs, agentpack
        ctx = {"agent_id": aid[:64], "target": target or "auto"}
        try:
            if agentfs.sanitize_agent_id(aid) != aid:
                raise PermissionError(f"invalid agent id {aid[:64]!r}")
            if not migrate_enabled():
                raise PermissionError("agent migration disabled (AVRONE_AGENT_MIGRATE != on)")
            first = target if target not in ("", "auto") else ""
            self.check_outbound("agentpack", "agent:migrate", 0, first)
            node = self.pick_migration_target(target)
            bundle, man = agentpack.export_pack(self._workspace(), self.data_dir, aid, self.node_id,
                                                home=agentpack.home_node(self.data_dir, aid, self.node_id))
        except (PermissionError, agentpack.PackError) as e:
            self._audit("agent.migrate_refused", reason=str(e)[:300], **ctx)
            raise
        except Exception as e:  # noqa: BLE001
            self._audit("agent.migrate_failed", reason=f"{type(e).__name__}: {e}"[:300], **ctx)
            raise
        import base64
        payload = {"from": self.node_id, "hops": 1, "capability": "agent:migrate", "agent_id": aid,
                   "bundle": base64.b64encode(bundle).decode()}
        try:
            res = await self._post(node["url"], AGENTPACK_PATH, payload, timeout=120)
        except Exception as e:  # noqa: BLE001
            refused = isinstance(e, RuntimeError) and re.search(r"-> (401|403|400):", str(e)) is not None
            self._audit("agent.migrate_refused" if refused else "agent.migrate_failed",
                        reason=f"{type(e).__name__}: {e}"[:300], **{**ctx, "target": node["node_id"]})
            raise
        agentpack.record(self.data_dir, aid, migrated_to=node["node_id"], migrated_stamp=man["created"],
                         migrated_at=int(time.time()))
        self._audit("agent.migrate", agent_id=aid, target=node["node_id"], files=len(man["files"]),
                    bytes=man["total_bytes"], created=man["created"], home=man["home"])
        return {"ok": True, "agent_id": aid, "target": node["node_id"], "files": len(man["files"]),
                "bytes": man["total_bytes"], "remote": res}

    def receive_agentpack(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Server side of /api/fleet/agentpack. Call ONLY with a payload returned by dispatch_gateway."""
        import base64
        import binascii

        from . import agentpack
        if request_capability("agentpack", payload) != "agent:migrate" or payload.get("privileged"):
            raise PermissionError("capability agent:migrate required (never privileged)")
        sender = sender_node(payload.get("from"))
        ctx = {"direction": "in", "sender": sender or None, "agent_id": str(payload.get("agent_id", ""))[:64]}
        if not migrate_enabled():
            self._audit("agent.migrate_refused", reason="AVRONE_AGENT_MIGRATE != on", **ctx)
            raise PermissionError("agent migration disabled on this node (AVRONE_AGENT_MIGRATE != on)")
        if not ID_RE.match(sender) or sender == self.node_id:
            raise ValueError("invalid sender node id")
        try:
            bundle = base64.b64decode(str(payload.get("bundle", "")), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("bundle must be base64") from None
        try:
            res = agentpack.import_pack(self._workspace(), self.data_dir, bundle, self.node_id,
                                        expect_agent=str(payload.get("agent_id", "")), expect_origin=sender)
        except agentpack.PackError as e:
            self._audit("agent.migrate_refused", reason=str(e)[:300], **ctx)
            raise ValueError(str(e)) from None
        self._audit("agent.migrate", **ctx, files=res["files"], bytes=res["bytes"], home=res["home"])
        return {"ok": True, "node": self.node_id, **res}

    def _busy_agent_ids(self) -> set[str]:
        """Agent home ids with a live (working) agent on this core right now."""
        from . import agentfs, memstore
        return {agentfs.sanitize_agent_id(a.get("agent_id") or a.get("role") or memstore.agent_namespace(n))
                for n, a in list(self.core.active.items()) if isinstance(a, dict) and a.get("status") == "working"}

    def idle_agents(self, now: float | None = None) -> list[str]:
        """Agent homes on this node that are not running and untouched for AVRONE_AGENT_IDLE_SECS."""
        from . import agentcontainer, agentfs, agentpack
        now = time.time() if now is None else now
        root = agentfs.agents_root(self._workspace())
        if not root.is_dir() or root.is_symlink():
            return []
        busy = self._busy_agent_ids()
        idx = agentpack.load_index(self.data_dir)
        out = []
        for d in sorted(root.iterdir()):
            aid = d.name
            if d.is_symlink() or not d.is_dir() or agentfs.sanitize_agent_id(aid) != aid or aid in busy:
                continue
            stamp = agentpack.content_stamp(self._workspace(), self.data_dir, aid)
            if now - stamp < agentcontainer.idle_secs():
                continue
            if idx.get(aid, {}).get("migrated_stamp") == stamp:  # already moved, unchanged since
                continue
            out.append(aid)
        return out

    async def thermal_migrate(self, now: float | None = None) -> list[dict[str, Any]]:
        """Governor hook: when this node is hot and AVRONE_AGENT_MIGRATE=on, move idle agents'
        homes to a cooler peer. Never raises; a failed agent stays put."""
        if not (self.enabled and migrate_enabled()):
            return []
        gov = getattr(self.core, "governor", None)
        try:
            if gov is None or not gov.is_hot():
                return []
            agents = self.idle_agents(now)
        except Exception as e:  # noqa: BLE001
            log.warning("thermal migrate check failed: %s", e)
            return []
        out: list[dict[str, Any]] = []
        for aid in agents:
            # `agents` is a snapshot and every migration awaits the network: an agent that started
            # working while an earlier pack was in flight is busy NOW. Re-check right before the
            # export (no await between this check and migrate_agent's synchronous export_pack).
            try:
                busy = aid in self._busy_agent_ids()
            except Exception:  # noqa: BLE001 - unsure = treat as busy = leave it home
                busy = True
            if busy:
                self._audit("agent.migrate_skipped", agent_id=aid[:64], reason="agent became busy")
                out.append({"agent_id": aid, "ok": False, "error": "agent became busy; left in place"})
                continue
            try:
                res = await self.migrate_agent(aid, "auto")
                out.append({"agent_id": aid, "ok": True, "target": res["target"]})
            except Exception as e:  # noqa: BLE001 - audited in migrate_agent; agent stays put
                out.append({"agent_id": aid, "ok": False, "error": str(e)[:200]})
                if "no online nodes" in str(e):
                    break
        return out

    async def thermal_loop(self) -> None:
        while True:
            await asyncio.sleep(migrate_interval())
            await self.thermal_migrate()

    async def join_parent_once(self) -> dict[str, Any]:
        from .presence import snapshot
        payload = {"presence": await snapshot(self.core),"node_id": self.node_id, "url": self.public_url,
                   "capabilities": {"tools": self.core.tools.names(), "runner": self.core.runner.kind,
                                    NETSQUAD_CAP: True},
                   "children": sorted(self.nodes), "load": self.info()["active_agents"],
                   "health": local_health(self.core)}
        return await self._post(self.parent, "/api/fleet/register", payload, timeout=10)

    async def heartbeat_loop(self) -> None:
        backoff = self.heartbeat_s
        while True:
            try:
                await self.join_parent_once()
                backoff = self.heartbeat_s
            except Exception as e:  # noqa: BLE001
                log.warning("fleet heartbeat to %s failed: %s", self.parent, e)
                backoff = min(backoff * 2, 300)
            await asyncio.sleep(backoff)

    def start(self) -> None:
        if self.enabled and self.parent and self.public_url and self._task is None:
            self._task = asyncio.create_task(self.heartbeat_loop())
        if self.enabled and migrate_enabled() and self._thermal_task is None:
            self._thermal_task = asyncio.create_task(self.thermal_loop())

    def start_queue(self, data_dir: Path) -> None:
        from .jobqueue import JobQueue, Worker
        self.queue = JobQueue(Path(data_dir) / "jobs.db")
        self.worker = Worker(self.queue, self.run_job)
        self.worker.start()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._thermal_task:
            self._thermal_task.cancel()
            self._thermal_task = None
        if self.worker:
            await self.worker.stop()
        if self.queue:
            await self.queue.close()
        for t in list(self._bg):
            t.cancel()
        self.flush_unauth_audit()
        if getattr(self, "_dirty", False):  # heartbeat refreshes still inside the save window
            try:
                await asyncio.to_thread(self.flush)
            except OSError as e:
                log.warning("could not save fleet.json on stop: %s", e)

    def pick(self, target: str) -> dict[str, Any]:
        if target in ("", "auto"):
            best = self.select_nodes(1)
            if best:
                return best[0]
            if any(self.status_of(n) == "online" and self.allowed(str(n.get("node_id", ""))) for n in self.nodes.values()):
                raise RuntimeError("no healthy online nodes (all online nodes are hot or throttled)")
            raise RuntimeError("no online nodes")
        n = self.nodes.get(target)
        if not n:
            raise RuntimeError(f"unknown node {target!r}; known: {sorted(self.nodes) or 'none'}")
        if not self.allowed(target):
            raise PermissionError(f"node {target} not in AVRONE_FLEET_ALLOW")
        if self.status_of(n) == "offline":
            raise RuntimeError(f"node {target} is offline")
        return n

    async def dispatch(self, route: list[str], mode: str, prompt: str, hops: int, n: int | None = None,
                       origin_run: str = "", privileged: str = "", rounds: int | None = None,
                       checkpoint_id: str | None = None) -> dict[str, Any]:
        """Send a job down `route` (first element = one of my children).
        `privileged` = "priv:shell"/"priv:admin" requests the elevated tier (signed into the body).
        `rounds` (deep only) is clamped to 1..5 and only sent when given.
        `checkpoint_id` (squad/deep) rides in the signed body; the running node keys it per sender,
        so re-sending the same job with the same id resumes it there."""
        if hops >= self.max_hops:
            raise PermissionError(f"max fleet hops ({self.max_hops}) reached")
        mode = mode if mode in SPAWN_MODES else "squad"  # the remote ran unknown modes as squad anyway
        cap = privileged or f"spawn:{mode}"
        self.check_outbound("run", cap, hops, route[0] if route else "", privileged=bool(privileged))
        node = self.pick(route[0] if route else "auto")
        payload = {"route": route[1:], "mode": mode, "prompt": prompt, "n": n, "hops": hops + 1,
                   "from": self.node_id, "origin_run": origin_run}
        if privileged:  # inside the signed body: cannot be added, removed or changed in transit
            payload.update(privileged=True, capability=privileged)
        rounds = clamp_deep_rounds(rounds)
        if rounds is not None:
            payload["rounds"] = rounds
        if checkpoint_id not in (None, ""):
            if mode == "chat":
                raise ValueError("checkpoint_id works with mode squad or deep")
            payload["checkpoint_id"] = sanitize_ckpt_id(checkpoint_id)
        auto = not route
        tried: list[str] = []
        while True:
            cur = node
            cur["load"] = node_load(cur) + 1
            try:
                return await self._post(cur["url"], "/api/fleet/run", payload, timeout=900)
            except Exception as e:  # noqa: BLE001
                if not _unreachable(e):
                    raise
                self.mark_unreachable(cur, e)
                tried.append(str(cur.get("node_id")))
                nxt = [c for c in self.select_nodes(FAILOVER_MAX) if str(c.get("node_id")) not in tried]
                if not auto or len(tried) >= FAILOVER_MAX or not nxt:
                    if len(tried) == 1:
                        raise  # nothing else was tried: keep the original error type for callers
                    raise RuntimeError(f"peer unreachable ({', '.join(tried)}): {type(e).__name__}: {e}") from e
                node = nxt[0]
            finally:
                cur["load"] = max(0, node_load(cur) - 1)

    async def send_message(self, route: list[str], agent: str, text: str, sender: str, hops: int = 0,
                           wait: bool = False) -> dict[str, Any]:
        """Deliver `text` to `agent` on the node at `route` (empty route = this node)."""
        if route:
            if hops >= self.max_hops:
                raise PermissionError(f"max fleet hops ({self.max_hops}) reached")
            self.check_outbound("message", "message", hops, route[0])
            node = self.pick(route[0])
            payload = {"route": route[1:], "agent": agent, "message": text, "from": sender, "hops": hops + 1, "wait": wait}
            res = await self._post(node["url"], "/api/fleet/message", payload, timeout=900 if wait else 30)
            return {**res, "path": [self.node_id] + res.get("path", [])}
        return await self.deliver_local(agent, text, sender, wait)

    async def deliver_local(self, agent: str, text: str, sender: str, wait: bool) -> dict[str, Any]:
        c = self.core
        if c.message_agent(agent, text, sender):
            return {"node": self.node_id, "path": [self.node_id], "agent": agent, "delivered": "live",
                    "note": "queued in the live agent's inbox; it acts on it at its next step"}
        # Agent finished or unknown here: start a fresh agent with that role in this node's environment.
        role = agent.rsplit("-", 1)[0] or "Agent"
        run_id = f"msg-{secrets.token_hex(4)}"
        c.codex.emit(run_id, "run.start", "Trinity", prompt=text, mode="message", target=agent, sender=sender)
        job = c.run_agent(run_id, role, f"Message from {sender} to {agent}: {text}")
        if not wait:
            t = asyncio.create_task(job)
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
            return {"node": self.node_id, "path": [self.node_id], "agent": agent, "delivered": "respawned", "run_id": run_id}
        answer = await job
        c.codex.emit(run_id, "run.done", "Trinity")
        return {"node": self.node_id, "path": [self.node_id], "agent": agent, "delivered": "respawned",
                "run_id": run_id, "answer": answer}

    async def handle_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("message payload must be a JSON object")
        hops = int(payload.get("hops", 1))
        if hops > self.max_hops:
            raise PermissionError("max fleet hops exceeded")
        agent, message = payload.get("agent"), payload.get("message")
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("'agent' must be a non-empty string")
        if not isinstance(message, str):
            raise ValueError("'message' must be a string")
        route = payload.get("route") or []
        if not isinstance(route, list):
            raise ValueError("'route' must be a list of node ids")
        return await self.send_message([str(x) for x in route], agent, message,
                                       str(payload.get("from", "?")), hops, payload.get("wait") is True)

    async def run_job(self, kind: str, p: dict[str, Any]) -> dict[str, Any]:
        """Queue handler: retried by the JobQueue if the target node is down.

        The payload is re-checked here: jobs.db is shared by every process on the data dir and
        outlives versions. A job without "task" (or "agent"/"message") failed with KeyError on
        each of its attempts, a non-string task reached squad()/dispatch() as is, and an unknown
        kind ran as a dispatch (round 78). Those now go dead at once."""
        from .jobqueue import PermanentJobError
        if kind not in ("run", "message"):
            raise PermanentJobError(f"unknown job kind {str(kind)[:40]!r}")
        if kind == "message":
            if not all(isinstance(p.get(k), str) and p.get(k) for k in ("agent", "message")):
                raise PermanentJobError("message job needs non-empty string 'agent' and 'message'")
        elif not isinstance(p.get("task"), str) or not p["task"].strip():
            raise PermanentJobError("run job needs a non-empty string 'task'")
        elif len(p["task"].encode("utf-8", "replace")) > QUEUE_TASK_MAX:
            raise PermanentJobError(f"run job task is over {QUEUE_TASK_MAX} bytes")
        if p.get("node") is not None and not isinstance(p.get("node"), str):
            raise PermanentJobError("job 'node' must be a string")
        route = [x for x in str(p.get("node", "local")).split("/") if x and x != "local"]
        if kind == "message":
            return await self.send_message(route, p["agent"], p["message"], p.get("from", self.node_id), 0, True)
        if not route and p.get("node") in ("local", "", None):
            res = await (self.core.chat(p["task"]) if p.get("mode") == "chat" else self.core.squad(p["task"], p.get("n")))
            return {"node": self.node_id, "answer": res.get("answer", "")}
        return await self.dispatch(route, p.get("mode", "squad"), p["task"], 0, p.get("n"))

    async def handle_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Server side of /api/fleet/run: forward if routed further, else run locally."""
        route = [str(x) for x in payload.get("route") or []]
        hops = int(payload.get("hops", 1))
        mode, prompt = str(payload.get("mode", "squad")), str(payload.get("prompt", ""))
        if hops > self.max_hops:
            raise PermissionError("max fleet hops exceeded")
        if payload.get("capability") == NETSQUAD_CAP or payload.get("netsquad") is not None:
            return await self._handle_netsquad(payload, hops)
        priv = ""
        if payload.get("privileged", False) is not False or payload.get("capability") in PRIVILEGED_CAPABILITIES:
            # defense in depth: re-run the gate even though the HTTP path already passed dispatch_gateway
            cap = str(payload.get("capability", ""))
            self._privileged_gate("run", cap, payload.get("privileged") is True, sender_node(payload.get("from")), hops)
            priv = cap
        ck_id = payload.get("checkpoint_id")
        if ck_id is not None:
            if mode not in ("squad", "deep"):
                raise CheckpointError("checkpoint_id works with mode squad or deep")
            ck_id = sanitize_ckpt_id(ck_id)  # CheckpointError -> 400
        if route:
            kw = {"rounds": r} if (r := clamp_deep_rounds(payload.get("rounds"))) is not None else {}
            if ck_id:
                kw["checkpoint_id"] = ck_id
            res = await self.dispatch(route, mode, prompt, hops, payload.get("n"), payload.get("origin_run", ""),
                                      privileged=priv, **kw)
            return {**res, "path": [self.node_id] + res.get("path", [])}
        c = self.core
        self.core.codex.emit("fleet", "fleet.run", self.node_id, mode=mode, frm=payload.get("from"), hops=hops,
                             **({"privileged": priv} if priv else {}), **({"checkpoint_id": ck_id} if ck_id else {}))
        ck: dict[str, Any] = {}
        release = None
        if ck_id:
            # keyed by the (signed) immediate sender and stamped with it: senders can't collide or
            # resume each other's checkpoints; a retried job from the same sender resumes
            sender = sender_node(payload.get("from"))
            ck_path = managed_path(self.data_dir, fleet_key(sender, ck_id))
            check_compatible(ck_path, prompt, mode, owner=f"fleet:{sender}")  # CheckpointConflict -> 409
            release = ckpt_claim(ck_path)
            try:
                stamp_owner(ck_path, f"fleet:{sender}")
            except BaseException:
                release()
                raise
            ck = {"checkpoint": ck_path}
        try:
            # core-wide half (core.fleet_hops / core.fleet_privileged) is ref-counted across every run
            # live on this core, see _run_scope; run-scoped half of the grant: tools spawned in THIS
            # run's task tree see it, a run executing concurrently on the same core does not (priv
            # tools require both halves to agree)
            with self._run_scope(hops, priv), priv_grant(priv):
                if mode == "chat":
                    res = await c.chat(prompt)
                elif mode == "deep":
                    r = clamp_deep_rounds(payload.get("rounds"))
                    res = await c.deep(prompt, None, payload.get("n"), **({"rounds": r} if r is not None else {}),
                                       **ck)
                else:
                    res = await c.squad(prompt, payload.get("n"), **ck)
        finally:
            if release:
                release()
        answer = res.get("answer") or res.get("report") or ""
        return {"node": self.node_id, "path": [self.node_id], "run_id": res.get("run_id"), "answer": answer,
                **({"checkpoint_id": ck_id} if ck_id else {})}


# ---------------------------------------------------------------- agent tools
def _fleet(ctx: "AgentContext") -> FleetManager:
    f = getattr(ctx.core, "fleet", None)
    if f is None or not f.enabled:
        raise RuntimeError("fleet disabled: set AVRONE_FLEET_TOKEN on every node")
    return f


async def t_fleet_nodes(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    rows = [f"this node: {f.node_id} (parent: {f.parent or 'none'})"]
    nodes = f.list_nodes()
    size = 0
    for i, n in enumerate(nodes):
        if size > FLEET_NODES_MAX_CHARS:
            rows.append(f"...[output capped: {i} of {len(nodes)} nodes shown]")
            break
        h = n.get("health") if isinstance(n.get("health"), dict) else {}
        hs = _clip1(f.node_state(n), 32)
        hp = "" if not h else (f" health={hs} score={health_score(n)}" if n["status"] == "online" else f" health={hs}")
        temp = (f" temp={_clip1(h['temp_c'], 16)}C" if h.get("temp_c") is not None and n["status"] == "online" else "")
        caps = n.get("capabilities") if isinstance(n.get("capabilities"), dict) else {}
        kids = n.get("children") if isinstance(n.get("children"), list) else []
        kid_txt = ", ".join(_clip1(k, 64) for k in kids[:FLEET_NODES_CHILDREN_SHOWN])
        if len(kids) > FLEET_NODES_CHILDREN_SHOWN:
            kid_txt += f", ... +{len(kids) - FLEET_NODES_CHILDREN_SHOWN} more"
        row = (f"- {_clip1(n.get('node_id'), 120)} [{_clip1(n['status'], 32)}] load={node_load(n)} "
               f"children=[{kid_txt}] runner={_clip1(caps.get('runner', '?'), 64)}{hp}{temp}")
        rows.append(row)
        size += len(row) + 1
    return "\n".join(rows) if len(rows) > 1 else rows[0] + "\nno child nodes registered"


FLEET_NODES_MAX_CHARS = 200_000
FLEET_NODES_CHILDREN_SHOWN = 16
_CTRL1 = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def _clip1(v: Any, n: int) -> str:
    """A peer-supplied value as one short line: fleet_nodes printed every node's 1,000 children
    and a megabyte `runner` string whole, and a newline in either forged extra node rows."""
    return _CTRL1.sub(" ", "" if v is None else str(v))[:n]


async def t_remote_spawn(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    target = str(args.get("node", "auto"))
    route = [p for p in target.split("/") if p] if target != "auto" else []
    hops = getattr(ctx.core, "fleet_hops", 0)
    priv = str(args.get("privileged") or "")
    if priv and not priv.startswith("priv:"):
        priv = f"priv:{priv}"
    kw = {"rounds": r} if (r := clamp_deep_rounds(args.get("rounds"))) is not None else {}
    if args.get("checkpoint_id"):  # squad/deep: a retried spawn with the same id resumes on that node
        kw["checkpoint_id"] = str(args["checkpoint_id"])
    try:
        res = await f.dispatch(route, str(args.get("mode", "squad")), str(args["task"]), hops, args.get("n"),
                               ctx.run_id, privileged=priv, **kw)
    except GatewayRefused as e:
        if not priv:
            raise
        return f"ERROR: privileged request refused: {e}"
    return f"[{' -> '.join([f.node_id] + res.get('path', []))}] run {res.get('run_id')}\n{res.get('answer', '')}"


async def t_remote_broadcast(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    targets = args.get("nodes") or [n["node_id"] for n in f.list_nodes() if n["status"] == "online"]
    # Normalize each route ("good/", "/good", " good " are all "good") before de-duplicating, and
    # drop empty ones: an empty route would silently become an "auto" pick of some other node.
    routes = ("/".join(p.strip() for p in str(t).split("/") if p.strip()) for t in targets)
    targets = list(dict.fromkeys(r for r in routes if r))[:8]
    if not targets:
        return "ERROR: no online nodes"
    hops = getattr(ctx.core, "fleet_hops", 0)

    async def one(t: str) -> str:
        try:
            r = await f.dispatch([p for p in t.split("/") if p], str(args.get("mode", "squad")), str(args["task"]),
                                 hops, args.get("n"), ctx.run_id)
            return f"## {t}\n{r.get('answer', '')}"
        except Exception as e:  # noqa: BLE001
            return f"## {t}\nERROR: {type(e).__name__}: {e}"
    return "\n\n".join(await asyncio.gather(*(one(t) for t in targets)))


async def t_fleet_agents(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from .presence import directory, render
    f = _fleet(ctx)
    rows = await directory(ctx.core)
    q = str(args.get("filter", "") or "").lower()
    if len(q) > 500:
        return "ERROR: filter is longer than 500 chars"
    live_only = bool(args.get("live_only"))

    def build() -> str:  # filter + render of a 2,000-row tree cost seconds on the event loop
        rs = rows
        if q:
            rs = [{**r, "agents": [a for a in r["agents"] if q in json.dumps(a, default=str).lower()]}
                  for r in rs if q in json.dumps(r, default=str).lower()]
        return f"directory from {f.node_id}:\n" + render(rs, live_only)
    return await asyncio.to_thread(build)


async def t_agent_message(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from .presence import directory, find_agent
    f = _fleet(ctx)
    route, name = find_agent(await directory(ctx.core), str(args["agent"]))
    res = await f.send_message([p for p in route.split("/") if p], name, str(args["message"]),
                               f"{f.node_id}/{ctx.name}", getattr(ctx.core, "fleet_hops", 0), bool(args.get("wait")))
    head = f"[{' -> '.join(res.get('path', []))}] {name}: {res.get('delivered')}"
    return head + (f"\n{res['answer']}" if res.get("answer") else f" ({res.get('note') or res.get('run_id')})")


QUEUE_TASK_MAX = 1_000_000


def _num(v: Any, default: int) -> int:
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(f):
        return default
    return int(max(-2**62, min(2**62, f)))


async def t_queue_submit(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    if f.queue is None:
        return "ERROR: queue not running"
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return "ERROR: task is required (a non-empty string)"
    if len(task.encode("utf-8", "replace")) > QUEUE_TASK_MAX:
        return f"ERROR: task is over {QUEUE_TASK_MAX} bytes; put the detail in a file and reference it"
    payload = {"task": task, "node": str(args.get("node", "auto")), "mode": str(args.get("mode", "squad")),
               "n": args.get("n"), "from": f"{f.node_id}/{ctx.name}"}
    first = payload["node"].split("/", 1)[0]
    try:  # vet at submit time too, so a doomed job never enters the durable queue
        mode = payload["mode"] if payload["mode"] in SPAWN_MODES else "squad"
        f.check_outbound("run", f"spawn:{mode}", getattr(ctx.core, "fleet_hops", 0), first)
    except PermissionError as e:
        return f"ERROR: refused by fleet gateway: {e}"
    from .jobqueue import QueueFull
    try:  # junk priority/max_attempts ("x", NaN, 1e400) used to raise ValueError out of the tool;
        # enqueue() already clamps and defaults them
        jid = await f.queue.enqueue("run", payload, _num(args.get("priority"), 0), _num(args.get("max_attempts"), 3))
    except QueueFull as e:
        return f"ERROR: {e}"
    except (TypeError, ValueError) as e:  # unserializable "n"
        return f"ERROR: job not queued: {e}"
    return f"queued job {jid} -> {payload['node']} (retries on failure; check with queue_status)"


async def t_queue_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    if f.queue is None:
        return "ERROR: queue not running"
    if args.get("job_id"):
        j = await f.queue.get(str(args["job_id"]))
        if not j:
            return "ERROR: no such job"
        res = j.get("result") or {}
        return (f"job {j['id']} {j['status']} attempts={j['attempts']}/{j['max_attempts']}"
                + (f"\nerror: {j['error']}" if j.get("error") and j["status"] != "done" else "")
                + (f"\n{res.get('answer', '')}" if isinstance(res, dict) else ""))
    return json.dumps(await f.queue.stats())


FLEET_TOOLS = [
    ("fleet_agents", "directory of every agent across the fleet: its node route, device, container, networks, services and current task.",
     '{"filter"?: str, "live_only"?: bool}', t_fleet_agents),
    ("agent_message", "send instructions to a specific agent anywhere in the fleet (\"Builder-3\" or \"router/edge/Builder-3\"); a live agent acts on it inside its own environment, a finished one is respawned there.",
     '{"agent": str, "message": str, "wait"?: bool}', t_agent_message),
    ("queue_submit", "durably queue a squad/chat run for a node (route, \"auto\" or \"local\"); retried if the node is down.",
     '{"task": str, "node"?: str, "mode"?: str, "n"?: int, "priority"?: int, "max_attempts"?: int}', t_queue_submit),
    ("queue_status", "status/result of a queued job, or queue counts if no job_id.", '{"job_id"?: str}', t_queue_status),
    ("fleet_nodes", "list linked AVRONE nodes/routers with status, load and their children.", "{}", t_fleet_nodes),
    ("remote_spawn", "spawn a squad/chat/deep run on a remote AVRONE node; node may be a route like \"router1/edge7\" or \"auto\" (healthiest: cool/idle first, hot/throttled skipped). Returns its answer.",
     '{"task": str, "node"?: str, "mode"?: "squad"|"chat"|"deep", "n"?: int, "rounds"?: 1-5 (deep), "privileged"?: "shell"|"admin", "checkpoint_id"?: str (squad/deep, resume on retry)}', t_remote_spawn),
    ("remote_broadcast", "run the same task on several remote nodes in parallel (default: all online, max 8) and return every answer.",
     '{"task": str, "nodes"?: [str], "mode"?: str, "n"?: int}', t_remote_broadcast),
]


def register_fleet_tools(registry) -> None:
    for name, desc, args_doc, fn in FLEET_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
