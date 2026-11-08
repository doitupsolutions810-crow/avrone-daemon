"""Per-agent persistent filesystem workspaces.

Every agent gets its OWN directory under the shared workspace that survives across
runs, so it can keep files between tasks:

    <settings.workspace>/agents/<agent_id>/

The agent id is derived from a stable name (by default the agent's role, or an
explicit ``agent_id``) and sanitized to ``[a-z0-9_-]{1,48}``, so the directory is
always exactly one level below ``workspace/agents`` and can never escape it
(no ``..``, no separators, no absolute paths, no symlinked dirs).

Inside the Docker runner the same host directory is reachable two ways: through the
shared ``/workspace`` bind mount (``/workspace/agents/<id>``, used by the long-lived
environment container) and, in isolated mode, as a dedicated per-agent volume
``-v <host agent dir>:/agent``.

Options (env, read at call time so config.py needs no change):
    AVRONE_AGENT_FS                 1/0  give agents their own dir (default 1)
    AVRONE_AGENT_DOCKER_ISOLATE     1/0  docker: run each agent command in a throwaway
                                         container that mounts ONLY the agent dir at
                                         /agent (default 0: exec in the shared container
                                         with cwd /workspace/agents/<id>)
"""
from __future__ import annotations

import errno
import os
import re
from pathlib import Path

AGENTS_SUBDIR = "agents"
CONTAINER_AGENT_MOUNT = "/agent"
CONTAINER_WORKSPACE = "/workspace"
MAX_ID_LEN = 48

_BAD = re.compile(r"[^a-z0-9_-]+")


class AgentFSError(ValueError):
    """An agent path that is invalid or would escape its confinement."""


def resolve(p: Path | str) -> Path:
    """Path.resolve() that turns a symlink loop or an unresolvable path into AgentFSError. Python
    3.12 raises RuntimeError("Symlink loop from '/abs/host/path'") and 3.13 OSError(ELOOP): either
    reached the model as a raw error naming the host's absolute workspace path, unaudited."""
    try:
        return Path(p).resolve()
    except (RuntimeError, OSError) as e:
        loop = isinstance(e, RuntimeError) or getattr(e, "errno", None) == errno.ELOOP
        raise AgentFSError("path has a symlink loop" if loop else "path cannot be resolved") from None


def _truthy(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def agent_fs_enabled() -> bool:
    return _truthy(os.environ.get("AVRONE_AGENT_FS"), True)


def docker_isolate() -> bool:
    return _truthy(os.environ.get("AVRONE_AGENT_DOCKER_ISOLATE"), False)


def sanitize_agent_id(raw: object) -> str:
    """Map any agent name/role to a safe, stable directory name.

    Lowercased; runs of anything outside [a-z0-9_-] collapse to '-'; leading/trailing
    '-', '_' and '.' are stripped; capped at MAX_ID_LEN. Empty results become 'agent'.
    The output never contains '/', '\\', '.' or '..'.

    Round 122: a role with letters or digits outside ASCII ("研究员", "Ελεγκτής", "Rédacteur")
    keeps its ASCII part and gets "_u" + 8 hex of a hash of the whole role (NFC, casefolded).
    Every non-Latin role used to become "agent" (one shared home, memory and pack for all of
    them), and "数据 Analyst" / "代码 Analyst" were both "analyst". Pure-ASCII roles, and symbols
    such as emoji, map exactly as before. The output is ASCII, so the mapping is idempotent."""
    text = str(raw if raw is not None else "").strip()
    if not text.isascii():
        import unicodedata
        text = unicodedata.normalize("NFC", text)  # "Re\u0301dacteur" is "Rédacteur"
    s = _BAD.sub("-", text.lower()).strip("-_")
    if any(not c.isascii() and (c.isalnum() or unicodedata.category(c).startswith("M")) for c in text):
        import hashlib
        key = text.casefold()
        sfx = "_u" + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        return (s[:MAX_ID_LEN - len(sfx)].strip("-_") or "agent") + sfx
    s = s[:MAX_ID_LEN].strip("-_")
    return s or "agent"


def agents_root(workspace: Path) -> Path:
    return resolve(workspace) / AGENTS_SUBDIR


def agent_dir(workspace: Path, agent_id: object, create: bool = True) -> Path:
    """The confined, persistent directory for `agent_id` (created on demand).

    Raises AgentFSError if the result would not sit directly under workspace/agents
    (e.g. a symlink planted there pointing elsewhere)."""
    ws = resolve(workspace)
    root = ws / AGENTS_SUBDIR
    sid = sanitize_agent_id(agent_id)
    if root.is_symlink():
        raise AgentFSError("agents root must not be a symlink")
    p = root / sid
    if p.is_symlink():
        raise AgentFSError(f"agent dir {sid!r} must not be a symlink")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        p.mkdir(mode=0o700, exist_ok=True)
    rp = resolve(p)
    if rp.parent != resolve(root) or not rp.is_relative_to(ws):
        raise AgentFSError("agent dir escapes the workspace")
    return rp


def is_agent_dir(workspace: Path, path: Path | None) -> bool:
    """True if `path` is exactly one confined agent dir (workspace/agents/<id>)."""
    if path is None:
        return False
    try:
        rp = resolve(path)
        root = agents_root(workspace)
    except AgentFSError:
        return False
    return rp.parent == root and sanitize_agent_id(rp.name) == rp.name


def confine(base: Path, rel: object) -> Path:
    """Resolve a RELATIVE path inside `base`; reject absolute paths, '..' and escapes."""
    raw = str(rel if rel is not None else "").strip()
    if not raw or raw in {".", "./"}:
        return resolve(base)
    if "\x00" in raw:
        raise AgentFSError("path contains a NUL byte")
    if raw.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", raw) or Path(raw).is_absolute():
        raise AgentFSError("absolute paths are not allowed; use a path relative to your agent dir")
    parts = re.split(r"[\\/]+", raw)
    if any(part == ".." for part in parts):
        raise AgentFSError("'..' is not allowed in agent paths")
    b = resolve(base)
    p = resolve(b / raw)
    if not p.is_relative_to(b):
        raise AgentFSError("path escapes your agent dir")
    return p


def container_agent_path(workspace: Path, host_dir: Path) -> str:
    """Where a host agent dir appears inside the shared container (/workspace/agents/<id>)."""
    rel = Path(host_dir).resolve().relative_to(Path(workspace).resolve())
    return f"{CONTAINER_WORKSPACE}/{rel.as_posix()}"


def bind_args(host: Path | str, target: str) -> list[str]:
    """docker args that bind-mount `host` (made absolute) at `target`.

    `-v SRC:DST` splits on ':', so a workspace like /data/proj:v2 became source /data/proj with an
    invalid mode (the container never started), and a relative path is a named volume to older
    docker. A path with ':' or ',' now uses `--mount` with CSV quoting (what docker's parser reads);
    a newline, NUL or '"' cannot be quoted for docker and is refused."""
    src = str(Path(host).resolve())
    if any(c in src for c in "\n\r\x00\""):
        raise AgentFSError("the host path contains a newline, NUL or quote; docker cannot mount it")
    if ":" in src or "," in src:
        return ["--mount", f'type=bind,"source={src}",target={target}']
    return ["-v", f"{src}:{target}"]


def agent_volume_args(host_dir: Path) -> list[str]:
    """docker args that mount one agent dir as its private volume at /agent."""
    return bind_args(host_dir, CONTAINER_AGENT_MOUNT)
