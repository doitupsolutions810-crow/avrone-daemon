"""state_health + registry_audit: what an agent (or the self-evolve loop) can see about its own
node's durability and its own toolset. Both read-only, both bounded.

    state_health {"verbose"?: bool}
        Scans this node's data_dir state (fleet.json, evolve proposals + loops, checkpoints,
        agentpack index, memory.jsonl, run bridges) for files that are unreadable, torn (JSONL last
        line without a newline / undecodable lines) or set aside as *.corrupt-*. Reports counts per
        area and the first problems by relative path; never prints file contents.

    registry_audit {"source"?: str}
        The registered tools by source, which ones have no argument schema (models get no
        pre-call ARG ERROR for them), and how many are read-only vs acting. A worklist for
        hardening, and a map of the agent's reach.
"""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext

MAX_FILES_PER_AREA = 2000
MAX_PROBLEMS_SHOWN = 15
MAX_JSONL_BYTES = 8 * 1024 * 1024
AREA_SCAN_MAX = 50_000        # directory entries read per area (round 94)
CORRUPT_SCAN_MAX = 200_000    # entries the *.corrupt-* sweep reads over the whole data_dir
# tools that change something outside the agent's own reply (files, processes, network, other nodes)
ACTING = {"run_logs", "fleet_forget", "secret_scan", "shell", "python", "git", "write_file", "edit_file", "fs_write", "fs_delete", "env_install", "docker_run",
          "spawn", "spawn_agent", "spawn_squad", "remote_spawn", "remote_spawn_many", "priv_shell", "priv_admin",
          "evolve_propose", "evolve_skill_propose", "evolve_start", "evolve_loop", "evolve_reset", "evolve_cancel",
          "memory_save", "memory_sync", "agent_migrate", "agent_pack_export", "agent_pack_import", "artifact_save",
          "task_board", "bridge_post", "bridge_reply", "research_note", "lesson_forget", "http_request",
          "fleet_message", "queue_submit", "port_scan"}


def _check_json(p: Path) -> str | None:
    from .safeio import FileTooBig, read_capped
    try:
        raw = read_capped(p, MAX_JSONL_BYTES)
        if raw is None:
            return "not a regular file"
        json.loads(raw.decode("utf-8"))
    except FileTooBig:
        return None  # too big to check here; not a problem in itself
    except UnicodeDecodeError:
        return "not utf-8"
    except (ValueError, RecursionError):
        return "invalid json"
    except OSError as e:
        return f"unreadable ({type(e).__name__})"
    return None


def _check_jsonl(p: Path) -> str | None:
    # round 94: stat() + read_bytes() followed a symlink to any host file and hung on a FIFO
    # swapped in after the listing; read_capped opens O_NOFOLLOW|O_NONBLOCK, regular files only.
    from .safeio import FileTooBig, read_capped
    try:
        data = read_capped(p, MAX_JSONL_BYTES)
    except FileTooBig:
        return None  # too big to scan here; not a problem in itself
    except OSError as e:
        return f"unreadable ({type(e).__name__})"
    if data is None:
        return "not a regular file"
    if data and not data.endswith(b"\n"):
        return "torn last line"
    bad = 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
        except (ValueError, UnicodeDecodeError):
            bad += 1
    return f"{bad} undecodable line(s)" if bad else None


def _areas(data_dir: Path) -> list[tuple[str, list[Path], Any]]:
    from .safeio import TreeWalk

    def g(d: Path, pat: str) -> list[Path]:
        # round 94: one bounded level (was sorted(glob) of the whole dir before the cap). Symlinks
        # and special files are listed so the check reports them, never followed or opened.
        out: list[Path] = []
        for _rel, de in TreeWalk(d, AREA_SCAN_MAX, recurse=False):
            if len(out) >= MAX_FILES_PER_AREA:
                break
            try:
                if de.name.startswith(".") or de.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if fnmatch.fnmatchcase(de.name, pat):
                out.append(Path(de.path))
        return out

    def one(p: Path) -> list[Path]:
        return [p] if os.path.lexists(p) and not p.is_dir() else []
    fleet = one(data_dir / "fleet.json")
    mem = one(data_dir / "memory.jsonl")
    return [("fleet", fleet, _check_json),
            ("evolve proposals", g(data_dir / "evolve", "*.json"), _check_json),
            ("evolve loops", g(data_dir / "evolve" / "loops", "*.json"), _check_json),
            ("checkpoints", g(data_dir / "checkpoints", "*.json"), _check_json),
            ("agentpacks", g(data_dir / "agentpacks", "index.json"), _check_json),
            ("memory", mem, _check_jsonl),
            ("bridges", g(data_dir / "bridge", "*.jsonl"), _check_jsonl)]


