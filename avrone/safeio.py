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
from typing import Any
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


TMP_NAME_KEEP = 120  # bytes of the target's name kept in a temp name (NAME_MAX is 255)


def tmp_prefix(name: str) -> str:
    """mkstemp prefix for a temp file beside `name`: ".<name>." when that fits, else a cut of the
    name plus a hash of the whole. Round 113: ".<name>.XXXXXXXX.tmp" for a 250-byte name is
    over NAME_MAX, so writing any legal long file name failed with ENAMETOOLONG (and the raw
    OSError carried the host path)."""
    raw = os.fsencode(name)
    if len(raw) <= TMP_NAME_KEEP:
        return f".{name}."
    import hashlib
    cut = os.fsdecode(raw[:TMP_NAME_KEEP]).encode("utf-8", "surrogateescape")[:TMP_NAME_KEEP]
    return f".{os.fsdecode(cut)}~{hashlib.sha256(raw).hexdigest()[:12]}."


def mkstemp_beside(p: Path) -> tuple[int, str]:
    """mkstemp for a temp file beside `p`. Round 121: when the dir refused it (read-only volume,
    EACCES, full inode table) the error named the random temp file (".f.txt.6u8bd1h0.tmp"),
    which the caller never asked about; it now names `p` itself."""
    try:
        return tempfile.mkstemp(dir=p.parent, prefix=tmp_prefix(p.name), suffix=".tmp")
    except OSError as e:
        if e.errno is None:
            raise
        raise type(e)(e.errno, e.strerror or os.strerror(e.errno), str(p)) from None


def with_target(e: BaseException, p: Any) -> BaseException:
    """`e` naming `p` when it names no file. Round 124: a full disk or file-size limit hit while
    writing raised a bare "[Errno 27] File too large" (write() errors carry no filename), so the
    agent was not told which file failed. Same class and errno; anything else is returned as is."""
    if isinstance(e, OSError) and e.errno is not None and e.filename is None and e.filename2 is None:
        try:
            return type(e)(e.errno, e.strerror or os.strerror(e.errno), str(p))
        except Exception:  # a subclass with its own constructor
            return e
    return e


def atomic_write(path: str | os.PathLike, data: str | bytes, *, encoding: str = "utf-8",
                 fsync: bool = False, mode: int | None = None) -> None:
    p = Path(path)
    raw = data.encode(encoding) if isinstance(data, str) else data
    fd, tmp = mkstemp_beside(p)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.chmod(tmp, _default_mode() if mode is None else mode)
        os.replace(tmp, p)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        t = with_target(e, p)
        if t is e:
            raise
        raise t from e


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


def lock_wait_s() -> float:
    """AVRONE_LOCK_WAIT_S: how long a cross-process flock waits before giving up (default 30 s,
    0..3600). Junk reads as the default."""
    try:
        v = float(os.environ.get("AVRONE_LOCK_WAIT_S", "").strip() or 30)
    except ValueError:
        return 30.0
    if v != v or v < 0:
        return 30.0
    return min(v, 3600.0)


def flock_wait(fd: int, timeout_s: float | None = None) -> bool:
    """LOCK_EX with a deadline (round 104). A blocking flock waited forever on a peer process that
    hung while holding the lock; every to_thread worker that touched the same file then blocked
    too, and once the default executor's workers were all stuck the whole node stopped. Polls
    LOCK_EX|LOCK_NB with backoff (2 ms to 100 ms). True when held, False on timeout. Raises
    OSError for errors other than "busy" (callers already treat those as "no flock here")."""
    import errno
    import fcntl
    import time as _t
    wait = lock_wait_s() if timeout_s is None else max(0.0, float(timeout_s))
    deadline = _t.monotonic() + wait
    delay = 0.002
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise
        left = deadline - _t.monotonic()
        if left <= 0:
            return False
        _t.sleep(min(delay, left))
        delay = min(delay * 2, 0.1)


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
    best, total = newest_entries(directory, suffix, n)
    return [Path(path) for _, _, path in best], total


def newest_entries(directory: str | os.PathLike, suffix: str, n: int) -> tuple[list[tuple[float, str, str]], int]:
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
    return sorted(best), total


def json_loads(data, **kw):
    """json.loads, but absurdly nested input (a few hundred KB of "[" from a model, a peer, a
    request body or a hand-edited state file) is a JSONDecodeError like any other bad JSON instead
    of a RecursionError that every `except ValueError` caller let escape (round 111)."""
    import json as _json
    try:
        return _json.loads(data, **kw)
    except RecursionError:
        raise _json.JSONDecodeError("JSON nested too deeply", "", 0) from None
