from __future__ import annotations

from types import SimpleNamespace

import pytest

from avrone import ai_observability
import httpx

from avrone.gateway import Gateway, LLMError


def settings():
    return SimpleNamespace(
        provider="ollama",
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="qwen2.5:3b",
        llm_api_key="",
        lead_model="",
        breaker_threshold=3,
        breaker_cooldown=30,
        llm_mode="live",
        llm_retries=1,
        llm_backoff=0,
        llm_timeout=2,
    )


@pytest.mark.asyncio
async def test_gateway_emits_generation_for_real_provider_success(monkeypatch):
    events = []

    async def request(upstream, messages, model, temperature, session):
        gateway_module._AI_USAGE.set({"input_tokens": 17, "output_tokens": 9})
        return "model answer"

    monkeypatch.setattr(ai_observability, "capture_generation", lambda **kw: events.append(kw))
    gateway = Gateway(settings())
    monkeypatch.setattr(gateway, "_request", request)
    result = await gateway.chat(
        [{"role": "user", "content": "task"}], session="avrone:r-1:Builder"
    )
    assert result == "model answer"
    assert len(events) == 1
    assert events[0]["provider"] == "ollama"
    assert events[0]["model"] == "qwen2.5:3b"
    assert events[0]["session"] == "avrone:r-1:Builder"
    assert events[0]["input_tokens"] == 17
    assert events[0]["output_tokens"] == 9
    assert events[0]["error"] is False


@pytest.mark.asyncio
async def test_gateway_records_failed_generation_but_preserves_original_error(monkeypatch):
    events = []

    async def request(upstream, messages, model, temperature, session):
        raise httpx.ConnectError("upstream offline")

    monkeypatch.setattr(ai_observability, "capture_generation", lambda **kw: events.append(kw))
    gateway = Gateway(settings())
    monkeypatch.setattr(gateway, "_request", request)
    with pytest.raises(LLMError, match="All upstreams failed"):
        await gateway.chat([{"role": "user", "content": "task"}], session="avrone:r-2:Builder")
    assert len(events) == 1
    assert events[0]["error"] is True
