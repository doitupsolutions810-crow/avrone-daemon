"""Round 82: one reading of the fleet token / capgate key for fleet, capgate, capgate_head_attest,
doctor and exposure_check (a "  " AVRONE_CAPGATE_KEY hid the fleet token and every node fell back
to a random per-process key; quoted or blank tokens were judged differently per module)."""
from __future__ import annotations

import pytest

from avrone import capgate, capgate_head_attest as cha, config as c, doctor, fleet
from avrone import tools_hostview as hv

TOK = "Zq8vR2mL7xN4pK9wT3yB6cH1"


@pytest.mark.parametrize("raw,want", [
    (None, ""), (5, ""), (b"tok", ""), ("", ""), ("   ", ""), ("''", ""), ('" "', ""),
    (" tok ", "tok"), ("'tok'", "tok"), ('"tok"', "tok"), ("' tok '", "tok"), ("'tok\"", "'tok\""),
    ("tok\n", "tok"), ("\"'tok'\"", "'tok'"),
])
def test_clean_secret(raw, want):
    assert c.clean_secret(raw) == want


def test_fleet_clean_token_matches_config():
    for raw in (" a ", "'a'", '"a"', "", None, "x'"):
        assert fleet.clean_token(raw) == c.clean_secret(raw if isinstance(raw, str) else str(raw or ""))


@pytest.mark.parametrize("key,fleet_tok,want", [
    ("   ", TOK, TOK), ("''", TOK, TOK), (None, f'"{TOK}"', TOK), (None, f" {TOK}\n", TOK),
    (f"'{TOK}x'", TOK, TOK + "x"), (None, None, ""), ("  ", "  ", ""),
])
def test_shared_key_text(key, fleet_tok, want, monkeypatch):
    for name, v in (("AVRONE_CAPGATE_KEY", key), ("AVRONE_FLEET_TOKEN", fleet_tok)):
        if v is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, v)
    assert c.shared_key_text() == want
    got = capgate._key()
    assert got == (want.encode() if want else capgate._PROC_KEY.encode())
    assert cha._residual(True)["orientation"] == ("pass" if want else "unmeasured")


def test_quoted_and_bare_fleet_token_sign_alike(monkeypatch):
    monkeypatch.delenv("AVRONE_CAPGATE_KEY", raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", f'"{TOK}"')
    a = capgate._key()
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOK)
    assert capgate._key() == a


def test_blank_capgate_key_caps_verify_across_restart(monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_KEY", "   ")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOK)
    cap = capgate.issue("agent-a", ["tool:shell"])
    old = capgate._PROC_KEY
    monkeypatch.setattr(capgate, "_PROC_KEY", "f" * 64)  # a "restarted" process
    assert capgate._PROC_KEY != old
    assert capgate.check_sig(cap)[0] is True


@pytest.mark.parametrize("raw,want", [("600", 600), ("'600'", 600), (' "120" ', 120), ("\uff16\uff10\uff10", 3600),
                                      ("", 3600), ("x", 3600), ("5", 30), ("999999", 86400), ("-1", 3600)])
def test_capgate_ttl(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_TTL", raw)
    assert capgate.ttl() == want


@pytest.mark.parametrize("raw,want", [(None, "unset"), ("", "unset"), ("   ", "unset"), ("''", "unset"),
                                      ("'changeme'", "placeholder"), ('"secret"', "placeholder"),
                                      ("short", "weak"), (f"'{TOK}'", "set"), (TOK, "set")])
def test_doctor_token_state(raw, want):
    assert doctor.token_state(raw) == want


def test_doctor_reuse_sees_quoted_same_secret():
    rep = doctor.run_doctor(env={"AVRONE_ADMIN_TOKEN": TOK, "AVRONE_FLEET_TOKEN": f'"{TOK}"'},
                            which=lambda n: None, sockets=())
    names = [ch["name"] if isinstance(ch, dict) else ch.name for ch in rep["checks"]]
    assert "security.token_reuse" in names


def test_doctor_reuse_ignores_two_blanks():
    rep = doctor.run_doctor(env={"AVRONE_ADMIN_TOKEN": "  ", "AVRONE_FLEET_TOKEN": "  "},
                            which=lambda n: None, sockets=())
    names = [ch["name"] if isinstance(ch, dict) else ch.name for ch in rep["checks"]]
    assert "security.token_reuse" not in names


@pytest.mark.parametrize("raw,on", [("   ", False), ("''", False), ("", False), (TOK, True), (f"'{TOK}'", True)])
def test_exposure_fleet_enabled_matches_fleet(raw, on, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", raw)
    a = hv.api_surface()
    assert a["fleet_enabled"] is on
    assert bool(fleet.clean_token(raw)) is on


def test_exposure_quoted_placeholder_fleet_token(monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "'changeme'")
    e = hv.exposure()
    assert e["fleet_token"] == "placeholder"


def test_exposure_quoted_same_secret(monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "admin_token", TOK)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", f'"{TOK}"')
    assert "admin and fleet tokens are the same secret" in hv.exposure()["issues"]


def test_admin_tokens_stay_exact(monkeypatch):
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN_PREVIOUS", " grace ")
    assert c.Settings().admin_token_previous == " grace "
