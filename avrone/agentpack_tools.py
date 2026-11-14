"""Agent-home tools (edgepack): pack, restore and migrate YOUR OWN agent home.

    agent_pack_export  {}                      bundle your dir + memory into data_dir/agentpacks/
    agent_pack_import  {"name": str}           restore one of YOUR packs from data_dir/agentpacks/
                                               (local file only, never a URL; must be your agent id)
    agent_migrate      {"target"?: str}        send your home to a peer node ("auto" = coolest/least
                                               loaded); needs AVRONE_AGENT_MIGRATE=on and the fleet

All three act only on the calling agent's own id (ctx.agent_id) and are audited as agentpack.*.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import agentcontainer, agentpack
from .tools import Tool, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext

PACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}-\d{1,12}\.tar\.gz\Z")


def _settings(ctx: "AgentContext") -> tuple[Path, Path]:
    s = getattr(ctx.core, "settings", None)
    return Path(getattr(s, "workspace", "./workspace")), Path(getattr(s, "data_dir", "./data"))


def _node(ctx: "AgentContext") -> str:
    f = getattr(ctx.core, "fleet", None)
    nid = getattr(f, "node_id", None)
    return nid if isinstance(nid, str) and nid else agentcontainer.local_node_id()


def _agent(ctx: "AgentContext") -> str:
    aid = getattr(ctx, "agent_id", None)
    if not aid:
        raise PermissionError("you have no agent home (per-agent fs is disabled: AVRONE_AGENT_FS=0)")
    return str(aid)


def _shown(v: Any, cap: int) -> str:
    """Untrusted text as one masked line of at most cap chars."""
    from .tools import one_line
    from .tools_researchnote import redact_text
    try:
        t = redact_text(one_line("" if v is None else v))
    except Exception:  # noqa: BLE001 - fail closed
        return "(unprintable)"
    return t if len(t) <= cap else t[:cap] + "..."


def _why(e: BaseException) -> str:
    """type and errno name of an OSError, never its path or message (round 68)."""
    import errno
    code = getattr(e, "errno", None)
    return type(e).__name__ + (f" {errno.errorcode.get(code, code)}" if isinstance(code, int) else "")


def _audit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    codex = getattr(ctx.core, "codex", None)
    if codex is None:
        return
    try:
        codex.emit(ctx.run_id, kind, ctx.name, agent_id=getattr(ctx, "agent_id", None), **data)
    except Exception:  # noqa: BLE001
        pass


@off_loop  # export/import of a 50 MB home took 0.6-1.6 s on the event loop
async def t_agent_pack_export(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        aid = _agent(ctx)
        ws, data = _settings(ctx)
        node = _node(ctx)
        bundle, man = agentpack.export_pack(ws, data, aid, node, home=agentpack.home_node(data, aid, node))
    except (PermissionError, agentpack.PackError) as e:
        _audit(ctx, "agentpack.refused", tool="agent_pack_export", reason=str(e)[:300])
        return f"ERROR: {e}"
    except OSError as e:  # round 68: an unreadable file in the home raised out of the tool unaudited
        _audit(ctx, "agentpack.failed", tool="agent_pack_export", reason=_why(e))
        return f"ERROR: could not read your home to pack it ({_why(e)})"
    name = f"{aid}-{man['created']}.tar.gz"
    try:
        out_dir = agentpack.pack_dir(data)
        out_dir.mkdir(parents=True, exist_ok=True)
        from .safeio import atomic_write
        atomic_write(out_dir / name, bundle)
    except OSError as e:  # round 68: a full disk / read-only data dir raised, and nothing was audited
        _audit(ctx, "agentpack.failed", tool="agent_pack_export", name=name, reason=_why(e))
        return f"ERROR: could not write the pack ({_why(e)}); nothing was saved"
    digest = hashlib.sha256(bundle).hexdigest()
    _audit(ctx, "agentpack.export", name=name, bytes=len(bundle), files=len(man["files"]), sha256=digest)
    return (f"packed agent {aid}: {name} ({len(bundle)} bytes, {len(man['files'])} files, "
            f"{man['memory']['entries']} memories) sha256={digest}")


@off_loop  # export/import of a 50 MB home took 0.6-1.6 s on the event loop
async def t_agent_pack_import(ctx: "AgentContext", args: dict[str, Any]) -> str:
    raw = args.get("name", "")
    if not isinstance(raw, str):  # round 68: {"name": {...}} was str()-ed into the audit and error
        return "ERROR: name must be text (a pack name like <your-id>-<created>.tar.gz)"
    name = raw.strip()
    try:
        aid = _agent(ctx)
        if not PACK_NAME_RE.match(name) or not name.startswith(f"{aid}-"):
            raise PermissionError(f"name must be one of YOUR packs in agentpacks/ ({aid}-<created>.tar.gz)")
        ws, data = _settings(ctx)
        p = agentpack.pack_dir(data) / name
        if p.is_symlink() or not p.is_file():
            raise PermissionError(f"no such pack: {name}")
        res = agentpack.import_pack(ws, data, agentpack.read_pack_file(p), _node(ctx), expect_agent=aid)
    except (PermissionError, agentpack.PackError) as e:
        _audit(ctx, "agentpack.refused", tool="agent_pack_import", name=_shown(name, 100), reason=str(e)[:300])
        return f"ERROR: {_shown(str(e), 500)}"
    except OSError as e:  # round 68: a pack unreadable or vanished between the check and the read
        _audit(ctx, "agentpack.failed", tool="agent_pack_import", name=_shown(name, 100), reason=_why(e))
        return f"ERROR: could not read pack {_shown(name, 100)} ({_why(e)}); your home is unchanged"
    _audit(ctx, "agentpack.import", name=name, files=res["files"], bytes=res["bytes"])
    m = res["memory"]
    return (f"restored agent {aid} from {name}: {res['files']} files, memory merged={m['merged']} "
            f"skipped={m['skipped']} rejected={m['rejected']}")


async def t_agent_migrate(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from .fleet import migrate_enabled
    if not migrate_enabled():
        return "ERROR: agent migration is disabled (set AVRONE_AGENT_MIGRATE=on)"
    f = getattr(ctx.core, "fleet", None)
    if f is None or not getattr(f, "enabled", False):
        return "ERROR: fleet disabled: agent_migrate needs AVRONE_FLEET_TOKEN"
    target = args.get("target", "auto")
    if target is None:
        target = "auto"
    if not isinstance(target, str):  # round 68: {"target": ["n1"]} was str()-ed into a node lookup
        return "ERROR: target must be text (a peer node id, or \"auto\")"
    try:
        res = await f.migrate_agent(_agent(ctx), target.strip()[:200] or "auto")
    except Exception as e:  # noqa: BLE001 - audited by migrate_agent; the home stays here
        # round 68: the peer's HTTP error body was echoed raw, so a peer could forge extra lines
        # (or reflect a header back) in the model's tool result
        return f"ERROR: migration refused or failed (your home stays on this node): {_shown(str(e), 500)}"
    return (f"migrated agent {_shown(res.get('agent_id'), 64)} home to {_shown(res.get('target'), 80)} "
            f"({res.get('files')} files, {res.get('bytes')} bytes)")


AGENTPACK_TOOLS = [
    ("agent_pack_export", "bundle YOUR agent home (persistent dir + memory) into a verified, deterministic pack "
     "under agentpacks/; returns its name and sha256.", "{}", t_agent_pack_export),
    ("agent_pack_import", "restore one of YOUR OWN agent packs (by name, from agentpacks/); hashes and paths are "
     "verified and it only replaces your home with a newer copy.", '{"name": str}', t_agent_pack_import),
    ("agent_migrate", "move YOUR agent home to a healthy peer node over the signed fleet gateway (needs "
     "AVRONE_AGENT_MIGRATE=on); target \"auto\" picks the coolest / least-loaded node.",
     '{"target"?: str}', t_agent_migrate),
]


def register_agentpack_tools(registry) -> None:
    for name, desc, args_doc, fn in AGENTPACK_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
