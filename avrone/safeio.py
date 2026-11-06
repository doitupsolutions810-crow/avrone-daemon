"""Atomic file replacement that never writes through a planted symlink (round 81).

Several state writers built their temp file name from the pid and thread id
(``.fleet.json.<pid>.<tid>.tmp``) and wrote it with ``Path.write_text``. Those names are
predictable, and ``write_text`` follows a symlink, so a link planted at the temp name (by an
agent's shell in its own dir, or anything else that can write the state dir) made the daemon
overwrite the link's target, then rename the link over the real file.

``atomic_write`` creates the temp file with ``mkstemp`` (random name, ``O_CREAT|O_EXCL``, never
follows a link) in the target's directory, writes, optionally fsyncs, sets the mode a plain
``open()`` would have given, and ``os.replace``s it over the target. On any failure the temp
file is removed and the error is raised unchanged.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

_UMASK: int | None = None


def _default_mode() -> int:
    global _UMASK
    if _UMASK is None:
        _UMASK = os.umask(0)
        os.umask(_UMASK)
    return 0o666 & ~_UMASK


def atomic_write(path: str | os.PathLike, data: str | bytes, *, encoding: str = "utf-8",
                 fsync: bool = False, mode: int | None = None) -> None:
    p = Path(path)
    raw = data.encode(encoding) if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.chmod(tmp, _default_mode() if mode is None else mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_nofollow(path: str | os.PathLike, flags: int, mode: int = 0o666) -> int:
    """os.open with O_NOFOLLOW (where the OS has it)."""
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


class NotRegularFile(OSError):
    """The path is a symlink, FIFO, socket, device or directory: never read or written through."""


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _open_regular(path: str | os.PathLike, flags: int, mode: int) -> int:
    try:
        fd = os.open(path, flags | _NOFOLLOW | _NONBLOCK | _CLOEXEC, mode)
    except OSError as e:
        import errno
        if e.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR, errno.EMLINK):
            raise NotRegularFile(e.errno, f"not a regular file: {Path(path).name}") from e
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"not a regular file: {Path(path).name}")
        if _NONBLOCK:  # regular files ignore O_NONBLOCK, but keep later reads/writes plainly blocking
            import fcntl
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_append(path: str | os.PathLike, mode: str = "ab"):
    """A file object appending to a regular file (created if missing). A symlink, FIFO or other
    special file at `path` raises NotRegularFile instead of being followed or blocking.
    mode: "ab", "a" (text, utf-8), "a+b" or "a+" (read too)."""
    rw = "+" in mode
    fd = _open_regular(path, (os.O_RDWR if rw else os.O_WRONLY) | os.O_APPEND | os.O_CREAT, 0o666)
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")


def open_lock(path: str | os.PathLike):
    """A lock file (created if missing) as a binary r/w file object; regular files only."""
    return os.fdopen(_open_regular(path, os.O_RDWR | os.O_CREAT, 0o666), "r+b")


def read_regular(path: str | os.PathLike, max_bytes: int | None = None) -> bytes | None:
    """Bytes of a regular file, or None when it is missing, a symlink or special file. With
    max_bytes, a bigger file yields only its last max_bytes bytes, starting after the first newline
    in them (whole lines only)."""
    try:
        fd = _open_regular(path, os.O_RDONLY, 0)
    except (FileNotFoundError, NotRegularFile):
        return None
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if max_bytes is None or size <= max_bytes:
            return f.read()
        f.seek(size - max_bytes)
        data = f.read(max_bytes)
        cut = data.find(b"\n")
        return data[cut + 1:] if cut >= 0 else b""
