"""HTTP framing regressions for the authenticated evidence receiver."""
from __future__ import annotations

import asyncio
import hashlib
import time

import httpx
from fastapi import FastAPI

from avrone.evidence_receiver import canonical, ingest_evidence, request_signature
from avrone.integration_contract import build_evidence_event

SECRET = "receiver-content-length-secret-" + "s" * 40
RECEIPT_SECRET = "receiver-content-length-receipt-" + "r" * 40
PATH = "/api/evidence/events"


def _event():
    return build_evidence_event(
        source="content-length-tests", run_id="framing-001", check_id="http-framing",
        failures=1, runs=10, refs=["test:http-framing"],
    )


def _headers(body: bytes):
    timestamp = str(int(time.time()))
    nonce = hashlib.sha256(str(time.monotonic_ns()).encode()).hexdigest()[:32]
    signature = request_signature(SECRET, "POST", PATH, timestamp, nonce, body)
    return {
        "content-type": "application/json",
        "x-avrone-evidence-timestamp": timestamp,
        "x-avrone-evidence-nonce": nonce,
        "x-avrone-evidence-signature": signature,
    }


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_EVIDENCE_HMAC_SECRET", SECRET)
    monkeypatch.setenv("AVRONE_EVIDENCE_RECEIPT_SECRET", RECEIPT_SECRET)
    monkeypatch.setenv("AVRONE_EVIDENCE_DB", str(tmp_path / "framing.sqlite3"))
    app = FastAPI()
    app.add_api_route(PATH, ingest_evidence, methods=["POST"])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_missing_content_length_is_accepted_and_body_is_authenticated(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            event = _event()
            body = canonical(event)
            request = client.build_request("POST", PATH, content=body, headers=_headers(body))
            request.headers.pop("content-length", None)
            response = await client.send(request)
            assert response.status_code == 201
            assert response.json()["accepted"] is True
            assert response.json()["receipt"]["event_digest"] == event["digest"]
    asyncio.run(go())


def test_malformed_and_oversized_content_length_fail_closed(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            body = canonical(_event())
            for declared in ("+" + str(len(body)), "-1", "1 2", "1,2", "9" * 13):
                headers = _headers(body)
                headers["content-length"] = declared
                response = await client.post(PATH, content=body, headers=headers)
                assert response.status_code == 400, (declared, response.status_code)
            headers = _headers(body)
            headers["content-length"] = str(256 * 1024 + 1)
            response = await client.post(PATH, content=body, headers=headers)
            assert response.status_code == 413
    asyncio.run(go())
