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


MAX_WALK_DIRS = 20_000
MAX_WALK_ENTRIES_PER_DIR = 20_000
MAX_PENDING_DIRS = 20_000


def _iter_files(root: Path, skip_names: set[str], skip_paths: set[str]):
    """Iterative bounded traversal; os.walk recursively descends and overflows on deep trees."""
    if root.is_file():
        yield root
        return
    pending = [root]
    visited_dirs = 0
    while pending and visited_dirs < MAX_WALK_DIRS:
        directory = pending.pop()
        visited_dirs += 1
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if len(entries) >= MAX_WALK_ENTRIES_PER_DIR:
                        break
                    entries.append(entry)
        except OSError:
            continue
        entries.sort(key=lambda entry: entry.name)
        child_dirs: list[Path] = []
        files: list[Path] = []
        for entry in entries:
            if entry.name in skip_names:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.path not in skip_paths:
                        child_dirs.append(Path(entry.path))
                else:
                    files.append(Path(entry.path))
            except OSError:
                continue
        # Match os.walk's top-down order: files in this directory, then sorted child dirs.
        for file_path in files:
            yield file_path
        room = max(0, MAX_PENDING_DIRS - len(pending))
        pending.extend(reversed(child_dirs[:room]))


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
