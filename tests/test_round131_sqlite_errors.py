"""Round 131: sqlite storage failures read as plain ERROR lines; /api/fleet/jobs never bare-500s.

v0.123: a full disk made queue_submit answer "ERROR: OperationalError: disk I/O error" (probe
/workspace/work/jqfull.py), and GET /api/fleet/jobs raised AttributeError (queue never started) or
OperationalError (broken jobs.db) as a 500.
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from avrone import server, tools


@pytest.mark.parametrize("msg,hint,retry", [
    ("database or disk is full", "disk is full", False),
    ("disk I/O error", "I/O error", False),
    ("attempt to write a readonly database", "read-only", False),
    ("database is locked", "busy", True),
    ("database disk image is malformed", "damaged", False),
])
def test_error_text_sqlite(msg, hint, retry):
    out = tools.error_text(sqlite3.OperationalError(msg))
    assert out.startswith("ERROR: database write or read failed:"), out
    assert hint in out and msg in out
    assert ("retried in a moment" in out) is retry


def test_other_sqlite_errors_unchanged():
    assert tools.error_text(sqlite3.OperationalError("no such table: x")) == \
        "ERROR: OperationalError: no such table: x"
    assert tools.error_text(ValueError("x")) == "ERROR: ValueError: x"


def _route(monkeypatch, fleet):
    monkeypatch.setattr(server, "require_admin", lambda tok: None)
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=fleet))


def test_jobs_route_queue_missing(monkeypatch):
    _route(monkeypatch, SimpleNamespace(enabled=True, queue=None))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server.fleet_jobs(None, None))
    assert ei.value.status_code == 503


class _BadQ:
    async def stats(self):
        raise sqlite3.OperationalError("disk I/O error")

    async def get(self, jid):
        raise sqlite3.DatabaseError("file is not a database")


def test_jobs_route_db_errors(monkeypatch):
    _route(monkeypatch, SimpleNamespace(enabled=True, queue=_BadQ()))
    for jid, word in ((None, "I/O error"), ("abc", "damaged")):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(server.fleet_jobs(jid, None))
        assert ei.value.status_code == 503 and word in ei.value.detail
        assert not ei.value.detail.startswith("ERROR")


class _OkQ:
    async def stats(self):
        return {"queued": 0}

    async def get(self, jid):
        return None


def test_jobs_route_normal(monkeypatch):
    _route(monkeypatch, SimpleNamespace(enabled=True, queue=_OkQ()))
    assert asyncio.run(server.fleet_jobs(None, None)) == {"queued": 0}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server.fleet_jobs("nope", None))
    assert ei.value.status_code == 404
