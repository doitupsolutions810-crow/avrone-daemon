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
