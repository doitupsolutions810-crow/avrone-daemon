"""Authenticated, durable, idempotent receiver for Great Scale evidence v1."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from .integration_contract import verify_evidence_event

MAX_BODY = 256 * 1024
MAX_CLOCK_SKEW = 300
NONCE_TTL = 600


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def request_signature(secret: str, method: str, path: str, timestamp: str,
                      nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = "\n".join((method.upper(), path, timestamp, nonce, body_hash)).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_receipt(secret: str, receipt: dict[str, Any]) -> bool:
    if not isinstance(receipt, dict) or set(receipt) != {
        "receipt_id", "event_digest", "stored_at", "duplicate", "signature"
    }:
        return False
    receipt_id = receipt.get("receipt_id")
    digest = receipt.get("event_digest")
    stored_at = receipt.get("stored_at")
    duplicate = receipt.get("duplicate")
    signature = receipt.get("signature")
    if not isinstance(receipt_id, str):
        return False
    try:
        if str(uuid.UUID(receipt_id)) != receipt_id:
            return False
    except (ValueError, AttributeError):
        return False
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or not isinstance(stored_at, int) or isinstance(stored_at, bool) or stored_at < 0
            or not isinstance(duplicate, bool)
            or not isinstance(signature, str) or len(signature) != 64
            or any(c not in "0123456789abcdef" for c in signature)):
        return False
    unsigned = {k: v for k, v in receipt.items() if k != "signature"}
    try:
        expected = hmac.new(secret.encode(), canonical(unsigned), hashlib.sha256).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return False
    return hmac.compare_digest(signature, expected)


class EvidenceStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS evidence (
                    digest TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    check_id TEXT NOT NULL,
                    event_json BLOB NOT NULL,
                    receipt_json BLOB NOT NULL,
                    stored_at INTEGER NOT NULL,
                    UNIQUE(source, run_id, check_id)
                );
                CREATE TABLE IF NOT EXISTS request_nonces (
                    nonce TEXT PRIMARY KEY,
                    seen_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_stored_at ON evidence(stored_at);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        # synchronous is connection-local in SQLite; apply FULL to every writer connection.
        db.execute("PRAGMA synchronous=FULL")
        db.row_factory = sqlite3.Row
        return db

    def accept(self, event: dict[str, Any], nonce: str, now: int,
               receipt_secret: str) -> tuple[dict[str, Any], bool]:
        valid, _ = verify_evidence_event(event)
        if not valid:
            raise ValueError("evidence contract rejected")
        digest = event["digest"]
        event_blob = canonical(event)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM request_nonces WHERE seen_at < ?", (now - NONCE_TTL,))
            try:
                db.execute("INSERT INTO request_nonces(nonce, seen_at) VALUES (?, ?)", (nonce, now))
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ValueError("replayed nonce") from exc
            row = db.execute("SELECT * FROM evidence WHERE digest = ?", (digest,)).fetchone()
            if row:
                if bytes(row["event_json"]) != event_blob:
                    db.rollback()
                    raise ValueError("digest collision")
                db.commit()
                receipt = json.loads(bytes(row["receipt_json"]))
                receipt["duplicate"] = True
                receipt.pop("signature", None)
                receipt["signature"] = hmac.new(receipt_secret.encode(), canonical(receipt),
                                                  hashlib.sha256).hexdigest()
                return receipt, True
            conflict = db.execute(
                "SELECT digest FROM evidence WHERE source=? AND run_id=? AND check_id=?",
                (event["source"], event["run_id"], event["check_id"]),
            ).fetchone()
            if conflict:
                db.rollback()
                raise KeyError("evidence identity conflict")
            unsigned = {"receipt_id": str(uuid.uuid4()), "event_digest": digest,
                        "stored_at": now, "duplicate": False}
            receipt = {**unsigned, "signature": hmac.new(
                receipt_secret.encode(), canonical(unsigned), hashlib.sha256).hexdigest()}
            db.execute(
                "INSERT INTO evidence(digest,source,run_id,check_id,event_json,receipt_json,stored_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (digest, event["source"], event["run_id"], event["check_id"],
                 event_blob, canonical(receipt), now),
            )
            db.commit()
            return receipt, False

    def count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])


def _strict_json(raw: bytes) -> Any:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def bad_constant(_):
        raise ValueError("non-finite JSON number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=bad_constant)


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        return None
    return values[0] if values else ""


def _config():
    secret = os.environ.get("AVRONE_EVIDENCE_HMAC_SECRET", "")
    receipt_secret = os.environ.get("AVRONE_EVIDENCE_RECEIPT_SECRET", "")
    db_path = os.environ.get("AVRONE_EVIDENCE_DB", "")
    if (len(secret) < 32 or len(receipt_secret) < 32 or not db_path
            or db_path == ":memory:" or not Path(db_path).is_absolute()):
        raise HTTPException(503, "evidence receiver is not configured")
    return secret, receipt_secret, db_path


async def ingest_evidence(request: Request):
    secret, receipt_secret, db_path = _config()
    content_type = _single_header(request, "content-type")
    media_type = content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else ""
    if media_type != "application/json":
        raise HTTPException(415, "evidence content type must be application/json")
    content_length = _single_header(request, "content-length")
    if content_length is None:
        raise HTTPException(400, "ambiguous content length")
    if content_length:
        if not content_length.isascii() or not content_length.isdecimal() or len(content_length) > 12:
            raise HTTPException(400, "invalid content length")
        if int(content_length) > MAX_BODY:
            raise HTTPException(413, "evidence event too large")
    chunks, size = [], 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise HTTPException(413, "evidence event too large")
            chunks.append(chunk)
    except ClientDisconnect:
        raise HTTPException(400, "incomplete evidence request body")
    raw = b"".join(chunks)
    if content_length and int(content_length) != size:
        raise HTTPException(400, "content length does not match received body")
    timestamp = _single_header(request, "x-avrone-evidence-timestamp")
    nonce = _single_header(request, "x-avrone-evidence-nonce")
    signature = _single_header(request, "x-avrone-evidence-signature")
    if (not isinstance(timestamp, str) or not timestamp.isascii()
            or not timestamp.isdecimal() or len(timestamp) > 12
            or not isinstance(nonce, str) or not nonce.isascii() or not nonce.isalnum()
            or not 16 <= len(nonce) <= 64
            or not isinstance(signature, str) or len(signature) != 64):
        raise HTTPException(401, "invalid evidence authentication")
    now = int(time.time())
    if abs(now - int(timestamp)) > MAX_CLOCK_SKEW:
        raise HTTPException(401, "evidence timestamp outside allowed clock skew")
    expected = request_signature(secret, request.method, request.url.path, timestamp, nonce, raw)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "invalid evidence authentication")
    try:
        event = _strict_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise HTTPException(400, "invalid evidence JSON")
    valid, _ = verify_evidence_event(event)
    if not valid:
        raise HTTPException(422, "evidence contract rejected")
    try:
        store = EvidenceStore(db_path)
        receipt, duplicate = await anyio.to_thread.run_sync(
            store.accept, event, nonce, now, receipt_secret
        )
    except ValueError:
        raise HTTPException(409, "replayed nonce or invalid evidence")
    except KeyError:
        raise HTTPException(409, "evidence identity conflict")
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "evidence storage unavailable")
    return JSONResponse({"accepted": True, "duplicate": duplicate, "receipt": receipt},
                        status_code=200 if duplicate else 201)
