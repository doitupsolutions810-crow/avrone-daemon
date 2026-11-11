"""Round 159: secret_scan scope data checks jobs.db; fix=true masks stored results and errors.

Job results and errors written before v0.148 are raw in jobs.db and every /api/fleet/jobs read
served them. secret_scan never looked inside the database. Payloads are reported, never rewritten.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import types

import pytest

from avrone import jobqueue as jq
from avrone import tools_secrets as ts


def _tok() -> str:  # built at run time: no token-shaped literal for push protection
    return "gh" + "p_" + "B" * 36


def _ctx(d):
    return types.SimpleNamespace(core=types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=d), runner=None))


def _seed(d, payload_secret=True):
    q = jq.JobQueue(d / "jobs.db")

    async def m():
        await q.enqueue("run", {"task": ("use " + _tok()) if payload_secret else "plain"})
        await q.close()
    asyncio.run(m())
    db = sqlite3.connect(d / "jobs.db")
    db.execute("UPDATE jobs SET status='done', result=?, error=?",
               (json.dumps({"answer": "key " + _tok(), "n": 3}), "boom " + _tok()))
    db.commit()
    db.close()


def test_scan_reports_and_fix_masks(tmp_path):
    _seed(tmp_path)
    out = asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "data"}))
    assert "FOUND" in out and "1 job(s) with a secret" in out and "payload" in out
    out = asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "data", "fix": True}))
    assert "fixed 1" in out
    res, err, payload = sqlite3.connect(tmp_path / "jobs.db").execute("select result, error, payload from jobs").fetchone()
    assert _tok() not in res and _tok() not in err and json.loads(res)["n"] == 3
    assert _tok() in payload  # task input is never rewritten


def test_queue_still_reads_after_fix(tmp_path):
    _seed(tmp_path, payload_secret=False)
    asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "data", "fix": True}))
    q = jq.JobQueue(tmp_path / "jobs.db")

    async def m():
        rows = await q.list(10) if hasattr(q, "list") else None
        await q.close()
        return rows
    asyncio.run(m())
    assert "CLEAN" in asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "data"}))


def test_missing_db_not_created_and_corrupt_reported(tmp_path):
    sd = getattr(jq, "scrub_db", None)
    if sd is None:
        pytest.fail("jobqueue.scrub_db missing")
    assert sd(tmp_path / "jobs.db")["rows"] == 0 and not (tmp_path / "jobs.db").exists()
    (tmp_path / "jobs.db").write_bytes(b"garbage" * 100)
    out = asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "data"}))
    assert "jobs.db: NOT scanned" in out and "CLEAN" in out.splitlines()[0]