def state_health(data_dir: Any) -> dict[str, Any]:
    root = Path(data_dir)
    areas: dict[str, dict[str, int]] = {}
    problems: list[dict[str, str]] = []
    for name, files, check in _areas(root):
        bad = 0
        for f in files:
            why = check(f)
            if why:
                bad += 1
                problems.append({"area": name, "path": str(f.relative_to(root)), "problem": why})
        areas[name] = {"files": len(files), "bad": bad}
    # round 94: rglob walked the whole data_dir (every run log, every evolve worktree) unbounded
    from .safeio import TreeWalk
    # runs/ holds only codex logs (never set aside as *.corrupt-*) and can hold 100k+ files
    walk = TreeWalk(root, CORRUPT_SCAN_MAX, skip=lambda n: n in (".git", "runs"))
    corrupt = [rel for rel, de in walk if fnmatch.fnmatchcase(de.name, "*.corrupt-*")] if root.is_dir() else []
    return {"data_dir_exists": root.is_dir(), "areas": areas, "problems": problems,
            "set_aside": corrupt[:MAX_PROBLEMS_SHOWN], "set_aside_count": len(corrupt),
            "set_aside_truncated": walk.truncated}


@off_loop  # round 94: up to 7 x 2000 file reads plus a data_dir walk ran on the event loop
async def t_state_health(ctx: "AgentContext", args: dict[str, Any]) -> str:
    data_dir = getattr(getattr(getattr(ctx, "core", None), "settings", None), "data_dir", None)
    if data_dir is None:
        return "state_health: no data_dir configured on this node"
    st = state_health(data_dir)
    if not st["data_dir_exists"]:
        return "state_health: data_dir does not exist yet (nothing persisted)"
    verbose = args.get("verbose") is True
    total_bad = sum(a["bad"] for a in st["areas"].values())
    head = "HEALTHY" if not total_bad and not st["set_aside_count"] else "ATTENTION"
    more = "+" if st.get("set_aside_truncated") else ""
    lines = [f"state_health: {head} ({total_bad} bad file(s), {st['set_aside_count']}{more} set aside as *.corrupt-*)"]
    for name, a in st["areas"].items():
        if a["files"] or verbose:
            lines.append(f"- {name}: {a['files']} file(s), {a['bad']} bad")
    for p in st["problems"][:MAX_PROBLEMS_SHOWN]:
        lines.append(f"  ! {p['path']}: {p['problem']}")
    if len(st["problems"]) > MAX_PROBLEMS_SHOWN:
        lines.append(f"  ... {len(st['problems']) - MAX_PROBLEMS_SHOWN} more")
    for p in st["set_aside"]:
        lines.append(f"  set aside: {p}")
    return "\n".join(lines)


def registry_audit(registry: Any, source: str | None = None) -> dict[str, Any]:
    names = sorted(registry.names())
    by_source: dict[str, int] = {}
    no_schema: list[str] = []
    acting = 0
    for n in names:
        t = registry.get(n)
        src = str(getattr(t, "source", "") or "unknown")
        if source and src != source:
            continue
        by_source[src] = by_source.get(src, 0) + 1
        if getattr(t, "schema", None) is None:
            no_schema.append(n)
        acting += n in ACTING
    shown = sum(by_source.values())
    return {"tools": shown, "by_source": by_source, "no_schema": no_schema, "acting": acting,
            "read_only": shown - acting}


async def t_registry_audit(ctx: "AgentContext", args: dict[str, Any]) -> str:
    reg = getattr(getattr(ctx, "core", None), "tools", None)
    if reg is None or not hasattr(reg, "names"):
        return "registry_audit: no tool registry on this core"
    src = args.get("source")
    src = str(src).strip() if isinstance(src, str) and src.strip() else None
    a = registry_audit(reg, src)
    if not a["tools"]:
        return f"registry_audit: no tools from source {src!r}"
    lines = [f"registry_audit: {a['tools']} tool(s){' from ' + src if src else ''}; "
             f"{a['acting']} acting, {a['read_only']} read-only",
             "by source: " + ", ".join(f"{k}={v}" for k, v in sorted(a["by_source"].items())),
             f"no arg schema ({len(a['no_schema'])}): " + (", ".join(a["no_schema"][:60]) or "none")]
    return "\n".join(lines)


STATE_HEALTH_TOOLS: list[Tool] = [
    Tool("state_health", "read-only: scan this node's persisted state (fleet, evolve, checkpoints, agentpacks, "
         "memory, bridges) for unreadable/torn/set-aside files. Never prints contents.",
         '{"verbose"?: bool}', t_state_health, source="builtin", schema={"optional": {"verbose": "bool"}}),
    Tool("registry_audit", "read-only: your toolset by source, acting vs read-only, and tools with no arg schema.",
         '{"source"?: str}', t_registry_audit, source="builtin", schema={"optional": {"source": "str"}}),
]


def register_statehealth_tools(registry) -> None:
    for t in STATE_HEALTH_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
