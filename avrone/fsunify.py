"""Unify the generic file tools with the per-agent filesystem.

When an agent has its own dir (``ctx.agent_dir`` set and ``AVRONE_AGENT_FS`` not 0), the
generic file tools (read_file, write_file, edit_file, list_files, search_files, diff_files)
resolve a path like this:

    "notes/a.txt"         -> <workspace>/agents/<id>/notes/a.txt   (own dir, confined)
    "shared/report.md"    -> <workspace>/report.md                  (old shared workspace)
    "shared" / "shared/"  -> <workspace>                            (shared root)

Both roots go through :func:`avrone.agentfs.confine`: absolute paths, '~', drive letters and
'..' are refused, and the symlink-resolved target must stay inside the chosen root. Through
``shared/`` the ``agents/`` subtree is off limits (so one agent cannot reach another's dir,
or bypass its own quota by writing to it via the shared path), and directory walks from the
shared root skip it.

A child spawned with ``share_dir=true`` (see :mod:`avrone.ctxspawn`) also gets

    "parent/x.txt"        -> <parent agent dir>/x.txt               (READ-ONLY, confined)

Only the read tools (read_file, list_files, search_files, diff_files) may use it; any other
tool is refused, as is any escape from the parent's dir.

Without an agent dir (or with AVRONE_AGENT_FS=0) everything resolves exactly as before:
relative to the shared workspace via :func:`avrone.tools._safe_path`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import agentfs
from .agentfs import AgentFSError

if TYPE_CHECKING:
    from .trinity import AgentContext

SHARED_PREFIX = "shared"
PARENT_PREFIX = "parent"
PARENT_READ_TOOLS = frozenset({"read_file", "list_files", "search_files", "diff_files"})


@dataclass(frozen=True)
class Resolved:
    path: Path          # resolved target
    root: Path          # the root it is confined to (agent dir or shared workspace)
    in_agent: bool      # True when the target lives in the agent's own dir
    unified: bool       # True when agent-dir mode is active for this ctx
    in_parent: bool = False  # True when the target lives in the parent's dir (share_dir, read-only)


def workspace(ctx: "AgentContext") -> Path:
    return Path(ctx.core.runner.workspace).resolve()


def agent_root(ctx: "AgentContext") -> Path | None:
    """The agent's own dir when unified mode applies, else None (legacy behaviour)."""
    if not agentfs.agent_fs_enabled():
        return None
    d = getattr(ctx, "agent_dir", None)
    if d is None:
        return None
    root = Path(d).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _audit_denied(ctx: "AgentContext", tool: str, raw: str, reason: str) -> None:
    codex = getattr(ctx.core, "codex", None)
    if codex is None:
        return
    try:
        codex.emit(ctx.run_id, "agentfs.denied", ctx.name, agent_id=getattr(ctx, "agent_id", None),
                   tool=tool, path=raw[:300], reason=reason)
    except Exception:  # noqa: BLE001 - auditing never breaks the tool
        pass


def split_shared(raw: str, prefix: str = SHARED_PREFIX) -> str | None:
    """If `raw` uses the shared/ (or `prefix`/) prefix, return the remainder (possibly ''), else None."""
    norm = raw.replace("\\", "/")
    if norm == prefix:
        return ""
    if norm.startswith(prefix + "/"):
        return norm[len(prefix) + 1:]  # "shared//x" stays absolute -> refused
    return None


def parent_root(ctx: "AgentContext") -> Path | None:
    """The parent's agent dir when this child was spawned with share_dir=true, else None."""
    d = getattr(ctx, "parent_dir", None)
    return Path(d).resolve() if d is not None else None


def resolve(ctx: "AgentContext", raw: Any, tool: str) -> Resolved:
    """Resolve a tool path for `ctx` (see module doc). Raises ValueError on refusal."""
    from .tools import _safe_path, check_path_text

    ws = workspace(ctx)
    root = agent_root(ctx)
    s = check_path_text(str(raw if raw is not None else "").strip())
    if root is None:
        return Resolved(_safe_path(ws, s), ws, False, False)
    try:
        proot = parent_root(ctx)
        prest = split_shared(s, PARENT_PREFIX) if proot is not None else None
        if prest is not None:
            if tool not in PARENT_READ_TOOLS:
                raise AgentFSError("parent/ is read-only; write to your own dir instead")
            return Resolved(agentfs.confine(proot, prest), proot, False, True, True)
        rest = split_shared(s)
        if rest is None:
            return Resolved(agentfs.confine(root, s), root, True, True)
        p = agentfs.confine(ws, rest)
        agents = agentfs.agents_root(ws)
        if p == agents or p.is_relative_to(agents):
            raise AgentFSError("shared/agents is off limits; your own dir is addressed without the shared/ prefix")
        return Resolved(p, ws, False, True)
    except AgentFSError as e:
        _audit_denied(ctx, tool, s, str(e))
        raise


def display(ctx: "AgentContext", p: Path) -> str:
    """How a path is shown back to the agent, in the same form it can pass in again."""
    ws = workspace(ctx)
    root = agent_root(ctx)
    rp = p.resolve()
    if root is not None:
        if rp.is_relative_to(root):
            return str(rp.relative_to(root)) or "."
        proot = parent_root(ctx)
        if proot is not None and rp.is_relative_to(proot):
            rel = rp.relative_to(proot).as_posix()
            return f"{PARENT_PREFIX}/{rel}" if rel != "." else f"{PARENT_PREFIX}/"
        if rp.is_relative_to(ws):
            rel = rp.relative_to(ws).as_posix()
            return f"{SHARED_PREFIX}/{rel}" if rel != "." else f"{SHARED_PREFIX}/"
        return str(p)
    try:
        return str(rp.relative_to(ws)) or "."
    except ValueError:
        return str(p)


def skip_dirs(ctx: "AgentContext", r: Resolved) -> set[Path]:
    """Directories a walk starting at `r` must not descend into (other agents' dirs)."""
    if not r.unified or r.in_agent or r.in_parent:
        return set()
    return {agentfs.agents_root(workspace(ctx))}
