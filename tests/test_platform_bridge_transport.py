"""Exercise the real bridge HTTP/parser path with adversarial byte streams."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.test_platform_bridge import _load_bridge, _call


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, stall=False):
        self.chunks = chunks
        self.stall = stall
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.stall:
            await asyncio.Future()

    async def aclose(self):
        self.closed = True


def install_transport(monkeypatch, module, handler):
    original_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", '{"runtime":"http://runtime:8700"}')
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_TOKEN", "avr_test_transport")


def request(module, *, method="GET", body=None):
    return json.loads(asyncio.run(module._request(
        "runtime", method, "/v1/health", body,
        idempotency_key="stable-request" if method == "POST" else None,
    )))


@pytest.mark.parametrize("status", [200, 502])
def test_unknown_length_stream_stops_at_cap_and_closes(monkeypatch, status):
    _registry, module = _load_bridge()
    stream = TrackingStream([b"x" * module.MAX_RESPONSE_BYTES, b"overflow", b"must-not-read"])

    def handler(req):
        assert req.headers["accept-encoding"] == "identity"
        return httpx.Response(status, stream=stream)

    install_transport(monkeypatch, module, handler)
    result = request(module, method="POST", body={"name": "m"})
    assert result["ok"] is False and "exceeded" in result["error"]
    assert result["http_status"] == status
    assert result["transport_ambiguous"] is True
    assert stream.reads == 2 and stream.closed


@pytest.mark.parametrize("length", ["65537", "9" * 5000])
def test_declared_oversize_response_is_never_read(monkeypatch, length):
    _registry, module = _load_bridge()
    stream = TrackingStream([b"must-not-read"])
    install_transport(monkeypatch, module, lambda req: httpx.Response(
        200, headers={"Content-Length": length}, stream=stream,
    ))
    result = request(module)
    assert result["ok"] is False and "exceeded" in result["error"]
    assert stream.reads == 0 and stream.closed


def test_exact_cap_identity_text_response_succeeds(monkeypatch):
    _registry, module = _load_bridge()
    stream = TrackingStream([b"x" * module.MAX_RESPONSE_BYTES])
    install_transport(monkeypatch, module, lambda req: httpx.Response(
        200, headers={"Content-Type": "text/plain"}, stream=stream,
    ))
    result = request(module)
    assert result["ok"] is True
    assert result["data"] == "x" * module.MAX_TEXT_CHARS
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "gzip, identity"])
def test_unsolicited_compression_is_rejected_before_decoding(monkeypatch, encoding):
    _registry, module = _load_bridge()
    stream = TrackingStream([b"not-a-valid-compressed-stream"])
    install_transport(monkeypatch, module, lambda req: httpx.Response(
        200, headers={"Content-Encoding": encoding}, stream=stream,
    ))
    result = request(module)
    assert result["ok"] is False and "content encoding" in result["error"]
    assert stream.reads == 0 and stream.closed


def test_total_deadline_closes_a_stalling_stream(monkeypatch):
    _registry, module = _load_bridge()
    monkeypatch.setattr(module, "TOTAL_REQUEST_SECONDS", 0.02)
    stream = TrackingStream([b"{"], stall=True)
    install_transport(monkeypatch, module, lambda req: httpx.Response(200, stream=stream))
    result = request(module, method="POST", body={"name": "m"})
    assert result["ok"] is False and "TimeoutError" in result["error"]
    assert result["transport_ambiguous"] is True and stream.closed


@pytest.mark.parametrize("raw", [
    b'{"mission_id":"first","mission_id":"second"}',
    b'{"value":NaN}', b'{"value":Infinity}', b'{"value":1e9999}',
    b'{"value":"\\ud800"}', b'{"value":"\xff"}', b'{"value":',
    b"[" * 65 + b"0" + b"]" * 65,
    b"[" * 1600 + b"0" + b"]" * 1600,
])
@pytest.mark.parametrize("content_type", ["application/json", "application/problem+json; charset=utf-8"])
def test_invalid_json_and_utf8_fail_without_crashing(monkeypatch, raw, content_type):
    _registry, module = _load_bridge()
    install_transport(monkeypatch, module, lambda req: httpx.Response(
        201, headers={"Content-Type": content_type}, content=raw,
    ))
    result = request(module, method="POST", body={"name": "m"})
    assert result["ok"] is False
    assert result["transport_ambiguous"] is True
    assert result["http_status"] == 201


def test_error_json_remains_bounded_and_classified_as_untrusted(monkeypatch):
    _registry, module = _load_bridge()
    install_transport(monkeypatch, module, lambda req: httpx.Response(
        409, json={"error": "conflict " + "x" * 1000},
    ))
    result = request(module)
    assert result["ok"] is False and result["http_status"] == 409
    assert len(result["remote_error"]) == 300
    assert "untrusted" in result["trust_boundary"]


def test_server_error_after_write_remains_ambiguous(monkeypatch):
    _registry, module = _load_bridge()
    install_transport(monkeypatch, module, lambda req: httpx.Response(503, json={"error": "unavailable"}))
    result = request(module, method="POST", body={"name": "m"})
    assert result["ok"] is False and result["http_status"] == 503
    assert result["transport_ambiguous"] is True


def test_swarm_successful_status_without_response_object_is_ambiguous(monkeypatch):
    registry, module = _load_bridge()
    install_transport(monkeypatch, module, lambda req: httpx.Response(201, json=["unexpected"]))
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON", "[]")
    result = json.loads(_call(registry, "platform_bridge_create_runtime_swarm", {
        "mission_name": "M", "objective": "O", "swarm_name": "S", "idempotency_key": "stable",
    }))
    assert result["ok"] is False and result["phase"] == "mission"
    assert result["transport_ambiguous"] is True
    assert "same idempotency_key" in result["recovery"]


@pytest.mark.parametrize("token", ["sëcret", "bad token", "a\r\nInjected: value", "x" * 8193])
def test_invalid_bearer_headers_fail_before_transport(monkeypatch, token):
    _registry, module = _load_bridge()

    def forbidden(req):
        raise AssertionError("invalid local token must not reach transport")

    install_transport(monkeypatch, module, forbidden)
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_TOKEN", token)
    result = request(module, method="POST", body={"name": "m"})
    assert result["ok"] is False and "local validation" in result["error"]
    assert token not in json.dumps(result)
    assert "transport_ambiguous" not in result


@pytest.mark.parametrize("body", [{"name": "\ud800"}, {"value": float("nan")}])
def test_unencodable_local_payload_fails_before_transport(monkeypatch, body):
    _registry, module = _load_bridge()

    def forbidden(req):
        raise AssertionError("invalid local JSON must not reach transport")

    install_transport(monkeypatch, module, forbidden)
    result = request(module, method="POST", body=body)
    assert result["ok"] is False and "local validation" in result["error"]
    assert "transport_ambiguous" not in result


def test_deep_operator_config_and_hidden_url_controls_fail_closed(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", "[" * 1600 + "0" + "]" * 1600)
    assert module._targets() == {}
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", json.dumps({"runtime": "http://run\ntime:8700"}))
    assert module._targets() == {}
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", '{"runtime":"http://runtime:8700"}')
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_TOKEN", "avr_test_transport")
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON", "[" * 1600 + "0" + "]" * 1600)
    result = _call(registry, "platform_bridge_create_runtime_swarm", {
        "mission_name": "M", "objective": "O", "swarm_name": "S", "idempotency_key": "stable",
    })
    assert result.startswith("ERROR:") and "is invalid" in result


def test_swarm_retry_reuses_phase_keys_after_lost_task_response(monkeypatch):
    registry, module = _load_bridge()
    stored = {}
    deliveries = []
    lost_task_response = False

    def handler(req):
        nonlocal lost_task_response
        identity = (req.url.path, req.headers["idempotency-key"])
        payload = json.loads(req.content)
        deliveries.append(identity)
        if identity in stored:
            previous_payload, response = stored[identity]
            assert payload == previous_payload
        else:
            fields = {"/v1/missions": "mission_id", "/v1/tasks": "task_id", "/v1/swarms": "swarm_id"}
            response = {fields[req.url.path]: "stable_" + fields[req.url.path]}
            stored[identity] = (payload, response)
        if req.url.path == "/v1/tasks" and not lost_task_response:
            lost_task_response = True
            # Model a task that committed but whose acknowledgement was lost.
            raise httpx.ReadError("lost response", request=req)
        return httpx.Response(201, json=response)

    install_transport(monkeypatch, module, handler)
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON", "[]")
    args = {"mission_name": "M", "objective": "O", "swarm_name": "S", "idempotency_key": "swarm-retry"}
    first = json.loads(_call(registry, "platform_bridge_create_runtime_swarm", args))
    assert first["ok"] is False and first["phase"] == "task"
    assert first["partial"] is True and first["transport_ambiguous"] is True
    assert "same idempotency_key" in first["recovery"]
    second = json.loads(_call(registry, "platform_bridge_create_runtime_swarm", args))
    assert second["ok"] is True and second["idempotent"] is True
    assert len(stored) == 3
    assert deliveries == [
        ("/v1/missions", "swarm-retry.mission"),
        ("/v1/tasks", "swarm-retry.task"),
        ("/v1/missions", "swarm-retry.mission"),
        ("/v1/tasks", "swarm-retry.task"),
        ("/v1/swarms", "swarm-retry.swarm"),
    ]
