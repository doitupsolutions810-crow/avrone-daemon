"""Agent file tools confined to the agent's OWN persistent dir (workspace/agents/<id>/).

    fs_write  {"path": str, "content": str, "append"?: bool}
    fs_read   {"path": str, "max_chars"?: int}
    fs_list   {"path"?: str, "recursive"?: bool}
    fs_delete {"path": str, "recursive"?: bool}

Every path is RELATIVE to the agent dir: absolute paths, '~', drive letters and any
'..' segment are rejected, and the resolved target (symlinks followed) must stay
inside the agent dir. Writes are capped per file and per dir (quota):

    AVRONE_AGENT_FS_QUOTA_MB     total bytes one agent dir may hold   (default 50, hard cap 1024)
    AVRONE_AGENT_FS_MAX_FILE_MB  largest single file fs_write accepts (default 5, hard cap 64,
                                 and never more than the quota)

Every write/delete, and every refusal (confinement, file cap, quota), is audited to the
CodeX as agentfs.* events with the agent id, path, bytes and dir usage.
"""
from __future__ import annotations

import errno
import os
import stat
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agentfs import AgentFSError, confine
from .tools import Tool, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext

MB = 1024 * 1024
DEFAULT_QUOTA_MB = 50
HARD_QUOTA_MB = 1024
DEFAULT_FILE_MB = 5
HARD_FILE_MB = 64
READ_DEFAULT_CHARS = 12000
READ_MAX_CHARS = 100_000
LIST_MAX_ENTRIES = 500

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(d: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(d), threading.Lock())


def _mb_env(name: str, default: int, hard: int) -> int:
    """Positive MB from env, clamped to [1, hard]; junk, empty or <=0 falls back to default."""
    raw = os.environ.get(name, "")
    try:
        v = float(raw) if raw.strip() else float(default)
    except ValueError:
        v = float(default)
    if v != v or v <= 0:  # NaN (e.g. "nan") is junk too: int(nan) would crash every fs tool
        v = float(default)
    return int(min(v, hard) * MB)


def quota_bytes() -> int:
    return _mb_env("AVRONE_AGENT_FS_QUOTA_MB", DEFAULT_QUOTA_MB, HARD_QUOTA_MB)


def max_file_bytes() -> int:
    return min(_mb_env("AVRONE_AGENT_FS_MAX_FILE_MB", DEFAULT_FILE_MB, HARD_FILE_MB), quota_bytes())


def dir_usage(d: Path) -> int:
    """Bytes held under `d` (regular files, not following symlinks)."""
    total = 0
    for root, dirs, files in os.walk(d, followlinks=False):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            total += st.st_size
    return total


def _audit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    codex = getattr(ctx.core, "codex", None)
    if codex is None:
        return
    try:
        codex.emit(ctx.run_id, kind, ctx.name, agent_id=getattr(ctx, "agent_id", None), **data)
    except Exception:  # noqa: BLE001 - auditing never breaks the tool
        pass


def _agent_root(ctx: "AgentContext") -> Path:
    d = getattr(ctx, "agent_dir", None)
    if d is None:
        raise AgentFSError("you have no agent dir (per-agent fs is disabled: AVRONE_AGENT_FS=0)")
    d = Path(d).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve(ctx: "AgentContext", tool: str, raw: Any) -> tuple[Path, Path]:
    root = _agent_root(ctx)
    try:
        return root, confine(root, raw)
    except AgentFSError as e:
        _audit(ctx, "agentfs.denied", tool=tool, path=str(raw)[:300], reason=str(e))
        raise


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root)) or "."


