"""Adversarial JSON parser regressions for the authenticated evidence receiver."""
from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from avrone.evidence_receiver import canonical, ingest_evidence
from avrone.integration_contract import build_evidence_event

SECRET = "receiver-json-adversarial-secret-" + "s" * 40
RECEIPT_SECRET = "receiver-json-adversarial-receipt-" + "r" * 40
PATH = "/api/evidence/events"


def _event():
    return build_evidence_event(
        source="json-adversarial",
        run_id="json-run-001",
        check_id="strict-json",
        failures=2,
        runs=20,
        refs=["test:json-adversarial"],
    )


def _headers(raw: bytes):
    import hashlib
    import time
    from avrone.evidence_receiver import request_signature

    timestamp = str(int(time.time()))
    nonce = "jsonadversarialnonce000000000001"
    return {
        "content-type": "application/json",
        "x-avrone-evidence-timestamp": timestamp,
        "x-avrone-evidence-nonce": nonce,
        "x-avrone-evidence-signature": request_signature(
            SECRET, "POST", PATH, timestamp, nonce, raw
        ),
    }


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_EVIDENCE_HMAC_SECRET", SECRET)
    monkeypatch.setenv("AVRONE_EVIDENCE_RECEIPT_SECRET", RECEIPT_SECRET)
    monkeypatch.setenv("AVRONE_EVIDENCE_DB", str(tmp_path / "json-adversarial.sqlite3"))
    app = FastAPI()
    app.add_api_route(PATH, ingest_evidence, methods=["POST"])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_escaped_lone_surrogate_in_identifier_is_rejected(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            raw = canonical(_event()).replace(
                b'"source":"json-adversarial"', b'"source":"\\ud800"'
            )
            response = await client.post(PATH, content=raw, headers=_headers(raw))
            assert response.status_code == 422
    asyncio.run(go())


def test_escaped_control_character_in_identifier_is_rejected(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            raw = canonical(_event()).replace(
                b'"source":"json-adversarial"', b'"source":"bad\\u0000id"'
            )
            response = await client.post(PATH, content=raw, headers=_headers(raw))
            assert response.status_code == 422
    asyncio.run(go())


def test_exponent_overflow_is_rejected_without_server_error(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            raw = canonical(_event()).replace(b'"p":0.1', b'"p":1e100000')
            assert raw != canonical(_event())
            response = await client.post(PATH, content=raw, headers=_headers(raw))
            assert response.status_code == 422
    asyncio.run(go())


def test_extreme_integer_token_is_a_controlled_bad_request(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            raw = canonical(_event()).replace(
                b'"failures":2', b'"failures":' + (b"9" * 5000)
            )
            response = await client.post(PATH, content=raw, headers=_headers(raw))
            assert response.status_code == 400
    asyncio.run(go())


def test_unknown_top_level_field_is_rejected(tmp_path, monkeypatch):
    async def go():
        async with _client(tmp_path, monkeypatch) as client:
            event = _event()
            event["unrecognized_extension"] = {"nested": [1, 2, 3]}
            raw = canonical(event)
            response = await client.post(PATH, content=raw, headers=_headers(raw))
            assert response.status_code == 422
    asyncio.run(go())
