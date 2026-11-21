"""Hardening round 65: proc_status masks credentials in child argv, exposure_check rates repeated
placeholders and low-variety tokens, registry_audit counts outbound-network tools as acting, and
boolean args accept "true"."""
from __future__ import annotations

import asyncio
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace as S

import pytest

from avrone import tools_hostview as hv
from avrone import tools_statehealth as sh


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------ argv redaction
@pytest.mark.parametrize("cmd,secret", [
    ("python3 app.py --password=hunter2xyz", "hunter2xyz"),
    ("app --password hunter2xyz", "hunter2xyz"),
    ("app --db-password=s3cr3tPass", "s3cr3tPass"),
    ("app --token abc123def456", "abc123def456"),
    ("app --api-key=K9x8y7z6", "K9x8y7z6"),
    ("app --api_key K9x8y7z6", "K9x8y7z6"),
    ("app --client-secret=zzTopSecret", "zzTopSecret"),
    ("app --access-key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("curl -H Authorization: Bearer abcdefSECRET123 https://x", "abcdefSECRET123"),
    ("curl -H x-api-key: 0123456789abcdef0123 https://x", "0123456789abcdef0123"),
    ("psql postgres://admin:pw1234@db/x", "pw1234"),
    ("git clone https://user:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/x", "ghp_abcdef"),
    ("app PASSWORD=hunter2xyz", "hunter2xyz"),
    ("app --PASSWORD=hunter2xyz", "hunter2xyz"),
])
def test_argv_secret_masked(cmd, secret):
    out = hv._redact_cmd(cmd)
    assert secret not in out
    assert out.split(" ", 1)[0] == cmd.split(" ", 1)[0]


@pytest.mark.parametrize("cmd", [
    "git push origin main", "sleep 30", "python3 -m http.server 8080", "x --no-auth-check --port 80",
    "pytest -q tests/test_tokens.py", "make -j4",
])
def test_argv_plain_untouched(cmd):
    assert hv._redact_cmd(cmd) == cmd


def test_flag_followed_by_flag_not_eaten():
    assert hv._redact_cmd("x --token -v") == "x --token -v"


def test_redaction_before_cut():
    cmd = "a" * (hv.CMD_MAX - 12) + " --password=hunter2xyzLONGSECRET"
    out = hv._escape(hv._redact_cmd(cmd)[:hv.CMD_MAX])
    assert "hunter2" not in out


def test_redact_failure_masks_all(monkeypatch):
    monkeypatch.setattr(hv, "_ARGV_SECRET_RE", S(sub=lambda *a: 1 / 0))
    assert hv._redact_cmd("prog --x secretvalue") == "prog ***"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_proc_status_hides_live_child_secret():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--password=hunter2xyz",
                          "--token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"])
    try:
        time.sleep(0.3)
        out = run(hv.t_proc_status(S(), {"all": True}))
        assert str(p.pid) in out
        assert "hunter2xyz" not in out and "ghp_abcdef" not in out
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
@pytest.mark.parametrize("val,shows_all", [("true", True), ("TRUE", True), ("1", True), (True, True),
                                            ("false", False), (None, False), ("nope", False)])
def test_proc_status_all_accepts_strings(val, shows_all):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.2)
        out = run(hv.t_proc_status(S(), {"all": val}))
        # a fresh child is neither long-running nor a zombie: shown either way (fallback to rows)
        assert str(p.pid) in out
    finally:
        p.kill()
        p.wait()


# ------------------------------------------------------------ token strength
@pytest.mark.parametrize("tok", [
    "secret-secret-secret", "secretsecretsecret", "passwordpassword", "changeme-changeme-1",
    "adminadminadmin123", "tokentokentoken12345", "test_test_test_test", "Password-Password",
    "default.default.default", "example/example/example", "letmeinletmein2026", "1234567890123456", "9999999999999999999",
])
def test_repeated_placeholders(tok):
    assert hv._token_strength(tok) == "placeholder"


@pytest.mark.parametrize("tok", [
    "aaaaaaaaaaaaaaaaaaaa", "abababababababababab", "x" * 64, "qwertyuiopasdfgh",
    "abcdefghijklmnopq", "zyxwvutsrqponmlk",
])
def test_low_variety_weak(tok):
    assert hv._token_strength(tok) == "weak"


@pytest.mark.parametrize("tok", ["Xk9#mP2vL8qR4wZ7nB", "admin-Xk9mP2vL8qR4wZ7n", "secret_9fK2mQ7xL4pR8vT1"])
def test_strong_still_ok(tok):
    assert hv._token_strength(tok) == "ok"


def test_random_tokens_never_flagged():
    for _ in range(2000):
        assert hv._token_strength(secrets.token_urlsafe(16)) == "ok"
        assert hv._token_strength(secrets.token_hex(16)) == "ok"


def test_exposure_flags_repeated_placeholder(monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "admin_token", "secret-secret-secret")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    e = hv.exposure()
    assert e["admin_token"] == "placeholder" and "admin token is placeholder" in e["issues"]
    out = run(hv.t_exposure_check(None, {}))
    assert "secret-secret-secret" not in out


# ------------------------------------------------------------ registry_audit / state_health
NETWORK = ["fetch", "web_search", "dns_lookup", "http_fingerprint", "ip_geo", "osint_domain", "rdap_whois",
           "tcp_ping", "tls_inspect", "traceroute"]


@pytest.mark.parametrize("name", NETWORK + ["port_scan", "http_request", "shell"])
def test_network_tools_acting(name):
    assert sh.is_acting(name)


def test_acting_by_description():
    assert sh.is_acting("brand_new_tool", S(description="acting: deletes things"))
    assert sh.is_acting("brand_new_tool", S(description="  Acting - posts to a peer"))
    assert not sh.is_acting("brand_new_tool", S(description="read-only: lists things"))
    assert not sh.is_acting("brand_new_tool", S(description=None))
    assert not sh.is_acting("brand_new_tool")


def test_registry_audit_counts_described_acting():
    from avrone.tools import Tool

    async def fn(ctx, args):
        return ""
    tools = {"a": Tool("a", "acting: writes", "{}", fn, source="plugin", schema={}),
             "b": Tool("b", "read-only: reads", "{}", fn, source="plugin", schema={}),
             "fetch": Tool("fetch", "http get a url", "{}", fn, source="builtin", schema={})}
    reg = S(names=lambda: list(tools), get=tools.get)
    a = sh.registry_audit(reg)
    assert a["acting"] == 2 and a["read_only"] == 1


def test_full_registry_no_acting_described_tool_counted_read_only():
    from avrone import tools_extra as tx
    from avrone.tools import builtin_registry
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    for n in reg.names():
        t = reg.get(n)
        if (t.description or "").lower().startswith("acting"):
            assert sh.is_acting(n, t)
    for n in NETWORK:
        if reg.get(n) is not None:
            assert sh.is_acting(n, reg.get(n))


@pytest.mark.parametrize("val,verbose", [("true", True), ("yes", True), (True, True), ("false", False),
                                         (None, False), (0, False)])
def test_state_health_verbose_strings(tmp_path, val, verbose):
    ctx = S(core=S(settings=S(data_dir=tmp_path)))
    out = run(sh.t_state_health(ctx, {"verbose": val}))
    # an empty data_dir lists every area only when verbose
    assert ("- checkpoints: 0 file(s)" in out) is verbose
