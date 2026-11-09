"""search_files' scan, run in a child process so a pathological regex cannot freeze the node.

Python's `re` never releases the GIL and cannot be interrupted: before round 53, an agent's
pattern like `(a+)+b` against a line of 28 'a's held the event loop for ~17 s (32 'a's: minutes),
stalling every agent, the HTTP API and the fleet heartbeats of the whole node. The parent now runs
this module with a deadline, reads the hits it streams (one JSON line each) and kills it on timeout.

Input (stdin, JSON): {"pattern", "root", "skip_dirs": [names], "skip_paths": [abs], "cap", "max_files",
"max_bytes"}. Output: {"path", "line", "text"} per hit, then {"done": true, "scanned": n}.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

from .safeio import read_capped


def _iter_files(root: Path, skip_names: set[str], skip_paths: set[str]):
    """Iterative depth-first walk; os.walk's recursive generator overflows on deep trees.

    Directory descriptors are opened relative to their already-open parent and with
    O_NOFOLLOW. This avoids both Python recursion and following a directory symlink
    swapped in between enumeration and descent. File reads remain O_NOFOLLOW via
    safeio.read_capped in run().
    """
    try:
        root_stat = root.stat()
    except OSError:
        return
    if stat.S_ISREG(root_stat.st_mode):
        yield root
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return

    stack: list[tuple[int, Path]] = [(root_fd, root)]
    try:
        while stack:
            dir_fd, directory = stack.pop()
            children: list[tuple[int, Path]] = []
            try:
                with os.scandir(dir_fd) as entries:
                    files: list[Path] = []
                    dirs: list[tuple[int, Path]] = []
                    for entry in entries:
                        child = directory / entry.name
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            is_dir = False
                        if not is_dir:
                            # Keep symlinks/special files in the file list so run()
                            # applies lstat + regular-file checks and never reads them.
                            files.append(child)
                            continue
                        if entry.name in skip_names or str(child) in skip_paths:
                            continue
                        try:
                            child_fd = os.open(
                                entry.name, flags, dir_fd=dir_fd,
                            )
                        except OSError:
                            continue
                        dirs.append((child_fd, child))
                for child in sorted(files, key=lambda p: p.name):
                    yield child
                # LIFO stack: reverse insertion preserves os.walk's ascending DFS order.
                stack.extend(reversed(sorted(dirs, key=lambda item: str(item[1]))))
            finally:
                os.close(dir_fd)
    finally:
        # A consumer may stop after cap hits or an exception; close queued descriptors.
        for queued_fd, _ in stack:
            try:
                os.close(queued_fd)
            except OSError:
                pass


def run(req: dict, out) -> None:
    rx = re.compile(req["pattern"])
    cap, max_files, max_bytes = int(req.get("cap", 200)), int(req.get("max_files", 5000)), int(
        req.get("max_bytes", 1_000_000))
    hits = scanned = 0
    root = Path(req["root"])
    for f in _iter_files(root, set(req.get("skip_dirs") or []), set(req.get("skip_paths") or [])):
        if scanned > max_files or hits >= cap:
            break
        scanned += 1
        try:
            # round 86: lstat + O_NOFOLLOW read. is_file()/read_bytes() followed a symlink in the
            # tree to any file on the host, and a FIFO swapped in after the check blocked the scan.
            st = f.lstat() if f != root else f.stat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
                continue
            if f != root:
                data = read_capped(f, max_bytes)
            else:  # round 107: a FIFO swapped in for the root file after stat() no longer blocks
                from .safeio import open_read
                with open_read(f, follow=True) as fh:
                    data = fh.read(max_bytes + 1)
                if len(data) > max_bytes:
                    continue
        except OSError:
            continue
        if data is None:
            continue
        if b"\0" in data[:4096]:
            continue
        for no, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            if rx.search(line):
                out.write(json.dumps({"path": str(f), "line": no, "text": line.strip()[:300]}) + "\n")
                out.flush()
                hits += 1
                if hits >= cap:
                    break
    out.write(json.dumps({"done": True, "scanned": scanned}) + "\n")
    out.flush()


def main() -> None:
    run(json.loads(sys.stdin.read()), sys.stdout)


if __name__ == "__main__":
    main()


def diff_main() -> None:
    """unified_diff in a child (see t_diff_files): difflib is quadratic on many similar short lines
    (20k lines: 5 s; the 1 MB input cap allows ~200k lines: minutes) and cannot be interrupted."""
    import difflib
    req = json.loads(sys.stdin.read())
    for ln in difflib.unified_diff(req["a"].splitlines(keepends=True), req["b"].splitlines(keepends=True),
                                   fromfile=req["a_name"], tofile=req["b_name"], n=int(req["n"])):
        sys.stdout.write(json.dumps(ln) + "\n")
    sys.stdout.flush()
