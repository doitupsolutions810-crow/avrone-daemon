"""Round 152: vendor-prefixed tokens are redacted everywhere (avrone/secretshapes.py).

On v0.144, ctxspawn.redact runs on every CodeX event, and it knew none of these tokens: Stripe,
GitLab, Hugging Face, npm, Groq, Replicate, Perplexity, Slack app, Google OAuth, SendGrid, AWS
temporary keys (ASIA), Azure AccountKey and Telegram bot tokens. secret_scan reported runs that
held them as CLEAN. research_note and env_report each had their own, different lists.
"""
from __future__ import annotations

import time

import pytest

from avrone.ctxspawn import redact
from avrone.tools_secrets import _kinds

TOKENS = {
    "stripe": "sk_live_" + "a1B2" * 6, "stripe-restricted": "rk_test_" + "a1B2" * 6,
    "gitlab": "glpat-" + "x" * 20, "huggingface": "hf_" + "a" * 34, "npm": "npm_" + "a" * 36,
    "google-oauth": "ya29." + "a0AfH6" * 10, "sendgrid": "SG." + "a" * 22 + "." + "b" * 43,
    "slack-app": "xapp-1-A0-" + "1" * 10 + "-" + "a" * 30, "telegram": "123456789:AA" + "x" * 33,
    "groq": "gsk_" + "a" * 52, "aws-temp": "ASIA" + "ABCDEFGHIJKLMNOP", "replicate": "r8_" + "a" * 37,
    "perplexity": "pplx-" + "a" * 48, "azure": "AccountKey=" + "a" * 86 + "==",
}


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_event_redactor_and_scan(name):
    tok = TOKENS[name]
    assert tok not in redact(f"out: {tok} done"), name
    assert _kinds(f"out: {tok}"), name


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_research_note_and_env_report(name):
    from avrone.tools_introspect import redact_value
    from avrone.tools_researchnote import redact_text
    tok = TOKENS[name]
    assert tok not in redact_text(f"note {tok} end")
    assert tok not in redact_value("SOME_SETTING", tok)


@pytest.mark.parametrize("text", ["hf_cache_dir", "npm_config_prefix", "SG.items.length", "ASIAN markets",
                                  "pk_live", "at 12:34:56 AAA", "sk_test_short"])
def test_ordinary_text_untouched(text):
    assert redact(text) == text


def test_gate_keeps_redaction_linear():
    t = time.perf_counter()
    redact("SG.a hf_ x:AA ASIA npm_ " * 20000)
    redact("1" * 200_000 + ":AA")
    redact("SG." + "a" * 200_000)
    assert time.perf_counter() - t < 2.0
