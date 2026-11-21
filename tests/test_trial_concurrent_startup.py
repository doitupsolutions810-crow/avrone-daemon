"""Round 203: reproducible competing SQLite receiver startup and evidence writes.

Unlike a single-process ThreadPoolExecutor, separate processes race through
connect, journal_mode=WAL, schema creation, trigger/index validation and their
first write. Keep every process's stdout, stderr and return code on failure.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from avrone.trial_receiver import TrialStore


WORKER = """
import json
import pathlib
import sys
import time
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore

database, ready, gate, index = sys.argv[1:]
index = int(index)
pathlib.Path(ready).touch()
deadline = time.monotonic() + 15.0
while not pathlib.Path(gate).exists():
    if time.monotonic() > deadline:
        raise SystemExit("worker did not receive start signal")
    time.sleep(0.002)
store = TrialStore(database)
envelope = make_trial(
    system="cyclekernel", node="node-startup", run_id="run-startup",
    check_id="cross-process", check_version="v2",
    attempt_id=f"worker-{index}", outcome="pass",
)
response = store.ingest_many(
    [envelope], sender=f"peer-{index}", transport="fleet"
)
assert response["accepted"] == 1, response
print(json.dumps({"worker": index, "status": response["results"][0]["status"]}),
      flush=True)
"""


def run_workers(tmp_path, *, seed: int, warm: bool):
    private = tmp_path / f"private-{seed}-{warm}"
    private.mkdir(mode=0o700)
    path = private / "trials.sqlite3"
    if warm:
        initial = TrialStore(path)
        assert initial.pool_snapshot()["runs"] == 0

    gate = tmp_path / f"go-{seed}-{warm}"
    workers = []
    ready_files = []
    root = Path(__file__).resolve().parents[1]
    try:
        for index in range(4):
            ready = tmp_path / f"ready-{seed}-{warm}-{index}"
            ready_files.append(ready)
            workers.append(subprocess.Popen(
                [sys.executable, "-c", WORKER, str(path), str(ready),
                 str(gate), str(index + seed * 4)],
                cwd=str(root), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 15.0
        while not all(p.exists() for p in ready_files):
            exited = []
            for i, proc in enumerate(workers):
                if proc.poll() is not None:
                    output, error = proc.communicate(timeout=2)
                    exited.append((i, proc.returncode, output[-1000:], error[-4000:]))
            if exited:
                pytest.fail(f"worker exited before database open gate: {exited!r}")
            if time.monotonic() > deadline:
                pytest.fail(f"workers did not reach start gate: {ready_files!r}")
            time.sleep(0.005)
        gate.touch()
        outputs = []
        for index, proc in enumerate(workers):
            try:
                output, error = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, error = proc.communicate(timeout=3)
                pytest.fail(f"worker {index} timed out; stdout={output!r}, stderr={error!r}")
            outputs.append((proc.returncode, output, error))
        bad = [(i, code, out[-1000:], err[-5000:])
               for i, (code, out, err) in enumerate(outputs) if code != 0]
        assert not bad, f"competing SQLite initialization failed: {bad!r}"
        assert len(outputs) == 4
        for code, output, error in outputs:
            assert json.loads(output)["status"] == "accepted", (output, error)
    finally:
        for proc in workers:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    final = TrialStore(path)
    assert final.pool_snapshot()["runs"] == 4
    assert len(final.receipt_records()) == 4
    assert final.quarantine_records() == []
    db = final._connect()
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        final._assert_source_identity_unique_index(db)
        final._assert_source_identity_triggers(db)
        final._assert_primary_key_schema(db)
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 4
    finally:
        db.close()


@pytest.mark.parametrize("seed", [0, 5])
@pytest.mark.parametrize("warm", [False, True], ids=["fresh-schema", "warm-wal"])
def test_competing_receiver_processes_create_stable_evidence(tmp_path, seed, warm):
    run_workers(tmp_path, seed=seed, warm=warm)
