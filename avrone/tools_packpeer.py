"""pack_list + peer_audit + fleet_forget.

    pack_list {}
        Every agent pack in data/agentpacks/: name, size, and whether it verifies (manifest, sha256s,
        quota, no links/traversal/decompression bomb) - or the exact reason it would be refused on
        import. Verification runs read_pack in memory; nothing is extracted. Read-only.

    peer_audit {}
        Every registered fleet peer checked against today's url rules (check_node_url): a fleet.json
        from before v0.31 may still hold a metadata/link-local/credentialed url this node would POST
        to (since v0.31 every outgoing call refuses such a url). Also shows each peer's status. Read-only:
        the fix is the peer re-registering with a valid url.

    fleet_forget {"node": str}
        Remove one peer from this node's registry (fleet.json). Refuses an online peer - it would
        rejoin on its next heartbeat - unless it is no longer allowed by AVRONE_FLEET_ALLOW, in which
        case removal sticks. Audited (fleet.forget). Acting.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import textsafe as _textsafe
from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

MAX_LIST = 50
PACK_SCAN_MAX = 100_000  # agentpacks/ entries pack_list reads (round 95)


def _data_dir(ctx: "AgentContext") -> Path:
    s = getattr(getattr(ctx, "core", None), "settings", None)
    return Path(getattr(s, "data_dir", "./data"))


def _show(v: Any, n: int = 80) -> str:
    """A name from a file name or a pack manifest, written so it can never start a line of its own
    (round 52: "a\nFORGED.tar.gz" and an agent_id of "x\n- ok.tar.gz: OK" forged pack_list rows)."""
    s = _textsafe.escape(v)
    return s if len(s) <= n else s[:n] + "..."


def _verify(p: Path) -> tuple[str, str]:
    from . import agentpack
    try:
        man, mem, blobs = agentpack.read_pack(agentpack.read_pack_file(p))
    except agentpack.PackError as e:
        return "REFUSED", _show(e, 160)
    except OSError as e:
        return "UNREADABLE", e.__class__.__name__
    except Exception as e:  # noqa: BLE001 - round 52: one odd pack (EOFError, zlib.error, a
        # RecursionError from a hostile manifest) raised out of pack_list and hid every other pack
        return "REFUSED", f"unexpected {e.__class__.__name__} while reading it"
    try:
        aid, origin = man.get("agent_id"), man.get("origin")
        nb, nm = len(blobs), len(mem)
    except Exception:  # noqa: BLE001 - a manifest that is not a dict
        return "REFUSED", "manifest is not an object"
    return "OK", f"agent {_show(aid)} from {_show(origin)}: {nb} file(s), {nm} memory record(s)"


async def t_pack_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from . import agentpack
    d = agentpack.pack_dir(_data_dir(ctx))
    if not await asyncio.to_thread(d.is_dir):
        return "pack_list: no agent packs yet (agent_pack_export creates them)"

    def listing() -> list[tuple[Path, bool, int]]:
        # round 95: iterdir + sort + a stat per pack ran on the event loop; one bounded scandir now
        from .safeio import TreeWalk
        out = []
        for _rel, de in TreeWalk(d, PACK_SCAN_MAX, recurse=False):
            if not de.name.endswith(".tar.gz") or de.name.startswith("."):
                continue
            try:
                link = de.is_symlink()
                size = -1 if link else de.stat(follow_symlinks=False).st_size
            except OSError:
                link, size = False, -1
            out.append((Path(de.path), link, size))
        return out
    entries = await asyncio.to_thread(listing)
    files = [p for p, _, _ in entries]
    meta = {p: (link, size) for p, link, size in entries}
    if not files:
        return "pack_list: no agent packs yet (agent_pack_export creates them)"
    lines = [f"pack_list: {len(files)} pack(s) in agentpacks/" + (f", first {MAX_LIST} shown" if len(files) > MAX_LIST else "")]
    bad = 0
    for p in files[:MAX_LIST]:
        link, size = meta[p]
        if link:
            lines.append(f"- {_show(p.name, 200)}: REFUSED - symlink (import refuses links)")
            bad += 1
            continue
        verdict, detail = await asyncio.to_thread(_verify, p)
        bad += verdict != "OK"
        sz = f"{size} bytes" if size >= 0 else "size unknown"
        lines.append(f"- {_show(p.name, 200)} ({sz}): {verdict} - {detail}")
    if bad:
        lines.append(f"{bad} pack(s) would be refused on import; delete or re-export them")
    return "\n".join(lines)


async def t_peer_audit(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from .fleet import check_node_url
    f = getattr(getattr(ctx, "core", None), "fleet", None)
    if f is None or not getattr(f, "enabled", False):
        return "peer_audit: fleet disabled on this node (no AVRONE_FLEET_TOKEN)"
    raw = getattr(f, "nodes", None)
    nodes = dict(raw) if isinstance(raw, dict) else {}  # round 52: a list raised out of dict()
    if not nodes:
        return "peer_audit: no registered peers"
    lines, bad = [], 0
    # round 52: ids are sorted as text (a non-str key from a hand-edited registry raised TypeError)
    for nid in sorted(nodes, key=lambda k: (not isinstance(k, str), str(k))):
        n = nodes[nid] if isinstance(nodes[nid], dict) else {}
        try:
            status = _show(f.status_of(n), 20)
        except Exception:  # noqa: BLE001
            status = "unknown"
        try:
            check_node_url(n.get("url"))
            verdict = "ok"
        except (ValueError, TypeError) as e:
            verdict = f"BAD URL - {e}" if isinstance(e, ValueError) else "BAD URL - not a url"
            bad += 1
        # round 52: a peer id with "\n" (an old fleet.json) printed a forged row
        lines.append(f"- {_show(nid, 120)} [{status}]: {verdict}")
    head = f"peer_audit: {len(nodes)} peer(s), {bad} with a url this node should not call"
    tail = ["calls to these peers are refused; have each re-register with a valid url"] if bad else []
    return "\n".join([head, *lines, *tail])


async def t_fleet_forget(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = getattr(getattr(ctx, "core", None), "fleet", None)
    if f is None or not getattr(f, "enabled", False):
        return "ERROR: fleet disabled on this node (no AVRONE_FLEET_TOKEN)"
    nid = args.get("node")
    if not isinstance(nid, str) or not nid.strip():
        return "ERROR: node is required (a peer id from fleet_status / peer_audit)"
    nid = nid.strip()
    try:
        # a peer no longer allowed cannot re-register, so removing it while online is safe and final
        revoked = not f.allowed(nid)
        r = f.forget(nid, allow_online=revoked)
    except KeyError:
        return f"ERROR: no peer {_show(nid, 120)!r} registered here"
    except (ValueError, PermissionError) as e:
        from .tools import error_text  # round 115: a PermissionError named the host path
        return error_text(e, ctx) if isinstance(e, OSError) and e.filename else f"ERROR: {e}"
    note = " (revoked in AVRONE_FLEET_ALLOW, so it cannot rejoin)" if revoked else ""
    return f"forgot peer {_show(r.get('node', nid), 120)} (was {_show(r.get('status', '?'), 20)}){note}"


PACKPEER_TOOLS = [
    Tool("pack_list", "read-only: list agent packs and verify each in memory (or say why import would refuse it).",
         "{}", t_pack_list, source="builtin", schema={}),
    Tool("peer_audit", "read-only: check every registered fleet peer's url against current rules (no metadata/"
         "link-local/credentials) plus its status. Never prints urls.", "{}", t_peer_audit, source="builtin", schema={}),
    Tool("fleet_forget", "acting: remove one fleet peer from this node's registry; refuses an online peer that "
         "is still allowed (it would rejoin).", '{"node": "<peer id>"}', t_fleet_forget, source="builtin",
         schema={"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}),
]


def register_packpeer_tools(registry) -> None:
    for t in PACKPEER_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
