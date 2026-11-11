"""Round 31: fleet request signing and peer auth. Covers token rotation (AVRONE_FLEET_TOKEN_PREV),
tokens wrapped in .env quotes, a signed path behind a reverse-proxy prefix or a peer url that has
a path, upper-case hex signatures, malformed signatures, clock-skew diagnostics, the weak-token
warning, and that none of this loosens replay, tamper or wrong-token refusals."""
import asyncio
import json
import logging
import time
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, GatewayRefused, sign, canonical_path, clean_token, WINDOW_S


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_TOKEN_PREV", "AVRONE_FLEET_ALLOW", "AVRONE_NODE_ID",
              "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


def core():
    c = MagicMock()
    c.active = {}
    c.codex.emit = MagicMock()
    return c


def make(tmp_path, monkeypatch, token="a" * 32, prev=None, nid="root", sub="n"):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", token)
    monkeypatch.setenv("AVRONE_NODE_ID", nid)
    if prev is None:
        monkeypatch.delenv("AVRONE_FLEET_TOKEN_PREV", raising=False)
    else:
        monkeypatch.setenv("AVRONE_FLEET_TOKEN_PREV", prev)
    d = tmp_path / sub
    d.mkdir(exist_ok=True)
    return FleetManager(core(), d)


BODY = b'{"node_id":"kid"}'
PATH = "/api/fleet/register"


def hdrs(token, path=PATH, body=BODY, ts=None, nonce=None):
    return sign(token, "POST", path, body, ts, nonce)


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize("raw,exp", [
    ("abc", "abc"), (' "abc" ', "abc"), ("'abc'", "abc"), ('"abc', '"abc'), ("'abc\"", "'abc\""),
    ('" abc "', "abc"), ("", ""), (None, ""), ('""', ""), ("a'b'c", "a'b'c")])
def test_clean_token(raw, exp):
    assert clean_token(raw) == exp


@pytest.mark.parametrize("p,exp", [
    ("/api/fleet/run", "/api/fleet/run"), ("/avrone/api/fleet/run", "/api/fleet/run"),
    ("/a/b/api/fleet/memory", "/api/fleet/memory"), ("/other", "/other"), ("", ""), (None, "")])
def test_canonical_path(p, exp):
    assert canonical_path(p) == exp


# ------------------------------------------------------------------ quoting
def test_quoted_token_matches_unquoted_peer(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token='"' + "s" * 32 + '"')
    assert fm.token == "s" * 32
    fm.verify("POST", PATH, BODY, hdrs("s" * 32))


def test_quoted_prev_token(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="'" + "o" * 32 + "'")
    fm.verify("POST", PATH, BODY, hdrs("o" * 32))


# ------------------------------------------------------------------ rotation
def test_prev_token_accepted_and_audited(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="o" * 32)
    fm.verify("POST", PATH, BODY, hdrs("o" * 32))
    assert "fleet.token.prev" in [c.args[1] for c in fm.core.codex.emit.call_args_list]
    assert fm.info()["token_rotating"] is True


def test_new_token_accepted_without_prev_audit(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="o" * 32)
    fm.verify("POST", PATH, BODY, hdrs("n" * 32))
    assert "fleet.token.prev" not in [c.args[1] for c in fm.core.codex.emit.call_args_list]


def test_other_token_still_refused_during_rotation(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="o" * 32)
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", PATH, BODY, hdrs("x" * 32))
    assert e.value.code == "bad_signature"


def test_prev_token_refused_once_unset(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32)
    with pytest.raises(GatewayRefused):
        fm.verify("POST", PATH, BODY, hdrs("o" * 32))
    assert fm.info()["token_rotating"] is False


def test_prev_equal_to_current_is_ignored(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="n" * 32)
    assert fm.prev_token == "" and fm.info()["token_rotating"] is False


def test_outbound_always_signs_with_new_token(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="o" * 32)
    seen = {}

    def h(req):
        seen["h"] = dict(req.headers)
        seen["body"] = req.content
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(h))
    asyncio.run(fm._post("http://p.test", PATH, {"x": 1}))
    h = seen["h"]
    assert h["x-avrone-sig"] == sign("n" * 32, "POST", PATH, seen["body"], h["x-avrone-ts"], h["x-avrone-nonce"])["x-avrone-sig"]


def test_prev_token_replay_still_refused(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="o" * 32)
    h = hdrs("o" * 32)
    fm.verify("POST", PATH, BODY, h)
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", PATH, BODY, h)
    assert e.value.code == "replay"


def test_prev_token_tampered_body_refused(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token="n" * 32, prev="o" * 32)
    with pytest.raises(GatewayRefused):
        fm.verify("POST", PATH, BODY + b" ", hdrs("o" * 32))


