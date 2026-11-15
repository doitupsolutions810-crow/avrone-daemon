"""Model client. Talks to any OpenAI-compatible /chat/completions endpoint.

Pick a backend with AVRONE_PROVIDER (openclaw | xai | openai | openrouter |
ollama | custom); AVRONE_LLM_BASE_URL / AVRONE_LLM_MODEL override the preset.
The default is the OpenClaw Gateway (enable
gateway.http.endpoints.chatCompletions.enabled = true in OpenClaw's config).
429 and 5xx responses are retried with exponential backoff.
AVRONE_LLM_MODE=mock runs a scripted model for demos/tests.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from .config import Settings

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

from .gateway import Gateway, is_loopback_url, LLMError  # noqa: E402,F401  (re-exported)
from .gateway import sanitize_messages as _sanitize, safe_temperature as _safe_temp  # noqa: E402


class LLM:
    """Thin wrapper over the multi-provider Gateway (keeps the old call sites working)."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.gateway = Gateway(settings)

    def status(self) -> dict[str, Any]:
        return self.gateway.status()

    async def chat(self, messages: list[dict[str, str]], model: str | None = None,
                   temperature: float = 0.7, session: str | None = None, lead: bool = False) -> str:
        return await self.gateway.chat(messages, model, temperature, session, lead)

    async def _legacy_chat(self, messages: list[dict[str, str]], model: str | None = None,
                           temperature: float = 0.7, session: str | None = None) -> str:
        if self.s.llm_mode == "mock":
            return _mock_reply(messages)
        headers = {"Content-Type": "application/json"}
        if self.s.llm_api_key:  # empty is fine: a proxy may inject auth
            headers["Authorization"] = f"Bearer {self.s.llm_api_key}"
        if session and self.s.provider in {"openclaw", "custom"}:
            # OpenClaw: keep each agent in its own gateway session.
            headers["x-openclaw-session-key"] = session
        if self.s.provider == "openrouter":
            headers["X-Title"] = "AVRONE"
        body: dict[str, Any] = {
            "model": model or self.s.llm_model,
            "messages": _sanitize(messages),
            "temperature": _safe_temp(temperature),
        }
        url = self.s.llm_base_url.rstrip("/") + "/chat/completions"
        tries = max(1, self.s.llm_retries)
        last_err = ""
        r = None
        for attempt in range(tries):
            try:
                async with httpx.AsyncClient(timeout=self.s.llm_timeout,
                                             trust_env=not is_loopback_url(url)) as client:
                    r = await client.post(url, headers=headers, json=body)
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
                raise LLMError(f"Model backend URL is invalid ({url!r}): {e}") from e
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise LLMError(f"Model backend unreachable at {url}: {e}") from e
            except httpx.HTTPError as e:  # read timeouts, dropped connections: retry
                last_err = f"Model backend request failed at {url}: {e}"
                r = None
            else:
                if r.status_code not in RETRY_STATUS:
                    break
                last_err = f"Model backend error {r.status_code}: {r.text[:500]}"
            if attempt < tries - 1:
                await asyncio.sleep(_retry_delay(r, self.s.llm_backoff, attempt))
        else:
            raise LLMError(f"{last_err} (after {tries} tries)")
        if r.status_code >= 400:
            raise LLMError(f"Model backend error {r.status_code}: {r.text[:500]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise LLMError(f"Unexpected model response: {r.text[:500]}") from e


def _retry_delay(r: httpx.Response | None, base: float, attempt: int) -> float:
    from .gateway import _bounded_delay
    delay = _bounded_delay(base, attempt)  # round 79: base 1e300 slept forever
    if r is not None:
        ra = r.headers.get("retry-after", "")
        try:
            f = float(ra)
        except (TypeError, ValueError):
            f = None
        if f is not None and f == f and f > 0:  # round 79: "-5"/"nan" were taken as-is
            delay = max(delay, min(f, 30.0))
    return delay


# ---------------------------------------------------------------- mock model
def _mock_reply(messages: list[dict[str, str]]) -> str:
    system = messages[0]["content"] if messages else ""
    last = messages[-1]["content"] if messages else ""
    if "You are the PLANNER" in system:
        task = re.search(r"TASK:\s*(.*)", last, re.S)
        t = (task.group(1).strip() if task else "the task")[:80]
        return json.dumps({"subtasks": [
            {"role": "Researcher", "focus": f"Gather facts relevant to: {t}"},
            {"role": "Engineer", "focus": f"Work out a concrete solution for: {t}"},
            {"role": "Critic", "focus": f"Find flaws and risks in approaches to: {t}"},
        ]})
    if "You are the DEEP LEAD" in system:
        n = last.count("### Reviewer ")
        return (
            "## Verdict\nExtended (mock). Confidence: Med\n\n"
            "## Corrections\n- None found by the mock Fact-Checker.\n\n"
            "## Additions\n- Mock Gap-Finder added edge cases.\n\n"
            "## Counterpoints\n- Mock Contrarian offered an alternative.\n\n"
            "## Sources\n- https://example.com/mock-source\n\n"
            f"## Bottom line\nDEEP REPORT (mock): {n} reviewers stress-tested the first answer."
        )
    if "You are the LEAD" in system:
        return "FINAL ANSWER (mock): merged the squad's findings into one answer."
    # Worker (squad or deep wave): use one tool on the first step, then answer.
    if "TOOL RESULT" not in last:
        return '```json\n{"tool": "shell", "args": {"cmd": "echo avrone-online && uname -s"}}\n```'
    if "DEEP ANALYSIS wave" in system:
        role = re.search(r"the (.+?) reviewer", system)
        return json.dumps({"final": f"Mock {role.group(1) if role else 'deep'} review: first answer checked."})
    return '```json\n{"final": "Mock worker result: environment checked, findings ready."}\n```'
