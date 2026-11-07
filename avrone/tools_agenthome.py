"""agent_home: a read-only, bounded report on the calling agent's OWN home.

    agent_home {"format"?: "text"|"json"}

Reports, for ctx.agent_id only (no argument can name another agent):
    path        the home dir relative to the workspace (agents/<id>), whether it exists
    usage       bytes used vs the AVRONE_AGENT_FS_QUOTA_MB quota (and the per-file cap from
                AVRONE_AGENT_FS_MAX_FILE_MB), regular-file count; symlinks are counted but never
                followed (lstat only), special files counted; the walk stops after SCAN_MAX_ENTRIES
    container   AVRONE_AGENT_CONTAINER mode, the runner kind, whether a docker binary is on PATH,
                whether the home is container-hosted, the expected container name, and this agent's
                live pool record (uses, idle seconds) when the runner holds one
    pack        this node, the agent's home node (agentpack index), its own provenance record
                (imported / migrated), and the newest of its own packs in data_dir/agentpacks/
Nothing here writes, spawns, networks or reads file contents. Output is capped at OUT_MAX_CHARS.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat as _st
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import agentcontainer, agentfs, agentpack
from .agentfs_tools import max_file_bytes, quota_bytes
from .tools import Tool, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext

SCAN_MAX_ENTRIES = 50_000
OUT_MAX_CHARS = 4000
PACKS_MAX_SCAN = 2000
_RECORD_KEYS = ("home", "origin", "created", "imported", "migrated_to", "migrated_stamp", "migrated_at")


def _clean(v: Any, n: int = 80) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v if v == v else None
    s = re.sub(r"[\x00-\x1f\x7f]", "?", str(v))
    return s if len(s) <= n else s[:n] + "..."


def _settings(ctx: "AgentContext") -> tuple[Path, Path]:
    s = getattr(ctx.core, "settings", None)
    return Path(getattr(s, "workspace", "./workspace")), Path(getattr(s, "data_dir", "./data"))


def _node(ctx: "AgentContext") -> str:
    nid = getattr(getattr(ctx.core, "fleet", None), "node_id", None)
    return nid if isinstance(nid, str) and nid else agentcontainer.local_node_id()


def scan_home(d: Path, limit: int = SCAN_MAX_ENTRIES) -> dict[str, Any]:
    """Bytes / files under `d` by lstat, never following a symlink; stops after `limit` entries."""
    out = {"bytes": 0, "files": 0, "dirs": 0, "symlinks": 0, "special": 0, "truncated": False}
    seen = 0
    for cur, dirs, files in os.walk(d, followlinks=False):
        for name in dirs + files:
            seen += 1
            if seen > limit:
                out["truncated"] = True
                return out
            try:
                st = os.lstat(os.path.join(cur, name))
            except OSError:
                continue
            if _st.S_ISLNK(st.st_mode):
                out["symlinks"] += 1
            elif _st.S_ISDIR(st.st_mode):
                out["dirs"] += 1
            elif _st.S_ISREG(st.st_mode):
                out["files"] += 1
                out["bytes"] += st.st_size
            else:
                out["special"] += 1
        # os.walk(followlinks=False) never descends into a symlinked dir; nothing else to prune
    return out


def _container(ctx: "AgentContext", aid: str, node: str) -> dict[str, Any]:
    mode = agentcontainer.container_mode()
    runner = getattr(ctx.core, "runner", None)
    kind = getattr(runner, "kind", None)
    docker = shutil.which("docker") is not None
    pool = getattr(runner, "agent_pool", None)
    pool_node = getattr(pool, "node", None) if pool is not None else None
    name = agentcontainer.container_name(pool_node if isinstance(pool_node, str) and pool_node else node, aid)
    live = None
    homes = getattr(pool, "homes", None) if pool is not None else None
    if isinstance(homes, dict) and name in homes:  # only THIS agent's record is ever looked up
        h = homes[name]
        try:
            idle = round(float(pool.clock()) - float(h.last_used), 1)
        except Exception:  # noqa: BLE001 - an odd clock never breaks a read-only report
            idle = None
        live = {"uses": int(getattr(h, "uses", 0) or 0), "idle_s": idle}
    return {"mode": mode, "runner": _clean(kind, 20), "docker_present": docker,
            "hosted": mode == "persistent" and kind == "docker", "name": name, "live": live,
            "idle_reap_s": agentcontainer.idle_secs()}


def _packs(data: Path, aid: str) -> dict[str, Any]:
    rx = re.compile(re.escape(aid) + r"-(\d{1,12})\.tar\.gz")
    newest, count = None, 0
    d = agentpack.pack_dir(data)
    if not d.is_dir() or d.is_symlink():
        return {"count": 0, "newest": None}
    try:
        with os.scandir(d) as it:
            for i, e in enumerate(it):
                if i >= PACKS_MAX_SCAN:
                    break
                m = rx.fullmatch(e.name)
                if not m or e.is_symlink() or not e.is_file(follow_symlinks=False):
                    continue
                count += 1
                created = int(m.group(1))
                if newest is None or created > newest["created"]:
                    newest = {"name": e.name, "created": created,
                              "bytes": e.stat(follow_symlinks=False).st_size}
    except OSError:
        pass
    return {"count": count, "newest": newest}


def agent_home(ctx: "AgentContext") -> dict[str, Any]:
    aid = getattr(ctx, "agent_id", None)
    if not aid:
        raise PermissionError("you have no agent home (per-agent fs is disabled: AVRONE_AGENT_FS=0)")
    aid = agentfs.sanitize_agent_id(aid)
    ws, data = _settings(ctx)
    node = _node(ctx)
    root = agentfs.agents_root(ws)
    d = root / aid
    rel = f"{agentfs.AGENTS_SUBDIR}/{aid}"
    quota, cap = quota_bytes(), max_file_bytes()
    rep: dict[str, Any] = {"agent_id": aid, "path": rel, "exists": False, "quota_bytes": quota,
                           "max_file_bytes": cap}
    if root.is_symlink() or d.is_symlink():
        rep["error"] = "home is a symlink (refused; not followed)"
        usage = None
    elif d.is_dir():
        rep["exists"] = True
        usage = scan_home(d)
    else:
        usage = {"bytes": 0, "files": 0, "dirs": 0, "symlinks": 0, "special": 0, "truncated": False}
    if usage is not None:
        rep.update(usage)
        rep["used_pct"] = round(100.0 * usage["bytes"] / quota, 1) if quota else None
        rep["over_quota"] = usage["bytes"] > quota
        rep["free_bytes"] = max(0, quota - usage["bytes"])
    rep["container"] = _container(ctx, aid, node)
    rec = agentpack.load_index(data).get(aid)
    rec = {k: _clean(rec[k]) for k in _RECORD_KEYS if isinstance(rec, dict) and k in rec}
    rep["pack"] = {"node": node, "home_node": _clean(agentpack.home_node(data, aid, node)),
                   "record": rec or None, **_packs(data, aid)}
    rep["pack"]["born_here"] = rep["pack"]["home_node"] == node
    return rep


def _cap(text: str) -> str:
    return text if len(text) <= OUT_MAX_CHARS else text[:OUT_MAX_CHARS] + "\n...[truncated]"


def _fmt_bytes(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MB" if n >= 1024 * 1024 else f"{n} B"


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_agent_home(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        rep = agent_home(ctx)
    except PermissionError as e:
        return f"ERROR: {e}"
    if str((args or {}).get("format", "text")).lower() == "json":
        return _cap(json.dumps(rep, indent=1, sort_keys=True))
    lines = [f"agent_home {rep['agent_id']} (your own; read-only): {rep['path']}"
             + ("" if rep["exists"] else " (not created yet)")]
    if rep.get("error"):
        lines.append(f"ERROR: {rep['error']}")
    else:
        lines.append(f"usage: {_fmt_bytes(rep['bytes'])} of {_fmt_bytes(rep['quota_bytes'])} quota "
                     f"({rep['used_pct']}%), {rep['files']} files, {rep['dirs']} dirs"
                     + (" - OVER QUOTA" if rep["over_quota"] else "")
                     + (" (scan truncated)" if rep["truncated"] else ""))
        if rep["symlinks"] or rep["special"]:
            lines.append(f"warning: {rep['symlinks']} symlink(s), {rep['special']} special file(s) "
                         "(not followed; agent_pack_export refuses a home holding these)")
        lines.append(f"per-file cap: {_fmt_bytes(rep['max_file_bytes'])}")
    c = rep["container"]
    lines.append(f"container: mode={c['mode']} runner={c['runner']} docker={'yes' if c['docker_present'] else 'no'} "
                 f"hosted={'yes' if c['hosted'] else 'no'} name={c['name']}"
                 + (f" live(uses={c['live']['uses']}, idle={c['live']['idle_s']}s)" if c["live"] else ""))
    p = rep["pack"]
    lines.append(f"node: {p['node']}  home node: {p['home_node']}"
                 + ("" if p["born_here"] else " (imported from elsewhere)"))
    if p["record"]:
        lines.append("pack record: " + ", ".join(f"{k}={v}" for k, v in sorted(p["record"].items())))
    if p["newest"]:
        lines.append(f"newest pack: {p['newest']['name']} ({p['newest']['bytes']} B; {p['count']} total)")
    else:
        lines.append("no packs exported yet (agent_pack_export)")
    return _cap("\n".join(lines))


AGENTHOME_TOOLS: list[Tool] = [
    Tool("agent_home",
         "read-only: report on YOUR OWN agent home: path (relative to the workspace), bytes used vs the "
         "AVRONE_AGENT_FS_QUOTA_MB quota, file count, whether it is container-hosted (and docker present), "
         "your home node and last pack export/import/migration record. Symlinks are never followed.",
         '{"format"?: "text"|"json"}', t_agent_home, source="builtin",
         schema={"optional": {"format": "str"}}),
]


def register_agenthome_tools(registry) -> None:
    for t in AGENTHOME_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