# ------------------------------------------------------------------ path prefix
def test_receiver_behind_prefix_accepts_bare_signature(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    fm.verify("POST", "/avrone" + PATH, BODY, hdrs("a" * 32, PATH))


def test_sender_with_prefixed_url_accepted_by_bare_receiver(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    fm.verify("POST", PATH, BODY, hdrs("a" * 32, "/avrone" + PATH))


def test_prefix_does_not_let_endpoint_be_swapped(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", "/avrone/api/fleet/run", BODY, hdrs("a" * 32, PATH))
    assert e.value.code == "bad_signature"


def test_method_still_bound(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    with pytest.raises(GatewayRefused):
        fm.verify("PUT", PATH, BODY, hdrs("a" * 32))


def test_end_to_end_peer_url_with_path(tmp_path, monkeypatch):
    sender = make(tmp_path, monkeypatch, nid="kid", sub="s")
    receiver = make(tmp_path, monkeypatch, nid="root", sub="r")

    def h(req):
        try:
            receiver.verify(req.method, req.url.path, req.content, {k.lower(): v for k, v in req.headers.items()})
        except GatewayRefused as e:
            return httpx.Response(401, json={"detail": str(e)})
        return httpx.Response(200, json={"ok": True, "path": req.url.path})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(h))
    res = asyncio.run(sender._post("http://proxy.test/avrone", PATH, {"node_id": "kid"}))
    assert res == {"ok": True, "path": "/avrone" + PATH}


# ------------------------------------------------------------------ signature format
def test_uppercase_hex_signature_accepted(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    h = hdrs("a" * 32)
    h["x-avrone-sig"] = h["x-avrone-sig"].upper()
    fm.verify("POST", PATH, BODY, h)


@pytest.mark.parametrize("bad", ["", "zz", "a" * 63, "a" * 65, "é" * 64, None, 123, " " + "a" * 63])
def test_malformed_signature_refused(tmp_path, monkeypatch, bad):
    fm = make(tmp_path, monkeypatch)
    h = hdrs("a" * 32)
    h["x-avrone-sig"] = bad
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", PATH, BODY, h)
    assert e.value.code == "bad_signature"


# ------------------------------------------------------------------ clock skew
def test_stale_message_says_sender_behind(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    ts = str(int(time.time()) - 600)
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", PATH, BODY, hdrs("a" * 32, ts=ts))
    assert e.value.code == "stale"
    msg = str(e.value)
    assert "behind" in msg and "NTP" in msg and f"±{WINDOW_S}s" in msg
    assert any(f"{n}s" in msg for n in range(598, 603))


def test_stale_message_says_sender_ahead(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", PATH, BODY, hdrs("a" * 32, ts=str(int(time.time()) + 3600)))
    assert "ahead of" in str(e.value)


def test_skew_not_revealed_without_valid_signature(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch)
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", PATH, BODY, hdrs("wrong" * 8, ts=str(int(time.time()) - 600)))
    assert e.value.code == "bad_signature" and "clock" not in str(e.value)


# ------------------------------------------------------------------ weak token
def test_weak_token_flagged_and_logged(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=fleet_mod.log.name):
        fm = make(tmp_path, monkeypatch, token="short")
    assert fm.info()["token_weak"] is True
    assert any("AVRONE_FLEET_TOKEN is 5 chars" in r.getMessage() for r in caplog.records)
    fm.verify("POST", PATH, BODY, hdrs("short"))  # warned, not refused


def test_strong_token_not_flagged(tmp_path, monkeypatch):
    assert make(tmp_path, monkeypatch, token="k" * 32).info()["token_weak"] is False


def test_disabled_node_not_flagged(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    fm = FleetManager(core(), tmp_path)
    assert fm.info()["token_weak"] is False


def test_quotes_only_token_disables_fleet(tmp_path, monkeypatch):
    fm = make(tmp_path, monkeypatch, token='""')
    assert fm.enabled is False
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", PATH, BODY, hdrs(""))
    assert e.value.code == "no_token"


# ------------------------------------------------------------------ via the server
def test_server_register_behind_prefix_and_rotation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", '"' + "n" * 32 + '"')
    monkeypatch.setenv("AVRONE_FLEET_TOKEN_PREV", "o" * 32)
    with TestClient(server.app) as c:
        body = json.dumps({"node_id": "kid", "url": "http://kid.test"}).encode()
        r = c.post(PATH, content=body, headers={"content-type": "application/json", **sign("o" * 32, "POST", PATH, body)})
        assert r.status_code == 200, r.text
        r = c.post(PATH, content=body, headers={"content-type": "application/json", **sign("x" * 32, "POST", PATH, body)})
        assert r.status_code == 401


@pytest.mark.parametrize(
    "offset,accepted",
    [
        (-WINDOW_S, True),
        (WINDOW_S, True),
        (-WINDOW_S - 1, False),
        (WINDOW_S + 1, False),
    ],
)
def test_signed_timestamp_window_exact_edges(tmp_path, monkeypatch, offset, accepted):
    fm = make(tmp_path, monkeypatch)
    fixed_now = 1_800_000_000.0
    monkeypatch.setattr(fleet_mod.time, "time", lambda: fixed_now)
    timestamp = str(int(fixed_now) + offset)
    headers = hdrs("a" * 32, ts=timestamp, nonce=f"edge-{offset}")

    if accepted:
        fm.verify("POST", PATH, BODY, headers)
        assert f"edge-{offset}" in fm._nonces
    else:
        with pytest.raises(GatewayRefused) as exc:
            fm.verify("POST", PATH, BODY, headers)
        assert exc.value.code == "stale"
        assert f"edge-{offset}" not in fm._nonces
