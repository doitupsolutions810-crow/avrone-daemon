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


def _open_regular(path: str | os.PathLike, flags: int, mode: int, follow: bool = False) -> int:
    try:
        fd = os.open(path, flags | (0 if follow else _NOFOLLOW) | _NONBLOCK | _CLOEXEC, mode)
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


class FileTooBig(OSError):
    """A regular file larger than the caller's cap: never read whole."""


def read_capped(path: str | os.PathLike, max_bytes: int) -> bytes | None:
    """Bytes of a regular file of at most max_bytes. None when it is missing, a symlink or a special
    file (FIFO, socket, device, directory); FileTooBig when it is bigger than max_bytes (also when
    it grows past the cap while being read)."""
    try:
        fd = _open_regular(path, os.O_RDONLY, 0)
    except (FileNotFoundError, NotRegularFile):
        return None
    with os.fdopen(fd, "rb") as f:
        if os.fstat(f.fileno()).st_size > max_bytes:
            raise FileTooBig(f"{Path(path).name} is over {max_bytes} bytes")
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise FileTooBig(f"{Path(path).name} is over {max_bytes} bytes")
    return data


def open_read(path: str | os.PathLike, follow: bool = False):
    """A binary file object reading a regular file; NotRegularFile for a special file, and for a
    symlink unless follow=True. With follow=True a link is read through (for tool paths already
    confined by fsunify/agentfs), but a FIFO swapped in after the caller's is_file() check still
    never blocks (O_NONBLOCK, then fstat must say regular)."""
    return os.fdopen(_open_regular(path, os.O_RDONLY, 0, follow), "rb")


class TreeWalk:
    """Entries of a tree in sorted(root.rglob("*")) order (each directory's names sorted, depth
    first), without listing the whole tree first (round 90). At most `scan_max` directory entries
    are read in all, counting entries of directories too big to sort whole; `truncated` says the
    walk stopped early. Symlinked directories are never entered; `skip(name)` prunes a name.
    Iterating yields (relative path with "/" separators, os.DirEntry)."""

    def __init__(self, root: str | os.PathLike, scan_max: int, skip=None, recurse: bool = True):
        self.root, self.scan_max, self.skip = os.fspath(root), max(0, int(scan_max)), skip
        self.recurse = recurse
        self.scanned = 0
        self.truncated = False

    def _listing(self, path: str) -> list:
        out = []
        try:
            with os.scandir(path) as it:
                for de in it:
                    if self.scanned >= self.scan_max:
                        self.truncated = True
                        break
                    self.scanned += 1
                    if self.skip is not None and self.skip(de.name):
                        continue
                    out.append(de)
        except OSError:
            return []
        out.sort(key=lambda de: de.name, reverse=True)
        return out

    def __iter__(self):
        stack = [("", self._listing(self.root))]
        while stack:
            prefix, entries = stack[-1]
            if not entries:
                stack.pop()
                continue
            de = entries.pop()
            rel = prefix + de.name
            yield rel, de
            try:
                is_dir = de.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir and self.recurse and not self.truncated:
                stack.append((rel + "/", self._listing(de.path)))


def newest_files(directory: str | os.PathLike, suffix: str, n: int) -> tuple[list[Path], int]:
    """The `n` most recently modified regular files named *<suffix> directly in `directory`, oldest
    first, and how many such files there are in all (round 92). One scandir + one lstat per entry
    and a bounded heap: listings used to open and parse every file in the directory. Symlinks,
    special files, dot-files and entries that vanish mid-listing are skipped."""
    import heapq
    best: list[tuple[float, str, str]] = []
    total = 0
    try:
        it = os.scandir(directory)
    except OSError:
        return [], 0
    with it:
        for de in it:
            name = de.name
            if not name.endswith(suffix) or name.startswith("."):
                continue
            try:
                st = de.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            total += 1
            if n <= 0:
                continue
            item = (st.st_mtime, name, de.path)
            if len(best) < n:
                heapq.heappush(best, item)
            elif item > best[0]:
                heapq.heapreplace(best, item)
    return [Path(path) for _, _, path in sorted(best)], total
