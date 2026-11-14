"""Round 211: WAL/SHM privacy under competing real processes and umasks.

The R205 failure was not a forged sidecar: fresh databases created with mode
0644 under a 022 umask spawned legitimate 0644 SQLite WAL/SHM files. These
tests start independent Python processes, not mocked connections, with fresh
and pre-existing private database variants and different inherited umasks.
Every participating process must complete ingestion, observe private 0600
database/WAL/SHM files, and leave exactly one durable receipt per attempt.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from avrone.trial_receiver import TrialStore


WORKER = """
import json
import os
import pathlib
import stat
import sys
import time
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore

database, ready_file, gate_file, umask_str, worker_id = sys.argv[1:]
os.umask(int(umask_str, 8))
path = pathlib.Path(database)
pathlib.Path(ready_file).touch()
deadline = time.monotonic() + 20
while not pathlib.Path(gate_file).exists():
    if time.monotonic() > deadline:
        raise SystemExit("launch gate timed out")
    time.sleep(0.002)

store = TrialStore(path)
envelope = make_trial(
    system="cyclekernel", node="node-211", run_id="run-211",
    check_id="wal-umask", check_version="v2",
    attempt_id="worker-" + worker_id, outcome="pass",
)
receipt = store.ingest_many([envelope], sender="peer-" + worker_id, transport="fleet")
assert receipt["accepted"] == 1, receipt
conn = store._connect()
try:
    # Force a read transaction and materialize active WAL/SHM files while
    # this connection is still held, before last-closer cleanup can unlink.
    assert conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0] >= 1
    modes = {}
    for name in ("", "-wal", "-shm"):
        p = pathlib.Path(str(path) + name)
        if p.exists():
            modes[name or "db"] = stat.S_IMODE(p.stat().st_mode)
            assert modes[name or "db"] == 0o600, (str(p), oct(modes[name or "db"]))
    assert modes["db"] == 0o600
    print(json.dumps({"worker": worker_id, "modes": modes, "accepted": True}), flush=True)
finally:
    conn.close()
"""


PROFILES = [
    pytest.param("fresh", "022", id="fresh-default-022"),
    pytest.param("fresh", "002", id="fresh-group-writable-002"),
    pytest.param("warm-0644", "022", id="trusted-legacy-0644-022"),
    pytest.param("warm-0600", "077", id="warm-private-077"),
]


def run_profile(tmp_path, *, mode, mask, seed):
    directory = tmp_path / f"private-{mode}-{mask}-{seed}"
    directory.mkdir(mode=0o700)
    path = directory / "trials.sqlite3"

    if mode.startswith("warm"):
        initial = TrialStore(path)
        assert initial.pool_snapshot()["runs"] == 0
        path.chmod(0o644 if mode == "warm-0644" else 0o600)
        assert stat.S_IMODE(path.stat().st_mode) == (
            0o644 if mode == "warm-0644" else 0o600
        )

    gate = tmp_path / f"start-{mode}-{mask}-{seed}"
    processes = []
    ready = []
    root = Path(__file__).resolve().parents[1]
    try:
        for worker in range(3):
            marker = tmp_path / f"ready-{mode}-{mask}-{seed}-{worker}"
            ready.append(marker)
            processes.append(subprocess.Popen(
                [
                    sys.executable, "-c", WORKER, str(path), str(marker),
                    str(gate), mask, str(worker + seed * 3),
                ],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            ))

        deadline = time.monotonic() + 20
        while not all(f.exists() for f in ready):
            premature = []
            for index, proc in enumerate(processes):
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=2)
                    premature.append((index, proc.returncode, stdout[-1200:], stderr[-5000:]))
            if premature:
                pytest.fail(f"workers exited before WAL gate: {premature!r}")
            if time.monotonic() > deadline:
                pytest.fail(f"workers failed to reach WAL gate: {ready!r}")
            time.sleep(0.005)

        gate.touch()
        outputs = []
        for index, proc in enumerate(processes):
            try:
                stdout, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=3)
                pytest.fail(f"worker {index} stuck: {stdout[-1200:]!r} {stderr[-5000:]!r}")
            outputs.append((proc.returncode, stdout, stderr))
        bad = [
            (index, code, output[-1000:], error[-5000:])
            for index, (code, output, error) in enumerate(outputs) if code != 0
        ]
        assert not bad, f"concurrent WAL privacy initialization failure: {bad!r}"
        for code, output, error in outputs:
            record = json.loads(output)
            assert record["accepted"] is True
            assert record["modes"]["db"] == 0o600
            assert all(permission == 0o600 for permission in record["modes"].values())
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    reopened = TrialStore(path)
    assert reopened.pool_snapshot()["runs"] == 3
    receipts = reopened.receipt_records()
    assert len(receipts) == 3
    assert {r["sender"] for r in receipts} == {
        f"peer-{worker + seed * 3}" for worker in range(3)
    }
    assert reopened.quarantine_records() == []
    db = reopened._connect()
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 3
    finally:
        db.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode,mask", PROFILES)
@pytest.mark.parametrize("seed", [0, 9])
def test_independent_workers_enforce_wal_privacy_across_umask_profiles(
    tmp_path, mode, mask, seed,
):
    run_profile(tmp_path, mode=mode, mask=mask, seed=seed)
