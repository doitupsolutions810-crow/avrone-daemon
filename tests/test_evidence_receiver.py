from __future__ import annotations

import asyncio
import hashlib
import hmac
import random
import threading
import time
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from avrone.evidence_receiver import (
    EvidenceStore, canonical, request_signature, ingest_evidence, verify_receipt,
)
from avrone.integration_contract import build_evidence_event

SECRET = "receiver-test-secret-" + "s" * 40
RECEIPT_SECRET = "receipt-test-secret-" + "r" * 40
PATH = "/api/evidence/events"


def event(**kw):
    data = dict(source="daemon-tests", run_id="run-001", check_id="contract",
                failures=2, runs=20, refs=["test:receiver"])
    data.update(kw)
    return build_evidence_event(**data)


def headers(body, nonce=None, timestamp=None, secret=SECRET):
    ts = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or hashlib.sha256(str(time.monotonic_ns()).encode()).hexdigest()[:32]
    return {
        "content-type": "application/json",
        "x-avrone-evidence-timestamp": ts,
        "x-avrone-evidence-nonce": nonce,
        "x-avrone-evidence-signature": request_signature(secret, "POST", PATH, ts, nonce, body),
    }


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_EVIDENCE_HMAC_SECRET", SECRET)
    monkeypatch.setenv("AVRONE_EVIDENCE_RECEIPT_SECRET", RECEIPT_SECRET)
    monkeypatch.setenv("AVRONE_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))
    app = FastAPI()
    app.add_api_route(PATH, ingest_evidence, methods=["POST"])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def run(coro):
    return asyncio.run(coro)


def test_persist_and_idempotent_receipt(tmp_path):
    store = EvidenceStore(tmp_path / "nested" / "evidence.db")
    with closing(store._connect()) as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    e = event()
    first, dup = store.accept(e, "nonce-first-000001", int(time.time()), RECEIPT_SECRET)
    assert not dup and verify_receipt(RECEIPT_SECRET, first)
    again, dup = EvidenceStore(store.path).accept(e, "nonce-second-00001", int(time.time()), RECEIPT_SECRET)
    assert dup and first["receipt_id"] == again["receipt_id"]
    assert verify_receipt(RECEIPT_SECRET, again)
    assert store.count() == 1


def test_api_accepts_and_retries_same_event(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            body = canonical(event())
            a = await c.post(PATH, content=body, headers=headers(body))
            b = await c.post(PATH, content=body, headers=headers(body))
            assert a.status_code == 201 and b.status_code == 200
            assert a.json()["receipt"]["receipt_id"] == b.json()["receipt"]["receipt_id"]
            assert verify_receipt(RECEIPT_SECRET, b.json()["receipt"])
    run(go())


@pytest.mark.parametrize("mutate", [
    lambda h: h.update({"x-avrone-evidence-signature": "0" * 64}),
    lambda h: h.update({"x-avrone-evidence-nonce": "bad"}),
    lambda h: h.update({"x-avrone-evidence-timestamp": "NaN"}),
])
def test_auth_headers_fail_closed(tmp_path, monkeypatch, mutate):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            body = canonical(event())
            h = headers(body); mutate(h)
            assert (await c.post(PATH, content=body, headers=h)).status_code == 401
    run(go())


def test_clock_skew_and_wrong_secret_rejected(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            body = canonical(event())
            assert (await c.post(PATH, content=body, headers=headers(body, timestamp=int(time.time())-1000))).status_code == 401
            assert (await c.post(PATH, content=body, headers=headers(body, secret="x"+SECRET))).status_code == 401
    run(go())


def test_reused_nonce_conflict_and_contract_tamper(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            e = event(); body = canonical(e); h = headers(body)
            assert (await c.post(PATH, content=body, headers=h)).status_code == 201
            assert (await c.post(PATH, content=body, headers=h)).status_code == 409
            conflict = canonical(event(failures=3))
            assert (await c.post(PATH, content=conflict, headers=headers(conflict))).status_code == 409
            altered = dict(e); altered["evidence"] = dict(e["evidence"], failures=0)
            bad = canonical(altered)
            assert (await c.post(PATH, content=bad, headers=headers(bad))).status_code == 422
    run(go())


def test_strict_json_media_type_and_body_size(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            body = canonical(event())
            h = headers(body); h["content-type"] = "text/plain"
            assert (await c.post(PATH, content=body, headers=h)).status_code == 415
            for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff'):
                assert (await c.post(PATH, content=raw, headers=headers(raw))).status_code == 400
            huge = b"x" * (256 * 1024 + 1)
            assert (await c.post(PATH, content=huge, headers=headers(huge))).status_code == 413
    run(go())


def test_unconfigured_or_nonpersistent_database_fails_closed(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setenv("AVRONE_EVIDENCE_RECEIPT_SECRET", RECEIPT_SECRET)
        monkeypatch.setenv("AVRONE_EVIDENCE_DB", ":memory:")
        app = FastAPI(); app.add_api_route(PATH, ingest_evidence, methods=["POST"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            body = canonical(event())
            assert (await c.post(PATH, content=body, headers=headers(body))).status_code == 503
            monkeypatch.setenv("AVRONE_EVIDENCE_HMAC_SECRET", SECRET)
            monkeypatch.setenv("AVRONE_EVIDENCE_DB", "relative.sqlite3")
            assert (await c.post(PATH, content=body, headers=headers(body))).status_code == 503
    run(go())


def test_concurrent_same_event_is_one_durable_record(tmp_path):
    store = EvidenceStore(tmp_path / "events.db")
    e = event(); barrier = threading.Barrier(12); results = []; errors = []
    def worker(i):
        try:
            barrier.wait()
            results.append(store.accept(e, f"nonce-thread-{i:04d}", int(time.time()), RECEIPT_SECRET))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads: t.start()
    for t in threads: t.join(15)
    assert not any(t.is_alive() for t in threads)
    assert not errors and len(results) == 12
    assert sum(not duplicate for _, duplicate in results) == 1
    assert len({r["receipt_id"] for r, _ in results}) == 1
    assert all(verify_receipt(RECEIPT_SECRET, r) for r, _ in results)
    assert store.count() == 1


def test_nonce_retention_expiry(tmp_path):
    store = EvidenceStore(tmp_path / "events.db")
    store.accept(event(), "nonce-expire-00001", 1000, RECEIPT_SECRET)
    r, dup = store.accept(event(run_id="run-002"), "nonce-expire-00001", 1700, RECEIPT_SECRET)
    assert not dup and verify_receipt(RECEIPT_SECRET, r)


def test_signature_binds_method_path_time_nonce_and_body():
    body = canonical(event()); ts = "1700000000"; nonce = "a" * 32
    good = request_signature(SECRET, "POST", PATH, ts, nonce, body)
    assert len(good) == 64
    for args in [
        ("GET", PATH, ts, nonce, body),
        ("POST", PATH + "/", ts, nonce, body),
        ("POST", PATH, str(int(ts)+1), nonce, body),
        ("POST", PATH, ts, "b"*32, body),
        ("POST", PATH, ts, nonce, body + b" "),
    ]:
        assert not hmac.compare_digest(good, request_signature(SECRET, *args))


def test_seeded_mac_mutation_corpus():
    rng = random.Random(0xA7E0E)
    body = canonical(event())
    good = request_signature(SECRET, "POST", PATH, "1700000000", "a"*32, body)
    for _ in range(5000):
        candidate = list(good)
        for _ in range(rng.randint(1, 4)):
            i = rng.randrange(64); candidate[i] = rng.choice("0123456789abcdef")
        forged = "".join(candidate)
        if forged != good:
            assert not hmac.compare_digest(good, forged)
    for _ in range(2000):
        altered = bytearray(body); i = rng.randrange(len(altered)); altered[i] ^= 1 << rng.randrange(8)
        forged = request_signature(SECRET, "POST", PATH, "1700000000", "a"*32, bytes(altered))
        assert not hmac.compare_digest(good, forged)


def test_server_exposes_receiver_route():
    from avrone.server import app
    assert sum(1 for route in app.routes
               if getattr(route, "path", None) == PATH
               and "POST" in getattr(route, "methods", set())) == 1


def test_content_length_must_match_streamed_body(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            body = canonical(event())
            for declared in (str(len(body)-1), str(len(body)+1)):
                h = headers(body)
                h["content-length"] = declared
                response = await c.post(PATH, content=body, headers=h)
                assert response.status_code == 400
            h = headers(body)
            h["content-length"] = "000" + str(len(body))
            assert (await c.post(PATH, content=body, headers=h)).status_code == 201
    run(go())


def test_deeply_nested_and_top_level_non_object_json_rejected(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            cases = [b"[" * 2000 + b"0" + b"]" * 2000, b"null", b"[]", b'"string"']
            for raw in cases:
                response = await c.post(PATH, content=raw, headers=headers(raw))
                assert response.status_code in (400, 422)
                assert response.status_code != 201
    run(go())


def test_receipt_tamper_and_wrong_secret_fail_verification():
    unsigned = {"receipt_id": "8c3a3d2c-1f7d-4a61-9e0d-5f4d9a0c72a1",
                "event_digest": "a"*64, "stored_at": 1, "duplicate": False}
    sig = hmac.new(RECEIPT_SECRET.encode(), canonical(unsigned), hashlib.sha256).hexdigest()
    receipt = {**unsigned, "signature": sig}
    assert verify_receipt(RECEIPT_SECRET, receipt)
    for secret, altered in [
        ("wrong-" + RECEIPT_SECRET, receipt),
        (RECEIPT_SECRET, {**receipt, "event_digest": "b"*64}),
        (RECEIPT_SECRET, {**receipt, "extra": True}),
        (RECEIPT_SECRET, {**receipt, "signature": "0"*64}),
        (RECEIPT_SECRET, {**receipt, "stored_at": True}),
        (RECEIPT_SECRET, {**receipt, "receipt_id": "not-a-uuid"}),
        (RECEIPT_SECRET, {**receipt, "event_digest": "Z"*64}),
        (RECEIPT_SECRET, {**receipt, "extra": object()}),
    ]:
        assert not verify_receipt(secret, altered)


def test_duplicate_security_headers_fail_closed(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            body = canonical(event())
            h = list(headers(body).items())
            h.extend([("x-avrone-evidence-signature", "0"*64)])
            response = await c.post(PATH, content=body, headers=h)
            assert response.status_code == 401
            h = list(headers(body).items())
            h.append(("content-type", "text/plain"))
            response = await c.post(PATH, content=body, headers=h)
            assert response.status_code == 415
            ts = str(int(time.time()))
            nonce = "c" * 32
            signature = request_signature(SECRET, "POST", PATH, ts, nonce, body)
            raw_headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"content-length", str(len(body)).encode()),
                (b"x-avrone-evidence-timestamp", ts.encode()),
                (b"x-avrone-evidence-nonce", nonce.encode()),
                (b"x-avrone-evidence-signature", signature.encode()),
            ]
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": PATH,
                "raw_path": PATH.encode(), "query_string": b"", "headers": raw_headers,
                "client": ("127.0.0.1", 12345), "server": ("test", 80),
            }
            delivered = False
            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.disconnect"}
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            request = Request(scope, receive)
            with pytest.raises(HTTPException) as exc:
                await ingest_evidence(request)
            assert exc.value.status_code == 400
    run(go())


def test_unwritable_database_parent_returns_service_unavailable(tmp_path, monkeypatch):
    async def go():
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")
        monkeypatch.setenv("AVRONE_EVIDENCE_HMAC_SECRET", SECRET)
        monkeypatch.setenv("AVRONE_EVIDENCE_RECEIPT_SECRET", RECEIPT_SECRET)
        monkeypatch.setenv("AVRONE_EVIDENCE_DB", str(blocker / "evidence.db"))
        app = FastAPI(); app.add_api_route(PATH, ingest_evidence, methods=["POST"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            body = canonical(event())
            response = await c.post(PATH, content=body, headers=headers(body))
            assert response.status_code == 503
    run(go())


def test_concurrent_api_ingest_is_one_logical_commit(tmp_path, monkeypatch):
    async def go():
        async with client(tmp_path, monkeypatch) as c:
            body = canonical(event())
            responses = await asyncio.gather(*[
                c.post(PATH, content=body, headers=headers(body))
                for _ in range(16)
            ])
            assert all(r.status_code in (200, 201) for r in responses), [
                (r.status_code, r.text) for r in responses
            ]
            assert sum(r.status_code == 201 for r in responses) == 1
            ids = {r.json()["receipt"]["receipt_id"] for r in responses}
            assert len(ids) == 1
            assert all(verify_receipt(RECEIPT_SECRET, r.json()["receipt"])
                       for r in responses)
    run(go())
