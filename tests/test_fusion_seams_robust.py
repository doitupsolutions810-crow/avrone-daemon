"""Hardening round 48: fusion_loopback (access-control loopback spelling, breaker buckets) and
fusion_plane (classifying a capability-authority execute body)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from avrone import doctor, gateway, server
from avrone.fusion_loopback import host_text, is_access_loopback, is_any_loopback, origin_key
from avrone.fusion_plane import classify_execute


# ------------------------------------------------------------ access loopback: still allowed
@pytest.mark.parametrize("host", [
    "127.0.0.1", "::1", "localhost", "LOCALHOST", "[::1]", "::1%lo", "::1%eth0", "[::1%lo]",
    "::ffff:127.0.0.1", "[::ffff:127.0.0.1]", "0:0:0:0:0:0:0:1", "::ffff:7f00:1", " 127.0.0.1 ",
    b"127.0.0.1", "\t::1",
])
def test_access_loopback_still_allowed(host):
    assert is_access_loopback(host) is True


# ------------------------------------------------------------ access loopback: refused
@pytest.mark.parametrize("host", [
    "]127.0.0.1[", "[[::1]]", "[127.0.0.1", "127.0.0.1]", "[]", "[",
    "127.0.0.1%evil", "localhost%x", "127.0.0.1%", "::1%", "::1%a%b", "::1%a b", "::1%\x00",
    "127.0.0.1\n", "127.0.0.1\r\n", "\n127.0.0.1", "127.0.0.1\x00", "\x00localhost", "local\x00host",
    "localhost\u200b", "\u200blocalhost", "ｌocalhost", "127.0.0.１", "::1\u2028",
    "127.0.0.2", "127.1", "0127.0.0.1", "::127.0.0.1", "::ffff:127.0.0.2", "10.0.0.1",
    "localhost.", "a.localhost", "localhost:80", "127.0.0.1:80",
    None, "", 2130706433, ["127.0.0.1"], b"\xff127.0.0.1", object(),
])
def test_access_loopback_refused(host):
    assert is_access_loopback(host) is False


def test_lookalike_bytes_and_str_never_raise():
    for h in ("\ud800", "\udc00127.0.0.1", "%" * 1000, "[" * 1000 + "]" * 1000, ":" * 5000):
        assert is_access_loopback(h) is False
        assert is_any_loopback(h) is False


# ------------------------------------------------------------ any loopback
@pytest.mark.parametrize("host", ["localhost", "localhost.", "a.localhost", "a.b.localhost", "127.8.9.1",
                                  "::ffff:127.0.0.2", "[::1]", "LOCALHOST."])
def test_any_loopback_yes(host):
    assert is_any_loopback(host) is True


@pytest.mark.parametrize("host", [".localhost", "a..localhost", "localhost..", "127.0.0.1%x",
                                  "localhost%x", "]localhost[", "localhost\n", "x.localhost\x00",
                                  "evil.com", "localhost.evil.com", "::ffff:10.0.0.1", "", None])
def test_any_loopback_no(host):
    assert is_any_loopback(host) is False


def test_gateway_and_doctor_follow():
    assert gateway.is_loopback_url("http://localhost./v1") is True
    assert gateway.is_loopback_url("http://127.0.0.1%25evil/v1") is False
    assert doctor._is_loopback_host("127.0.0.1%evil") is False
    assert doctor._is_loopback_host("::1") is True


def test_host_text_shapes():
    assert host_text("[::1]") == "::1"
    assert host_text("[[::1]]") == ""
    assert host_text("::1%lo") == "::1"
    assert host_text("Example.COM") == "example.com"
    assert host_text(5) == ""


# ------------------------------------------------------------ the HTTP policy end to end
@pytest.mark.parametrize("client", ["127.0.0.1%evil", "]127.0.0.1[", "127.0.0.1\n", "localhost%x"])
def test_forged_client_spellings_get_403(tmp_path, monkeypatch, client):
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "loopback")
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(server.settings, "admin_token", "")
    with TestClient(server.app, client=(client, 9)) as c:
        assert c.get("/api/runs").status_code == 403


def test_caller_allowed_direct(monkeypatch):
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "loopback")
    monkeypatch.setattr(server.settings, "admin_token", "")
    assert server.caller_allowed("127.0.0.1", None) is True
    assert server.caller_allowed("127.0.0.1%x", None) is False
    assert server.caller_allowed("[[::1]]", None) is False


# ------------------------------------------------------------ origin_key
@pytest.mark.parametrize("base", ["http://127.0.0.1:99999", "http://127.0.0.1:abc", "http://[::1:8090",
                                  "http://h:-1", "http://h:1:2", None, 5, b"http://127.0.0.1:1"])
def test_origin_key_never_raises(base):
    assert origin_key(base) == "invalid:"


def test_origin_key_default_ports():
    assert origin_key("http://localhost") == origin_key("http://localhost:80") == "loopback:80"
    assert origin_key("https://localhost") == "loopback:443"
    assert origin_key("http://localhost") != origin_key("https://localhost")
    assert origin_key("HTTPS://example.com") == origin_key("https://example.com:443")
    assert origin_key("http://example.com") != origin_key("https://example.com")


def test_origin_key_ipv6_unambiguous():
    assert origin_key("http://[fe80::1]:8090") == "[fe80::1]:8090"
    assert origin_key("http://[fe80::1:8090]") != origin_key("http://[fe80::1]:8090")


def test_origin_key_loopback_spellings_share_and_others_split():
    k = origin_key("http://127.0.0.1:8090")
    for b in ("http://[::1]:8090", "http://LOCALHOST:8090", "http://[::ffff:127.0.0.1]:8090",
              "http://[0:0:0:0:0:0:0:1]:8090"):
        assert origin_key(b) == k == "loopback:8090"
    assert origin_key("http://127.0.0.2:8090") == "127.0.0.2:8090"
    assert origin_key("http://127.0.0.1:8091") != k


# ------------------------------------------------------------ classify_execute
GOOD = {"status": "executed", "execution_status": "SUCCEEDED", "result": {"sha256": "abc"}}


def test_succeeded_still_executed():
    assert classify_execute(dict(GOOD)) == {"executed": True, "reason": "plane_succeeded", "runner": "plane"}


@pytest.mark.parametrize("patch", [
    {"status": ["executed"]}, {"execution_status": ["SUCCEEDED"]}, {"status": {"a": 1}},
    {"execution_status": {"x": 1}}, {"status": None}, {"execution_status": "succeeded"},
    {"execution_status": "SUCCEEDED "}, {"status": "Executed"}, {"error": "boom"},
    {"result": "not_executed"},
])
def test_not_executed_variants_never_raise(patch):
    body = dict(GOOD)
    body.update(patch)
    assert classify_execute(body)["executed"] is False


class Liar(str):
    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class Always:
    def __eq__(self, other):
        return True

    __hash__ = object.__hash__


@pytest.mark.parametrize("v", [Liar("nope"), Always()])
def test_objects_cannot_pose_as_succeeded(v):
    assert classify_execute({"status": v, "execution_status": v})["executed"] is False


def test_dict_subclass_get_cannot_lie():
    class D(dict):
        def get(self, k, d=None):
            return {"status": "executed", "execution_status": "SUCCEEDED"}.get(k, d)
    assert classify_execute(D())["executed"] is False


@pytest.mark.parametrize("body", [[], "x", None, 5, ("status", "executed"), object()])
def test_malformed(body):
    assert classify_execute(body)["reason"] == "malformed"


def test_unhashable_status_in_stub_branch():
    assert classify_execute({"status": ["ok"], "result": "executed"})["reason"] == "not_executed"
    assert classify_execute({"status": "ok", "result": ["executed"]})["reason"] == "not_executed"


def test_stub_and_not_implemented_kept():
    assert classify_execute({"status": "ok", "result": "executed"})["reason"] == "unscoped_stub"
    assert classify_execute({"execution_status": "NOT_IMPLEMENTED"})["reason"] == "not_implemented"


@pytest.mark.parametrize("err,expect", [
    ("verify failed", "verify failed"), ("plane_succeeded", "plane_succeeded"),
    ("a\nb\x1b[31mc", "a b [31mc"), ("", "error"), (None, "error"), (False, "error"),
    ({"x": 1}, "dict"), (12, "int"), ("x\u2028y\u200bz", "x y z"), ("\ud800", "error"),
])
def test_error_reason_is_a_code_and_detail_is_clean(err, expect):
    got = classify_execute({"status": "error", "error": err})
    assert got["reason"] == "error" and got["executed"] is False
    assert got["detail"] == expect
    assert "\n" not in got["detail"] and "\x1b" not in got["detail"]


def test_error_detail_capped():
    got = classify_execute({"status": "error", "error": "x" * 100000})
    assert len(got["detail"]) <= 203


def test_hostile_str_error_never_called():
    class Boom:
        def __str__(self):
            raise RuntimeError("no")
    assert classify_execute({"status": "error", "error": Boom()})["detail"] == "Boom"