def quota_write(ctx: "AgentContext", root: Path, p: Path, data: bytes, append: bool = False,
                tool: str = "fs_write") -> tuple[int, int, int]:
    """Write `data` to `p` inside agent dir `root`, enforcing the per-file cap and dir quota.

    Shared by fs_write and the unified write_file/edit_file. Returns (file bytes, dir usage,
    quota); raises AgentFSError (audited as agentfs.denied) on any refusal."""
    if p == root:
        raise AgentFSError("path must name a file")
    cap, quota = max_file_bytes(), quota_bytes()
    with _lock_for(root):
        if p.is_symlink() or (p.exists() and not p.is_file()):
            _audit(ctx, "agentfs.denied", tool=tool, path=_rel(root, p), reason="not a regular file")
            raise AgentFSError(f"{_rel(root, p)} is not a regular file")
        old = p.stat().st_size if p.exists() else 0
        new_size = (old + len(data)) if append else len(data)
        if new_size > cap:
            _audit(ctx, "agentfs.denied", tool=tool, path=_rel(root, p), reason="file_cap",
                   bytes=new_size, cap=cap)
            raise AgentFSError(f"file would be {new_size} bytes; per-file cap is {cap} bytes")
        usage = dir_usage(root)
        projected = usage - old + new_size
        if projected > quota:
            _audit(ctx, "agentfs.denied", tool=tool, path=_rel(root, p), reason="quota",
                   bytes=new_size, usage=usage, quota=quota)
            raise AgentFSError(f"agent dir quota exceeded: {projected} > {quota} bytes "
                               f"(using {usage}); delete files with fs_delete first")
        p.parent.mkdir(parents=True, exist_ok=True)
        # re-check after mkdir: an intermediate symlinked dir must not lead out
        if not p.parent.resolve().is_relative_to(root):
            raise AgentFSError("path escapes your agent dir")
        # Never follow a symlink at the leaf: one planted (e.g. by the agent's own shell) between
        # the checks above and the write, or at the predictable temp name, used to redirect the
        # write outside the agent dir. O_NOFOLLOW refuses a symlinked leaf; O_EXCL a planted tmp.
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            if append:
                fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow, 0o600)
                with os.fdopen(fd, "ab") as f:
                    f.write(data)
            else:
                tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{threading.get_ident()}-{os.urandom(4).hex()}")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    try:  # overwrite keeps the old file's mode (a chmod +x script stays runnable)
                        old_mode = stat.S_IMODE(os.lstat(p).st_mode) if p.exists() and not p.is_symlink() else None
                    except OSError:
                        old_mode = None
                    if old_mode is not None:
                        os.chmod(tmp, old_mode | 0o600)
                    os.replace(tmp, p)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
        except OSError as e:
            if p.is_symlink() or e.errno == errno.ELOOP:
                _audit(ctx, "agentfs.denied", tool=tool, path=_rel(root, p), reason="symlink")
                raise AgentFSError(f"{_rel(root, p)} is not a regular file (symlink)") from None
            raise
    _audit(ctx, "agentfs.write", path=_rel(root, p), bytes=new_size, append=append,
           usage=projected, quota=quota, **({} if tool == "fs_write" else {"tool": tool}))
    return new_size, projected, quota


# off_loop: quota checks walk the whole agent dir (100k files ~0.26 s), listing sorts it (~0.6 s)
@off_loop
async def t_fs_write(ctx: "AgentContext", args: dict[str, Any]) -> str:
    root, p = _resolve(ctx, "fs_write", args.get("path", ""))
    data = str(args.get("content", "")).encode("utf-8")
    new_size, projected, quota = quota_write(ctx, root, p, data, bool(args.get("append", False)))
    return f"wrote {_rel(root, p)} ({new_size} bytes; dir {projected}/{quota} bytes)"


@off_loop
async def t_fs_read(ctx: "AgentContext", args: dict[str, Any]) -> str:
    root, p = _resolve(ctx, "fs_read", args.get("path", ""))
    if not p.exists():
        return f"ERROR: {_rel(root, p)} not found"
    if not p.is_file():
        return f"ERROR: {_rel(root, p)} is a directory; use fs_list"
    try:
        limit = int(args.get("max_chars", READ_DEFAULT_CHARS))
    except (TypeError, ValueError):
        limit = READ_DEFAULT_CHARS
    limit = max(1, min(limit, READ_MAX_CHARS))
    with p.open("rb") as f:
        raw = f.read(limit * 4 + 4)
    text = raw.decode("utf-8", errors="replace")
    size = p.stat().st_size
    out = text[:limit]
    if len(text) > limit or size > len(raw):
        out += f"\n...[truncated; file is {size} bytes]"
    return out


