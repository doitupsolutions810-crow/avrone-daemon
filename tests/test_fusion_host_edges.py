"""Host edges: unprivileged read-only directory, a full tmpfs, rename atomicity,
signals, and several FusionWatch peers at once.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from avrone.fusion_deliver import Ledger, ManualClock
from avrone.fusion_peer import FusionWatch
from avrone.safeio import atomic_write

PY = sys.executable
REPO = Path(__file__).resolve().parents[1]


def _event(eid: str) -> dict:
    return {"id": eid, "source": "avrone", "kind": "evidence", "run_id": "edge",
            "payload": {"text": eid}, "ts": 1}


def test_rename_is_all_or_nothing_under_kill(tmp_path):
    path = tmp_path / "swap.json"
    atomic_write(path, "A" * 4000, fsync=True)
    code = r"""
import sys
from pathlib import Path
from avrone.safeio import atomic_write
path = Path(sys.argv[1])
i = 0
while True:
    atomic_write(path, ("A" if i % 2 == 0 else "B") * 4000, fsync=True)
    i += 1
"""
    proc = subprocess.Popen([PY, "-c", code, str(path)], cwd=str(REPO))
    time.sleep(0.3)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)
    assert path.read_text(encoding="utf-8") in {"A" * 4000, "B" * 4000}


def test_many_watches_forward_together():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path.startswith("/health"):
                body = b'{"boot":"peer"}'
            else:
                body = json.dumps({
                    "posts": [{"id": self.server.post_id, "text": "x", "kind": "finding",
                               "run_id": "run", "ts": 1}],
                    "count": 1, "boot": "board",
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self.server.hits += 1
            body = b'{"accepted":true,"duplicate":false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    servers = []
    for i in range(8):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.post_id = f"peer{i:04d}0001"
        server.hits = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)

    def one(i, server):
        host, port = server.server_address
        base = f"http://{host}:{port}"
        watch = FusionWatch(Path(f"/tmp/fusion-watch-{port}.json"))
        report = watch.watch_once(
            board_url=base + "/api/blackboard", events_url=base + "/v1/events",
            health_url=base + "/health", board_token="t", events_token="t", timeout=3,
        )
        return report["forwarded"], server.hits

    try:
        boxes = []
        threads = []
        for i, server in enumerate(servers):
            box: dict = {}
            def go(i=i, server=server, box=box):
                box["v"] = one(i, server)
            t = threading.Thread(target=go)
            t.start()
            threads.append(t)
            boxes.append(box)
        for t in threads:
            t.join(timeout=20)
        assert [b["v"] for b in boxes] == [(1, 1)] * 8
    finally:
        for server in servers:
            server.shutdown()


def _python_paths() -> list[Path]:
    import site
    import sysconfig
    found: list[Path] = []
    for raw in [sysconfig.get_path("purelib"), *site.getsitepackages(), site.getusersitepackages()]:
        if not raw:
            continue
        path = Path(raw)
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def _open_traverse(start: Path, opened: list[tuple[Path, int]]) -> None:
    node = start.resolve()
    while True:
        try:
            mode = node.stat().st_mode
        except OSError:
            break
        if mode & 0o001 == 0 and all(prev != node for prev, _mode in opened):
            subprocess.run(["sudo", "chmod", "o+x", str(node)], check=True)
            opened.append((node, mode))
        if node == node.parent:
            break
        node = node.parent


def _let_others_import(repo: Path) -> tuple[list[tuple[Path, int]], str]:
    """Other users must be able to walk to the package and its installed deps.

    GitHub's checkout is mode 750 under /home/runner, so fusionro cannot
    import it. Only directories that lacked other-execute are changed, and
    they are restored. No home directory is named.
    """
    opened: list[tuple[Path, int]] = []
    _open_traverse(repo, opened)
    subprocess.run(["sudo", "chmod", "-R", "o+rX", str(repo / "avrone")], check=True)
    libs = _python_paths()
    for lib in libs:
        _open_traverse(lib, opened)
    py_path = os.pathsep.join([str(repo), *(str(lib) for lib in libs)])
    return opened, py_path


def test_unprivileged_read_only_dir(tmp_path):
    if subprocess.call(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        pytest.skip("needs sudo")
    subprocess.run(["sudo", "useradd", "-M", "-N", "-s", "/usr/sbin/nologin", "fusionro"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    opened, py_path = _let_others_import(REPO)
    parent = Path("/tmp/fusion-ro-ledger")
    subprocess.run(["sudo", "rm", "-rf", str(parent)], check=True)
    subprocess.run(["sudo", "mkdir", "-p", str(parent)], check=True)
    subprocess.run(["sudo", "chown", "fusionro", str(parent)], check=True)
    script = r"""
import os, sys
from pathlib import Path
from avrone.fusion_deliver import Ledger, ManualClock
parent = Path(sys.argv[1])
path = parent / "ledger.json"
def event(eid):
    return {"id": eid, "source": "avrone", "kind": "evidence", "run_id": "edge",
            "payload": {"text": eid}, "ts": 1}
assert Ledger(path, ManualClock()).offer(event("kept")) == "accepted"
os.chmod(parent, 0o555)
try:
    try:
        Ledger(path, ManualClock()).offer(event("newid"))
    except OSError:
        pass
    else:
        raise SystemExit("accepted on a read-only directory")
    text = path.read_text(encoding="utf-8")
    if "kept" not in text or "newid" in text:
        raise SystemExit(text[:200])
finally:
    os.chmod(parent, 0o755)
"""
    try:
        proc = subprocess.run(
            ["sudo", "-u", "fusionro", "env", f"PYTHONPATH={py_path}", PY, "-c", script, str(parent)],
            cwd="/tmp", capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        for path, mode in opened:
            subprocess.run(["sudo", "chmod", format(mode & 0o777, "o"), str(path)], check=False)
        subprocess.run(["sudo", "rm", "-rf", str(parent)], check=False)


def test_tmpfs_disk_full_does_not_claim_a_save():
    if subprocess.call(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        pytest.skip("needs sudo")
    mnt = Path("/mnt/fusion-full")
    subprocess.run(["sudo", "mkdir", "-p", str(mnt)], check=True)
    if subprocess.run(["sudo", "mount", "-t", "tmpfs", "-o", "size=128k,mode=1777", "tmpfs", str(mnt)]).returncode != 0:
        pytest.skip("tmpfs mount failed")
    try:
        subprocess.run(["dd", "if=/dev/zero", f"of={mnt / 'pad'}", "bs=1024", "count=256"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path = mnt / "ledger.json"
        with pytest.raises(OSError):
            Ledger(path, ManualClock()).offer(_event("full"))
        if path.exists():
            text = path.read_text(encoding="utf-8")
            json.loads(text)
            assert "full" not in text
    finally:
        subprocess.run(["sudo", "umount", str(mnt)], check=False)
