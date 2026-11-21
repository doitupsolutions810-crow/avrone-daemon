"""Round 86: access and fleet words read the node's way. Quoted '"admin"' in AVRONE_PUBLIC_ACCESS fell
back to loopback (local callers got in without the token); '"1"' in AVRONE_FLEET_REQUIRE_ALLOW read as
off; '"3"' or "3x" in AVRONE_FLEET_MAX_HOPS raised inside FleetManager(); a padded search provider sent
queries to duckduckgo; fusion readers took fullwidth digits and ignored quotes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from avrone import agentcontainer, config as c, doctor, fleet, fusion_deliver as fd, server
from avrone import tools_extra as tx


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    c._NUM_WARNED.clear()
    for k in ("AVRONE_PUBLIC_ACCESS", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_PRIVILEGED",
              "AVRONE_FLEET_PRIVILEGED_MAX_HOPS", "AVRONE_FLEET_TOKEN", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL",
              "AVRONE_FLEET_ALLOW", "AVRONE_PRIVILEGED_NODES", "AVRONE_NODE_ID", "AVRONE_SEARCH_PROVIDER",
              "BRAVE_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# ------------------------------------------------------------------ AVRONE_PUBLIC_ACCESS
@pytest.mark.parametrize("raw,want", [
    (None, "loopback"), ("", "loopback"), ("  ", "loopback"), ("admin", "admin"), ('"admin"', "admin"),
    ("'admin'", "admin"), (" ADMIN \n", "admin"), ('"all"', "all"), ("loopback", "loopback"),
    ("everyone", "loopback"), ("adm\u0131n", "loopback"), ("'admin\"", "loopback"), ("al l", "loopback"),
])
def test_access_policy_words(raw, want, monkeypatch):
    if raw is not None:
        monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", raw)
    assert c.access_policy() == server.access_policy() == want


def test_quoted_admin_refuses_tokenless_loopback(monkeypatch):
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", '"admin"')
    assert server.caller_allowed("127.0.0.1", None) is False
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "loopback")
    assert server.caller_allowed("127.0.0.1", None) is True


def test_access_policy_explicit_env_mapping():
    assert c.access_policy({"AVRONE_PUBLIC_ACCESS": "'all'"}) == "all"
    assert c.access_policy({}) == "loopback"
    assert c.access_policy({"AVRONE_PUBLIC_ACCESS": 5}) == "loopback"


def _doctor(env):
    return doctor.run_doctor(env=env, which=lambda n: None, sockets=())


def test_doctor_warns_for_quoted_all_on_exposed_host():
    env = {"AVRONE_HOST": "0.0.0.0", "AVRONE_PUBLIC_ACCESS": '"all"'}
    rep = _doctor(env)
    names = [ch["name"] if isinstance(ch, dict) else ch.name for ch in rep["checks"]]
    assert any(n in ("security.public_access", "security.exposed_shell") for n in names)


# ------------------------------------------------------------------ fleet words and numbers
def _fm(tmp_path):
    return fleet.FleetManager(None, tmp_path)


@pytest.mark.parametrize("raw,want", [("", 3), ("3x", 3), ('"5"', 5), ("'5'", 5), (" 5 ", 5), ("-5", 0),
                                      ("999", 32), ("\uff15", 3), ("nan", 3), ("1e1", 10), ("2.9", 2),
                                      ("9" * 30, 32)])
def test_max_hops_never_crashes(raw, want, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_MAX_HOPS", raw)
    assert fleet.max_hops_env() == want
    assert _fm(tmp_path).max_hops == want


@pytest.mark.parametrize("raw,want", [("1", True), ('"1"', True), ("'yes'", True), (" ON ", True), ("0", False),
                                      ("", False), ('"0"', False), ("nope", False)])
def test_require_allow_quoted(raw, want, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_REQUIRE_ALLOW", raw)
    assert _fm(tmp_path).require_allow is want


@pytest.mark.parametrize("raw,want", [("allow", True), ('"allow"', True), ("allow ", True), ("deny", False),
                                      ("allowed", False), ("", False)])
def test_privileged_word(raw, want, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_PRIVILEGED", raw)
    assert _fm(tmp_path).privileged is want
    t = _doctor({"AVRONE_FLEET_PRIVILEGED": raw})["toggles"]
    assert t["fleet_privileged"] == ("allow" if want else "deny")


def test_quoted_lists_and_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", '"node-a,node-b"')
    monkeypatch.setenv("AVRONE_PUBLIC_URL", '"http://10.0.0.5:8710/"')
    fm = _fm(tmp_path)
    assert fm.allow == {"node-a", "node-b"}
    assert fm.public_url == "http://10.0.0.5:8710"


@pytest.mark.parametrize("raw,want", [("", 1), ('"2"', 2), ("x", 1), ("0", 1), ("99", 3)])
def test_privileged_max_hops(raw, want, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_PRIVILEGED_MAX_HOPS", raw)
    assert _fm(tmp_path).privileged_max_hops == want


@pytest.mark.parametrize("raw,want", [("", 60.0), ("inf", 60.0), ('"30"', 30.0), ("1", 10.0), ("nan", 60.0),
                                      ("\uff13\uff10", 60.0)])
def test_migrate_interval(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE_INTERVAL", raw)
    assert fleet.migrate_interval() == want


@pytest.mark.parametrize("raw,want", [("", 300.0), ("nan", 300.0), ('"60"', 60.0), ("1", 5.0), ("9999", 900.0)])
def test_offload_timeout(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD_TIMEOUT", raw)
    assert fleet.offload_timeout() == want


@pytest.mark.parametrize("raw,want", [("", 15.0), ('"30"', 30.0), ("\uff13", 15.0), ("0", 15.0), ("1e9", 3600.0)])
def test_heartbeat(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_HEARTBEAT", raw)
    assert fleet.heartbeat_env() == want


@pytest.mark.parametrize("raw,want", [('"spill"', "spill"), ("SPREAD ", "spread"), ("other", "off"), ("", "off")])
def test_offload_mode_and_doctor_agree(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", raw)
    assert fleet.offload_mode() == want
    assert _doctor({"AVRONE_FLEET_OFFLOAD": raw})["toggles"]["fleet_offload"] == want


@pytest.mark.parametrize("raw,want", [('"network"', "network"), ("'hybrid'", "hybrid"), ("x", "local"), ("", "local")])
def test_squad_dispatch_and_doctor_agree(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", raw)
    assert fleet.squad_dispatch() == want
    assert _doctor({"AVRONE_SQUAD_DISPATCH": raw})["toggles"]["squad_dispatch"] == want


@pytest.mark.parametrize("raw,want", [('"on"', True), ("1 ", True), ("off", False), ('"0"', False)])
def test_migrate_enabled_and_doctor_agree(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", raw)
    assert fleet.migrate_enabled() is want
    assert _doctor({"AVRONE_AGENT_MIGRATE": raw})["toggles"]["agent_migrate"] == ("on" if want else "off")


def test_doctor_agent_container_quoted():
    assert _doctor({"AVRONE_AGENT_CONTAINER": '"persistent"'})["toggles"]["agent_container"] == "persistent"
    assert _doctor({"AVRONE_AGENT_CONTAINER": "nope"})["toggles"]["agent_container"] == "off"


@pytest.mark.parametrize("raw,want", [("", 2), ('"3"', 3), ("\uff13", 2), ("1e9", 4), ("0", 1)])
def test_net_subsquad(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_NET_SUBSQUAD", raw)
    assert fleet.net_subsquad() == want


# ------------------------------------------------------------------ fusion
@pytest.mark.parametrize("raw,want", [("", 5.0), ('"2"', 2.0), ("\uff12", 5.0), ("nan", 5.0), ("inf", 5.0),
                                      ("0", 0.05), ("999", 30.0)])
def test_server_fusion_timeout(raw, want, monkeypatch):
    monkeypatch.setenv("FUSION_TIMEOUT", raw)
    assert server._fusion_timeout() == want


@pytest.mark.parametrize("raw,want", [("", 10.0), ('"20"', 20.0), ("\uff12", 10.0), ("x", 10.0), ("0", None),
                                      ("nan", None), ("inf", None), ("999", 120.0)])
def test_server_fusion_deadline(raw, want, monkeypatch):
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", raw)
    assert server._fusion_deadline() == want


@pytest.mark.parametrize("t,cd,want", [("", "", (1, 30.0)), ('"3"', "'10'", (3, 10.0)), ("\uff13", "\uff11", (1, 30.0)),
                                       ("-1", "-1", (0, 30.0)), ("1e9", "1e9", (100, 600.0)), ("inf", "nan", (1, 30.0))])
def test_server_fusion_breaker(t, cd, want, monkeypatch):
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", t)
    monkeypatch.setenv("FUSION_BREAKER_COOLDOWN", cd)
    assert server._fusion_breaker() == want


@pytest.mark.parametrize("raw,want", [("", 5.0), ('"7"', 7.0), ("\uff17", 5.0), ("nan", 5.0), ("1_0", 10.0)])
def test_deliver_float_env(raw, want, monkeypatch):
    monkeypatch.setenv("FUSION_TIMEOUT", raw)
    assert fd._float_env("FUSION_TIMEOUT", 5.0) == want


@pytest.mark.parametrize("raw,want", [("", 1), ('"4"', 4), ("4.0", 4), ("\uff14", 1), ("x", 1), ("9" * 40, 1)])
def test_deliver_int_env(raw, want, monkeypatch):
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", raw)
    assert fd._int_env("FUSION_BREAKER_THRESHOLD", 1) == want


def test_deliver_config_from_quoted_env(monkeypatch):
    monkeypatch.setenv("FUSION_TIMEOUT", '"2"')
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "'3'")
    cfg = fd.config_from_env()
    assert cfg.timeout == 2.0 and cfg.breaker_threshold == 3


def test_deliver_token_keeps_spaces(monkeypatch):
    monkeypatch.setenv("CK_FUSION_TOKEN", " tok ")
    assert fd.config_from_env().token == " tok "  # _raw_env is exact by design


# ------------------------------------------------------------------ agentcontainer numbers
def test_agentcontainer_num(monkeypatch):
    monkeypatch.setenv("AVRONE_X_NUM", '"2"')
    assert agentcontainer._num("AVRONE_X_NUM", 1.0, 0.5, 8.0) == 2.0
    for junk in ("\uff12", "nan", "inf", "-1", "0", "x"):
        monkeypatch.setenv("AVRONE_X_NUM", junk)
        assert agentcontainer._num("AVRONE_X_NUM", 1.0, 0.5, 8.0) == 1.0
    monkeypatch.setenv("AVRONE_X_NUM", "99")
    assert agentcontainer._num("AVRONE_X_NUM", 1.0, 0.5, 8.0) == 8.0


# ------------------------------------------------------------------ web search provider
def _ctx():
    s = SimpleNamespace(allow_network=True, search_provider="", brave_api_key="", tavily_api_key="")
    return SimpleNamespace(core=SimpleNamespace(settings=s), run_id="r", name="W", depth=0)


@pytest.mark.parametrize("provider", ['"brave"', "brave ", " BRAVE\n", "'brave'"])
def test_padded_provider_uses_chosen_engine(provider, monkeypatch):
    seen = {}

    async def fake_brave(q, n, key):
        seen["key"] = key
        return [{"title": "t", "url": "https://e.example/", "snippet": "s"}]

    async def fake_ddg(q, n):
        raise AssertionError("query leaked to duckduckgo")

    monkeypatch.setattr(tx, "_search_brave", fake_brave)
    monkeypatch.setattr(tx, "_search_ddg", fake_ddg)
    monkeypatch.setenv("AVRONE_SEARCH_PROVIDER", provider)
    monkeypatch.setenv("BRAVE_API_KEY", ' "bk-123" ')
    out = asyncio.run(tx.t_web_search(_ctx(), {"query": "q"}))
    assert "(brave)" in out and seen["key"] == "bk-123"


def test_padded_provider_without_key_errors(monkeypatch):
    monkeypatch.setenv("AVRONE_SEARCH_PROVIDER", '"tavily"')
    monkeypatch.setenv("TAVILY_API_KEY", "  ")
    assert "TAVILY_API_KEY is not set" in asyncio.run(tx.t_web_search(_ctx(), {"query": "q"}))


def test_unknown_provider_still_duckduckgo(monkeypatch):
    async def fake_ddg(q, n):
        return []
    monkeypatch.setattr(tx, "_search_ddg", fake_ddg)
    monkeypatch.setenv("AVRONE_SEARCH_PROVIDER", "bing")
    assert asyncio.run(tx.t_web_search(_ctx(), {"query": "q"})) == "No results for 'q' (duckduckgo)."
