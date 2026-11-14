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

from .safeio import json_loads as _jloads  # round 111: RecursionError -> JSONDecodeError
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .textsafe import escape as _escape
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
          "fleet_message", "queue_submit", "port_scan",
          # round 65: tools that send traffic to a third-party host count as acting (the definition
          # above: network). port_scan and http_request were here, but tcp_ping, traceroute, fetch
          # and the recon lookups were counted read-only, so the "map of the agent's reach"
          # hid its outbound reach.
          "fetch", "web_search", "dns_lookup", "http_fingerprint", "ip_geo", "osint_domain", "rdap_whois",
          "tcp_ping", "tls_inspect", "traceroute"}


def is_acting(name: str, tool: Any = None) -> bool:
    """In ACTING, or a tool that says so itself (description "acting: ..."), so a newly added
    acting tool is not silently counted read-only until someone edits the set (round 65)."""
    if name in ACTING:
        return True
    d = getattr(tool, "description", None)
    return isinstance(d, str) and d.lstrip().lower().startswith("acting")


def _check_json(p: Path) -> str | None:
    from .safeio import FileTooBig, read_capped
    try:
        raw = read_capped(p, MAX_JSONL_BYTES)
        if raw is None:
            return "not a regular file"
        _jloads(raw.decode("utf-8"))
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
    # round 54: bytes.splitlines() also split on a bare "\r" (JSON whitespace), so one valid record
    # was counted as two undecodable halves; every JSONL reader here splits on "\n" only
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            _jloads(line)
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
    try:
        root = Path(data_dir)
    except TypeError:  # round 54: a non-path data_dir setting raised out of the tool
        return {"data_dir_exists": False, "areas": {}, "problems": [], "set_aside": [],
                "set_aside_count": 0, "set_aside_truncated": False}
    areas: dict[str, dict[str, int]] = {}
    problems: list[dict[str, str]] = []
    for name, files, check in _areas(root):
        bad = 0
        for f in files:
            why = check(f)
            if why:
                bad += 1
                try:
                    rel = str(f.relative_to(root))
                except ValueError:
                    rel = f.name
                problems.append({"area": name, "path": rel, "problem": why})
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
    if data_dir is None or not isinstance(data_dir, (str, os.PathLike)) or not str(data_dir):
        return "state_health: no data_dir configured on this node"
    st = state_health(data_dir)
    if not st["data_dir_exists"]:
        return "state_health: data_dir does not exist yet (nothing persisted)"
    from .argnum import tool_bool  # round 65: "true" as a string was ignored
    verbose = tool_bool(args.get("verbose"), False) is True
    total_bad = sum(a["bad"] for a in st["areas"].values())
    head = "HEALTHY" if not total_bad and not st["set_aside_count"] else "ATTENTION"
    more = "+" if st.get("set_aside_truncated") else ""
    lines = [f"state_health: {head} ({total_bad} bad file(s), {st['set_aside_count']}{more} set aside as *.corrupt-*)"]
    for name, a in st["areas"].items():
        if a["files"] or verbose:
            lines.append(f"- {name}: {a['files']} file(s), {a['bad']} bad")
    for p in st["problems"][:MAX_PROBLEMS_SHOWN]:
        # round 54: file names are chosen by whoever wrote them; "x\nstate_health: HEALTHY" printed
        # a forged line of its own, and U+202E reversed the row
        lines.append(f"  ! {_escape(p['path'])}: {p['problem']}")
    if len(st["problems"]) > MAX_PROBLEMS_SHOWN:
        lines.append(f"  ... {len(st['problems']) - MAX_PROBLEMS_SHOWN} more")
    for p in st["set_aside"]:
        lines.append(f"  set aside: {_escape(p)}")
    return "\n".join(lines)


def registry_audit(registry: Any, source: str | None = None) -> dict[str, Any]:
    try:
        names = sorted(str(n) for n in registry.names())
    except Exception:  # noqa: BLE001 - round 54: a broken registry listing fails soft
        names = []
    by_source: dict[str, int] = {}
    no_schema: list[str] = []
    acting = 0
    for n in names:
        try:
            t = registry.get(n)
        except Exception:  # noqa: BLE001
            t = None
        if t is None:  # round 54: a name with no tool behind it was counted as source "unknown"
            continue
        src = str(getattr(t, "source", "") or "unknown")
        if source and src != source:
            continue
        by_source[src] = by_source.get(src, 0) + 1
        if getattr(t, "schema", None) is None:
            no_schema.append(n)
        acting += is_acting(n, t)
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
        return f"registry_audit: no tools from source {_escape(src)!r}"
    # round 54: plugin sources and tool names are a plugin author's choice; escape them on display
    lines = [f"registry_audit: {a['tools']} tool(s){' from ' + _escape(src) if src else ''}; "
             f"{a['acting']} acting, {a['read_only']} read-only",
             "by source: " + ", ".join(f"{_escape(k)}={v}" for k, v in sorted(a["by_source"].items())),
             f"no arg schema ({len(a['no_schema'])}): "
             + (", ".join(_escape(n) for n in a["no_schema"][:60]) or "none")
             + (f" ... {len(a['no_schema']) - 60} more" if len(a["no_schema"]) > 60 else "")]
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
