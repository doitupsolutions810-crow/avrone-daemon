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
    if root.is_file():
        yield root
        return
    # Python 3.11's os.walk recurses through generator frames. A workspace
    # with >1,000 nested directories can raise RecursionError. Iterate with an
    # explicit stack and preserve walk's sorted, no-symlink-descent behavior.
    pending = [root]
    while pending:
        directory = pending.pop()
        dirs: list[Path] = []
        files: list[Path] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    if is_dir:
                        if (not entry.is_symlink() and entry.name not in skip_names
                                and entry.path not in skip_paths):
                            dirs.append(Path(entry.path))
                    else:
                        files.append(Path(entry.path))
        except OSError:
            continue
        for file in sorted(files, key=lambda path: path.name):
            yield file
        pending.extend(reversed(sorted(dirs, key=lambda path: path.name)))


def _lines(text: str):
    """Round 49: split on "\n" only, as grep, git and editors count lines (textsafe.lines_keepends
    has the same rule). str.splitlines() also broke on \x0c, \x0b, \x1c-\x1e, NEL, U+2028 and a
    lone \r, so after a form feed in a Python file every reported line number was off, and a
    pattern could not match text either side of one. A CRLF's \r is dropped; a UTF-8 BOM at the
    start of the file is dropped so ^anchors match line 1."""
    if text.startswith("\ufeff"):
        text = text[1:]
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    for ln in parts:
        yield ln[:-1] if ln.endswith("\r") else ln


def _int(req: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = req.get(key, default)
    if type(v) is not int:
        return default
    return max(lo, min(hi, v))


def run(req: dict, out) -> None:
    rx = re.compile(req["pattern"])
    # round 49: the parent's limits, bounded (a bool, float or string limit was int()-ed as is)
    cap = _int(req, "cap", 200, 0, 10_000)
    max_files = _int(req, "max_files", 5000, 0, 1_000_000)
    max_bytes = _int(req, "max_bytes", 1_000_000, 0, 64_000_000)
    hits = scanned = 0
    # Zero and negative scan budgets must not enumerate or open the filesystem.
    if cap <= 0 or max_files <= 0:
        out.write(json.dumps({"done": True, "scanned": 0}) + "\n")
        out.flush()
        return
    root = Path(req["root"])
    for f in _iter_files(root, set(req.get("skip_dirs") or []), set(req.get("skip_paths") or [])):
        if scanned >= max_files or hits >= cap:  # round 49: was ">", one file past the limit
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
        for no, line in enumerate(_lines(data.decode(errors="replace")), 1):
            if rx.search(line):
                out.write(json.dumps({"path": str(f), "line": no, "text": line[:4000].strip()[:300]}) + "\n")
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

    from avrone.textsafe import lines_keepends
    req = json.loads(sys.stdin.read())
    for ln in difflib.unified_diff(lines_keepends(req["a"]), lines_keepends(req["b"]),
                                   fromfile=req["a_name"], tofile=req["b_name"], n=max(0, min(int(req["n"]), 10_000))):
        sys.stdout.write(json.dumps(ln) + "\n")
    sys.stdout.flush()