@off_loop
async def t_fs_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    root, p = _resolve(ctx, "fs_list", args.get("path", ""))
    if not p.exists():
        return f"ERROR: {_rel(root, p)} not found"
    if p.is_file():
        return f"{_rel(root, p)}  {p.stat().st_size} bytes"
    recursive = bool(args.get("recursive", False))
    items = sorted(p.rglob("*") if recursive else p.iterdir())
    lines = []
    for q in items[:LIST_MAX_ENTRIES]:
        if q.is_symlink():
            lines.append(f"{_rel(root, q)} -> (symlink)")
        elif q.is_dir():
            lines.append(f"{_rel(root, q)}/")
        else:
            lines.append(f"{_rel(root, q)}  {q.stat().st_size} bytes")
    if len(items) > LIST_MAX_ENTRIES:
        lines.append(f"...[{len(items) - LIST_MAX_ENTRIES} more]")
    head = f"[agent dir {getattr(ctx, 'agent_id', '') or root.name}: {dir_usage(root)}/{quota_bytes()} bytes used]"
    return head + "\n" + ("\n".join(lines) if lines else "(empty)")


@off_loop
async def t_fs_delete(ctx: "AgentContext", args: dict[str, Any]) -> str:
    root = _agent_root(ctx)
    raw = str(args.get("path", "")).strip().rstrip("/\\")
    # Confine the PARENT (resolved), then act on the leaf un-resolved, so a symlink is
    # removed itself and never followed to what it points at.
    head, _, leaf = raw.replace("\\", "/").rpartition("/")
    try:
        if not leaf or leaf in {".", ".."}:
            raise AgentFSError("refusing to delete your whole agent dir; name a file or subdir"
                               if raw in {"", "."} else "'..' is not allowed in agent paths")
        confine(root, raw if not (root / raw).is_symlink() else (head or "."))
        parent = confine(root, head or ".")
    except AgentFSError as e:
        _audit(ctx, "agentfs.denied", tool="fs_delete", path=raw[:300] or ".", reason=str(e))
        raise
    target = parent / leaf
    rel = _rel(root, parent / leaf) if parent.is_relative_to(root) else raw
    if not target.is_symlink() and not target.exists():
        return f"ERROR: {rel} not found"
    with _lock_for(root):
        if target.is_symlink() or target.is_file():
            freed = target.lstat().st_size
            target.unlink()
        elif target.is_dir():
            if not bool(args.get("recursive", False)) and any(target.iterdir()):
                return f"ERROR: {rel} is a non-empty directory; pass recursive: true"
            freed = dir_usage(target)
            shutil.rmtree(target)
        else:
            return f"ERROR: {rel} is not a file or directory"
    _audit(ctx, "agentfs.delete", path=rel, bytes=freed)
    return f"deleted {rel} ({freed} bytes freed)"


AGENT_FS_TOOLS = [
    ("fs_write", "write a file in YOUR OWN persistent agent dir (kept across runs). Path is relative to that dir; "
     "per-file cap and dir quota apply.", '{"path": str, "content": str, "append"?: bool}', t_fs_write),
    ("fs_read", "read a file from your own persistent agent dir.", '{"path": str, "max_chars"?: int}', t_fs_read),
    ("fs_list", "list your own persistent agent dir (or a subdir) with sizes and quota usage.",
     '{"path"?: str, "recursive"?: bool}', t_fs_list),
    ("fs_delete", "delete a file (or, with recursive, a subdir) in your own persistent agent dir.",
     '{"path": str, "recursive"?: bool}', t_fs_delete),
]


def register_agent_fs_tools(registry) -> None:
    for name, desc, args_doc, fn in AGENT_FS_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
