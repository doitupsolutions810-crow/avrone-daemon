"""Round 84: deny switches read exactly as written. AVRONE_SELF_CHANGES / AVRONE_SELF_EVOLVE were
os.getenv(...).lower() == "deny": "deny " or '"deny"' left installs/self-evolution ON on a locked
node. Unknown words now deny. Also AVRONE_RECON/LESSONS/AGENT_CONTAINER/EVOLVE_LONG quoting and
AVRONE_ALLOW_ABS_READ (doctor said blank = on while the reader had it off)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from avrone import agentcontainer, agentkit, bridge, config as c, doctor, evolve, recon

ENVS = ("AVRONE_SELF_CHANGES", "AVRONE_SELF_EVOLVE", "AVRONE_RECON", "AVRONE_LESSONS",
        "AVRONE_AGENT_CONTAINER", "AVRONE_EVOLVE_LONG", "AVRONE_ALLOW_ABS_READ")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)


def run(co):
    return asyncio.run(co)


@pytest.mark.parametrize("raw,want", [(None, "x"), ("", "x"), ("  ", "x"), ("''", "x"), (' "A" ', "a"),
                                      ("DENY\n", "deny"), ("'log'", "log"), (5, "x")])
def test_switch_word(raw, want):
    assert c.switch_word(raw, "x") == want


@pytest.mark.parametrize("raw,want", [
    (None, "allow"), ("", "allow"), ("  ", "allow"), ("allow", "allow"), ("LOG", "log"), ("log", "log"),
    ("deny", "deny"), ("deny ", "deny"), ("DENY\n", "deny"), ('"deny"', "deny"), ("'deny'", "deny"),
    ("\tdeny\r", "deny"), ("denied", "deny"), ("dney", "deny"), ("off", "deny"), ("ｄｅｎｙ", "deny"),
    ("allow;", "deny"), ('"allow"', "allow"),
])
def test_self_changes_policy(raw, want):
    env = {} if raw is None else {"AVRONE_SELF_CHANGES": raw}
    assert c.self_changes_policy(env) == want


@pytest.mark.parametrize("raw,denied", [
    (None, False), ("", False), ("allow", False), ("ALLOW ", False), ("'allow'", False),
    ("deny", True), ("deny ", True), ('"deny"', True), ("Deny\n", True), ("no", True), ("alow", True),
])
def test_self_evolve_denied(raw, denied, monkeypatch):
    env = {} if raw is None else {"AVRONE_SELF_EVOLVE": raw}
    assert c.self_evolve_denied(env) is denied
    if raw is not None:
        monkeypatch.setenv("AVRONE_SELF_EVOLVE", raw)
    assert evolve._denied() is denied


def _ctx(tmp):
    class Runner:
        workspace = tmp / "ws"

        async def run(self, cmd, timeout=None):
            raise AssertionError("must not install on a denied node")
    (tmp / "ws").mkdir(exist_ok=True)
    core = SimpleNamespace(runner=Runner(), settings=SimpleNamespace(data_dir=tmp / "d"),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="Agent-1", depth=0)


@pytest.mark.parametrize("raw", ["deny ", '"deny"', "'deny'", "DENY\n", "dney"])
def test_env_install_refused(raw, monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_SELF_CHANGES", raw)
    out = run(agentkit.t_env_install(_ctx(tmp_path), {"package": "requests"}))
    assert out.startswith("ERROR: self-changes are disabled")
    assert ("unrecognised" in out) is (raw == "dney")


def test_evolve_propose_refused_with_padded_deny(monkeypatch):
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny ")
    assert run(evolve.t_evolve_propose(None, {})) == evolve.DENIED_MSG


@pytest.mark.parametrize("raw,ok", [('"allow"', True), ("'on'", True), (" allow ", True), ("", True),
                                    ('"deny"', False), ("'off'", False), ("maybe", False)])
def test_recon(raw, ok, monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", raw)
    assert (recon._denied() is None) is ok


@pytest.mark.parametrize("raw,on", [(None, True), ("", True), ('"off"', False), ("'0'", False), (" no ", False),
                                    ("on", True), ("'on'", True)])
def test_lessons(raw, on, monkeypatch):
    if raw is not None:
        monkeypatch.setenv("AVRONE_LESSONS", raw)
    assert bridge.lessons_enabled() is on


@pytest.mark.parametrize("raw,want", [('"persistent"', "persistent"), (" PERSISTENT\n", "persistent"),
                                      ("", "off"), ("'on'", "off"), ("persistentx", "off")])
def test_container_mode(raw, want, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", raw)
    assert agentcontainer.container_mode() == want


@pytest.mark.parametrize("raw,on", [('"allow"', True), (" ALLOW ", True), ("", False), ("yes", False), ("'deny'", False)])
def test_evolve_long(raw, on, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", raw)
    assert evolve.long_loops_allowed() is on


@pytest.mark.parametrize("raw,on", [(None, True), ("1", True), (" 1 ", True), ('"1"', True), ("", False),
                                    ("  ", False), ("0", False), ("yes", False), ("１", False)])
def test_abs_read(raw, on, monkeypatch):
    env = {} if raw is None else {"AVRONE_ALLOW_ABS_READ": raw}
    assert c.abs_read_allowed(env) is on
    if raw is not None:
        monkeypatch.setenv("AVRONE_ALLOW_ABS_READ", raw)
    assert agentkit._abs_read_ok() is on


def _toggles(env):
    return doctor.run_doctor(env=env, which=lambda n: None, sockets=())["toggles"]


@pytest.mark.parametrize("env,want", [
    ({"AVRONE_SELF_CHANGES": '"deny"'}, ("allow", "deny", "on")),
    ({"AVRONE_SELF_EVOLVE": "deny ", "AVRONE_SELF_CHANGES": "LOG"}, ("deny", "log", "on")),
    ({"AVRONE_SELF_EVOLVE": "nope", "AVRONE_SELF_CHANGES": "nope"}, ("deny", "deny", "on")),
    ({"AVRONE_ALLOW_ABS_READ": ""}, ("allow", "allow", "off")),
    ({"AVRONE_ALLOW_ABS_READ": '"1"'}, ("allow", "allow", "on")),
])
def test_doctor_reports_effective_policy(env, want):
    t = _toggles(env)
    assert (t["self_evolve"], t["self_changes"], t["abs_read"]) == want


def test_doctor_recon_quoted_allow_is_not_unrecognised():
    t = _toggles({"AVRONE_RECON": '"allow"'})
    assert t["recon"] == "allow"


def test_doctor_agrees_with_readers(monkeypatch):
    for raw in ("deny", "deny ", '"deny"', "x", "", "allow", "'log'"):
        env = {"AVRONE_SELF_CHANGES": raw, "AVRONE_SELF_EVOLVE": raw}
        monkeypatch.setenv("AVRONE_SELF_CHANGES", raw)
        monkeypatch.setenv("AVRONE_SELF_EVOLVE", raw)
        t = _toggles(env)
        assert (t["self_evolve"] == "deny") is evolve._denied()
        assert t["self_changes"] == c.self_changes_policy()
