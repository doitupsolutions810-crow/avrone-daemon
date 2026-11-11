"""Round 156: more vendor tokens, signed-URL/OAuth query values, webhook URLs and Discord tokens.

Each sample below reached CodeX logs, bridges and peers unmasked on v0.148 and secret_scan
reported it CLEAN. The new shapes are anchored (a fixed prefix plus a long body, or a fixed URL
form), so the ordinary text at the end must come through untouched.
"""
from __future__ import annotations

import pytest

from avrone.ctxspawn import redact
from avrone import secretshapes

# Built at run time from split parts, so GitHub push protection (and other scanners) never see a
# whole token-shaped literal in this file. None of these are real credentials.
def _t(*parts: str) -> str:
    return "".join(parts)


_A = "abcdefghijklmnopqrstuvwxyz"
_H = "0123456789abcdef"
TOKENS = [
    _t("dckr", "_pat_", _A, "1"),
    _t("shp", "at_", _H, _H),
    _t("sq0", "atp-", _A[:22]),
    _t("gl", "sa_", _A, "012345_", "abcdef12"),
    _t("lin", "_api_", _A, "0123456789ABCD"),
    _t("pypi", "-AgEIcHlwaS5vcmc", "CJGFiY2RlZmdoLWlqa2wtbW5vcC1xcnN0LXV2d3h5ejAxMjM0NTY3ODkw"),
    _t("dop", "_v1_", _H * 4),
    _t("tskey", "-auth-", "kAbCdEf1CNTRL-", _A, "012345"),
    _t("vercel", "_blob_rw_", _A[:16], "_", _A, "0123"),
    _t("EAA", "CEdEose0cBA", _A, "0123456789"),
    _t("key", "-", _H, _H),
    _t("rzp", "_live_", _A[:14]),
    _t("fig", "d_", _A, "0123456789ABCD"),
    _t("ntn", "_", "0123456789", _A, "ABCDEFGH"),
    _t("secret", "_", _A, "ABCDEFGHIJKLMNOPQ"),
    _t("FLW", "SECK-", _H, _H, "-X"),
    _t("AGE-SECRET", "-KEY-1", "QYQSZQGPQY" * 5, "QYQSZQGP"),
    _t("hv", "s.", "CAESIA", _A, "0123456789"),
    _t("pat", "-na1-", "01234567-89ab-cdef-0123-456789abcdef"),
    _t("ATA", "TT3", "xFfGF0", _A, "0123456789"),
    _t("sb", "p_", _H, _H, "01234567"),
    _t("S", "K", _H, _H),
    _t("MTA4NzY1NDMyMTA5ODc2NTQzMg", ".", "GaBcDe", ".", _A, "0123456789AB"),
]


@pytest.mark.parametrize("tok", TOKENS)
def test_token_masked(tok):
    out = redact(f"config: {tok} end")
    assert tok not in out and out.endswith(" end") and out.startswith("config: ")


@pytest.mark.parametrize("url,secret,kept", [
    ("https://x.blob.core.windows.net/c/f?sv=2022&sig=AbCdEf%2B123&se=2026", "AbCdEf%2B123", "&se=2026"),
    ("https://h/cb?code=4/0AbcDEFghiJK&state=x", "4/0AbcDEFghiJK", "&state=x"),
    ("https://s/o?X-Goog-Signature=abcdef0123456789&x=1", "abcdef0123456789", "&x=1"),
    ("https://hooks.slack.com/services/" + "T00000000/" + "B00000000/" + "X" * 24, "XXXXXXXXXXXX", "hooks.slack.com/services/"),
    ("https://discord.com/api/webhooks/123456789012345678/" + _A + "ABCD", "abcdefghijklmnopqrst", "/123456789012345678/"),
])
def test_url_secrets_masked(url, secret, kept):
    out = redact(url)
    assert secret not in out and kept in out


@pytest.mark.parametrize("text", [
    "SKU 12345", "shp_file.py", "key-value store", "secret_sauce_v2", "Node.js.README", "version 1.2.3.4",
    "pattern pat-1", "secret_key_base", "https://docs.python.org/3/library/re.html?highlight=x#y",
    "Microsoft.Extensions.DependencyInjection.Abstractions.ServiceCollection", "EAAB",
])
def test_ordinary_text_untouched(text):
    assert redact(text) == text


def test_secret_scan_names_new_rules():
    from avrone import tools_secrets
    names = {n for *_x, n in secretshapes.CONTEXT_RULES}
    for want in ("url-query-secret", "slack-webhook", "discord-webhook", "discord-token"):
        assert want in names, want
    assert "vendor-token" in tools_secrets._kinds("x " + TOKENS[0])
    assert "discord-token" in tools_secrets._kinds("bot " + TOKENS[-1])


def test_research_note_shape_shared():
    assert secretshapes.TOKEN_PREFIX_RE.search(TOKENS[1])
