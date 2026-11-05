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
import sys
from pathlib import Path


def _iter_files(root: Path, skip_names: set[str], skip_paths: set[str]):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in skip_names and os.path.join(dirpath, d) not in skip_paths)
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


def run(req: dict, out) -> None:
    rx = re.compile(req["pattern"])
    cap, max_files, max_bytes = int(req.get("cap", 200)), int(req.get("max_files", 5000)), int(
        req.get("max_bytes", 1_000_000))
    hits = scanned = 0
    for f in _iter_files(Path(req["root"]), set(req.get("skip_dirs") or []), set(req.get("skip_paths") or [])):
        if scanned > max_files or hits >= cap:
            break
        scanned += 1
        try:
            if not f.is_file() or f.stat().st_size > max_bytes:
                continue
            data = f.read_bytes()
        except OSError:
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
