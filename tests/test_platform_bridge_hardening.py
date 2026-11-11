"""Additional adversarial coverage for the cross-repository HTTP bridge.

These tests are intentionally network-free. They assert that hostile IDs, bad
operator URLs, and unsafe HTTP header values fail before any remote side effect.
"""
from __future__ import annotations

import json

from tests.test_platform_bridge import _load_bridge, _call


def test_runtime_mission_status_rejects_dot_segments(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", '{"runtime":"http://runtime:8700"}')
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_TOKEN", "avr_test_tenant")
    calls = []

    async def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({"ok": True})

    monkeypatch.setattr(module, "_request", fake_request)
    for mission_id in (".", "..", ".hidden", "mission..bad", "../secrets", "/v1/tasks", "m%2Ftask"):
        result = _call(registry, "platform_bridge_runtime_mission_status",
                       {"mission_id": mission_id})
        assert result.startswith("ERROR:"), mission_id
    assert calls == []


def test_mission_and_swarm_reject_non_ascii_idempotency_headers(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", '{"runtime":"http://runtime:8700"}')
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_TOKEN", "avr_test_tenant")
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON", "[]")

    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid idempotency key must not reach the network")

    monkeypatch.setattr(module, "_request", forbidden)
    for key in ("tâche", "fine\u200bprint", "mission\nheader", "key\tname"):
        response = _call(registry, "platform_bridge_create_mission",
                         {"name": "m", "objective": "o", "idempotency_key": key})
        assert response.startswith("ERROR:"), key
        response = _call(registry, "platform_bridge_create_runtime_swarm",
                         {"mission_name": "m", "objective": "o", "swarm_name": "s",
                          "idempotency_key": key})
        assert response.startswith("ERROR:"), key


def test_bad_operator_service_urls_are_discarded(monkeypatch):
    _registry, module = _load_bridge()
    urls = [
        "http://service:bogus", "http://service:999999", "http://service:0",
        "http://[broken", "ftp://service", "http://name:pass@service:443",
        "http://service/private", "http://service/?token=x",
    ]
    for url in urls:
        monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON",
                           json.dumps({"grokbot": url}))
        assert "grokbot" not in module._targets(), url


def test_swarm_rejects_malformed_remote_mission_id_before_task_creation(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", '{"runtime":"http://runtime:8700"}')
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_TOKEN", "avr_test_tenant")
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON", "[]")
    calls = []

    async def fake_request(target, method, path, body=None, idempotency_key=None):
        calls.append(path)
        assert path == "/v1/missions"
        return json.dumps({"ok": True, "data": {"mission_id": {"mission_id": "../escape"}}})

    monkeypatch.setattr(module, "_request", fake_request)
    result = json.loads(_call(registry, "platform_bridge_create_runtime_swarm", {
        "mission_name": "m", "objective": "o", "swarm_name": "s",
        "idempotency_key": "swarm-edge-1",
    }))
    assert result["ok"] is False and result["phase"] == "mission"
    assert calls == ["/v1/missions"]


def test_swarm_rejects_malformed_remote_task_id_before_swarm_creation(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", '{"runtime":"http://runtime:8700"}')
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_TOKEN", "avr_test_tenant")
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    monkeypatch.setenv("AVRONE_BRIDGE_RUNTIME_CAPABILITIES_JSON", "[]")
    calls = []

    async def fake_request(target, method, path, body=None, idempotency_key=None):
        calls.append(path)
        if path == "/v1/missions":
            data = {"mission_id": {"mission_id": "mission_abc"}}
        elif path == "/v1/tasks":
            data = {"task_id": "task/../../bad"}
        else:
            raise AssertionError("must not create swarm with malformed task ID")
        return json.dumps({"ok": True, "data": data})

    monkeypatch.setattr(module, "_request", fake_request)
    result = json.loads(_call(registry, "platform_bridge_create_runtime_swarm", {
        "mission_name": "m", "objective": "o", "swarm_name": "s",
        "idempotency_key": "swarm-edge-2",
    }))
    assert result["ok"] is False and result["phase"] == "task"
    assert result["partial"] is True
    assert calls == ["/v1/missions", "/v1/tasks"]
