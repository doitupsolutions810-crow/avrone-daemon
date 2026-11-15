"""Round 88: the word and text settings (runner, LLM mode, provider, docker image, models,
fallbacks), the evolve gate command and repo dir, and the fusion delivery/watch URLs and paths
are read the way every other env value is: trimmed, one pair of wrapping quotes removed, blank
means the default."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from avrone import config, evolve, fusion_deliver, gateway
from avrone.config import Settings


def _settings(monkeypatch, **env):
    for k in ("AVRONE_RUNNER", "AVRONE_LLM_MODE", "AVRONE_PROVIDER", "AVRONE_DOCKER_IMAGE",
              "AVRONE_LLM_MODEL", "AVRONE_LEAD_MODEL", "AVRONE_LLM_FALLBACKS", "AVRONE_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


# --- runner: a quoted/padded/capitalised "docker" used to boot the LOCAL runner -----------------

@pytest.mark.parametrize("raw,want", [
    ('"docker"', "docker"), ("'docker'", "docker"), ("Docker", "docker"), (" docker\n", "docker"),
    ("DOCKER", "docker"), ('"local"', "local"), ("Auto", "auto"), ("", "auto"), ("   ", "auto"),
    ('""', "auto"), ("podman", "podman"),
])
def test_runner_word(monkeypatch, raw, want):
    assert _settings(monkeypatch, AVRONE_RUNNER=raw).runner == want


def test_quoted_docker_runner_is_not_local(monkeypatch, tmp_path):
    """sovereign.establish compares settings.runner to "docker"/"auto"; anything else went local."""
    from avrone import sovereign
    s = _settings(monkeypatch, AVRONE_RUNNER='"docker"', AVRONE_DATA_DIR=str(tmp_path / "d"),
                  AVRONE_WORKSPACE=str(tmp_path / "w"))
    seen = {}

    class FakeDocker:
        def __init__(self, settings):
            seen["kind"] = "docker"
            raise RuntimeError("no docker here")

    monkeypatch.setattr(sovereign, "DockerRunner", FakeDocker)
    with pytest.raises(RuntimeError):
        asyncio.run(sovereign.establish(s))
    assert seen == {"kind": "docker"}  # explicit docker never silently falls back to local


def test_unknown_runner_still_reported_by_doctor(monkeypatch):
    s = _settings(monkeypatch, AVRONE_RUNNER=' "Podman" ')
    assert s.runner == "podman"


# --- LLM mode: '"mock"' used to call a live model --------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ('"mock"', "mock"), ("Mock", "mock"), (" mock ", "mock"), ("'MOCK'", "mock"),
    ("", "live"), ("  ", "live"), ('"live"', "live"), ("live", "live"),
])
def test_llm_mode_word(monkeypatch, raw, want):
    assert _settings(monkeypatch, AVRONE_LLM_MODE=raw).llm_mode == want


def test_quoted_mock_mode_never_dials(monkeypatch):
    from avrone.llm import LLM
    s = _settings(monkeypatch, AVRONE_LLM_MODE='"mock"')
    assert s.llm_mode == "mock"
    llm = LLM(s)
    assert llm.s.llm_mode == "mock"


# --- provider: a quoted provider raised ValueError at import ---------------------------------

@pytest.mark.parametrize("raw,want", [
    ('"xai"', "xai"), ("'ollama'", "ollama"), (" XAI ", "xai"), ("", "openclaw"), ("  ", "openclaw"),
    ('""', "openclaw"),
])
def test_provider_word(monkeypatch, raw, want):
    assert _settings(monkeypatch, AVRONE_PROVIDER=raw).provider == want


def test_unknown_provider_still_refused(monkeypatch):
    with pytest.raises(ValueError):
        _settings(monkeypatch, AVRONE_PROVIDER='"bogus"')


@pytest.mark.parametrize("bad", [None, 3, ["xai"]])
def test_resolve_provider_non_text_is_default(bad):
    name, *_ = config.resolve_provider(bad, env={})
    assert name == "openclaw"


# --- docker image / models ------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ('"python:3.12-slim"', "python:3.12-slim"), ("'alpine:3'", "alpine:3"), ("  ubuntu  ", "ubuntu"),
    ("", "python:3.12-slim"), ("   ", "python:3.12-slim"), ('""', "python:3.12-slim"),
])
def test_docker_image(monkeypatch, raw, want):
    s = _settings(monkeypatch, AVRONE_DOCKER_IMAGE=raw)
    assert s.docker_image == want
    from avrone import agentcontainer
    assert agentcontainer.check_image(s.docker_image) == want


def test_flag_image_still_refused(monkeypatch):
    from avrone import agentcontainer
    s = _settings(monkeypatch, AVRONE_DOCKER_IMAGE='"--privileged"')
    with pytest.raises(ValueError):
        agentcontainer.check_image(s.docker_image)


@pytest.mark.parametrize("raw,want", [('"grok-4"', "grok-4"), (" big ", "big"), ("", ""), ("  ", "")])
def test_lead_model(monkeypatch, raw, want):
    assert _settings(monkeypatch, AVRONE_LEAD_MODEL=raw).lead_model == want


def test_blank_llm_model_takes_preset(monkeypatch):
    s = _settings(monkeypatch, AVRONE_PROVIDER="ollama", AVRONE_LLM_MODEL="   ")
    assert s.llm_model == config.PROVIDERS["ollama"][1]


def test_clean_text_settings_leaves_non_text_alone():
    class S:
        runner = None
        llm_mode = 5
        docker_image = ["x"]
    s = S()
    config.clean_text_settings(s)
    assert s.runner is None and s.llm_mode == 5 and s.docker_image == ["x"]


# --- fallbacks -------------------------------------------------------------------------------

def _ups(monkeypatch, raw):
    s = _settings(monkeypatch, AVRONE_PROVIDER="xai", AVRONE_LLM_MODE="live")
    env = {"AVRONE_LLM_FALLBACKS": raw}
    return [(u.provider, u.model) for u in gateway.parse_upstreams(s, env)[1:]]


@pytest.mark.parametrize("raw", ['"openrouter:x-ai/grok-4,ollama:llama3.1"',
                                 "'openrouter:x-ai/grok-4', 'ollama:llama3.1'",
                                 " openrouter:x-ai/grok-4 ,\nollama:llama3.1 "])
def test_quoted_fallbacks(monkeypatch, raw):
    assert _ups(monkeypatch, raw) == [("openrouter", "x-ai/grok-4"), ("ollama", "llama3.1")]


@pytest.mark.parametrize("raw", ["", "  ", '""', ",,", "' , '"])
def test_blank_fallbacks(monkeypatch, raw):
    assert _ups(monkeypatch, raw) == []


def test_no_model_keeps_a_quote(monkeypatch):
    for _, m in _ups(monkeypatch, '"ollama:llama3.1"'):
        assert '"' not in m and "'" not in m


# --- evolve gate command: a blank command passed every proposal ------------------------------

@pytest.mark.parametrize("arg", ["", " ", "  \t", "\n", '""', "''", '" "', None, 0, 5, [], ["pytest"], {"a": 1}])
def test_blank_or_non_text_gate_is_default(monkeypatch, arg):
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    assert evolve.gate_command(arg) == evolve.DEFAULT_TEST_CMD


@pytest.mark.parametrize("env", ["", " ", "  \n", '""', "' '"])
def test_blank_env_gate_is_default(monkeypatch, env):
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", env)
    assert evolve.gate_command(None) == evolve.DEFAULT_TEST_CMD
    assert evolve.gate_command("  ") == evolve.DEFAULT_TEST_CMD


@pytest.mark.parametrize("raw,want", [
    ('"pytest -q"', "pytest -q"), ("'make test'", "make test"), ("  pytest -x \n", "pytest -x"),
    ('"a" b "c"', '"a" b "c"'), ("pytest -k 'x or y'", "pytest -k 'x or y'"),
    ("'pytest -k \"a\"'", 'pytest -k "a"'), ('"', '"'), ("false", "false"),
])
def test_gate_command_text(monkeypatch, raw, want):
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    assert evolve.gate_command(raw) == want


def test_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", "make env")
    assert evolve.gate_command("make arg") == "make arg"
    assert evolve.gate_command(None) == "make env"
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", '"make quoted"')
    assert evolve.gate_command(None) == "make quoted"


def test_blank_gate_never_runs_a_bare_assignment(monkeypatch):
    """The shell line was f"PYTHONPATH=... {test_cmd} 2>&1"; with a blank command it is just an
    assignment and exits 0. The command that reaches the shell must never be blank."""
    import subprocess
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    for arg in ("", "   ", '""'):
        cmd = evolve.gate_command(arg)
        assert cmd.strip()
    r = subprocess.run("PYTHONPATH=/x   2>&1", shell=True)
    assert r.returncode == 0  # why a blank command used to "pass"


# --- evolve repo dir -------------------------------------------------------------------------

def test_blank_repo_dir_is_the_package_repo(monkeypatch):
    pkg_repo = Path(evolve.__file__).resolve().parent.parent
    for raw in ("", "   ", '""', "' '"):
        monkeypatch.setenv("AVRONE_REPO_DIR", raw)
        assert evolve._repo_root(None) == pkg_repo


def test_quoted_repo_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_REPO_DIR", f'"{tmp_path}"')
    assert evolve._repo_root(None) == tmp_path.resolve()
    monkeypatch.setenv("AVRONE_REPO_DIR", f"  {tmp_path}\n")
    assert evolve._repo_root(None) == tmp_path.resolve()


def test_tilde_repo_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AVRONE_REPO_DIR", "~/r")
    assert evolve._repo_root(None) == (tmp_path / "r").resolve()


# --- fusion delivery -------------------------------------------------------------------------

def _cfg(monkeypatch, **env):
    for k in ("CK_BASE", "CK_EVENTS_PATH", "CK_HEALTH_PATH", "AVRONE_FUSION_STATE_PATH"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return fusion_deliver.config_from_env()


@pytest.mark.parametrize("raw,want", [
    ('"http://ck:9000"', "http://ck:9000"), ("'http://ck:9000/'", "http://ck:9000"),
    ("  http://ck:9000/ \n", "http://ck:9000"), ("", "http://127.0.0.1:8090"), ("  ", "http://127.0.0.1:8090"),
    ('""', "http://127.0.0.1:8090"), ("/", "http://127.0.0.1:8090"),
])
def test_ck_base(monkeypatch, raw, want):
    assert _cfg(monkeypatch, CK_BASE=raw).base == want


@pytest.mark.parametrize("raw,want", [
    ('"/v2/events"', "/v2/events"), ("  /v2/events ", "/v2/events"), ("v2/events", "/v2/events"),
    ("", "/v1/events"), ("   ", "/v1/events"), ("''", "/v1/events"),
])
def test_events_path(monkeypatch, raw, want):
    assert _cfg(monkeypatch, CK_EVENTS_PATH=raw).events_path == want


@pytest.mark.parametrize("raw,want", [('"/ok"', "/ok"), ("ok", "/ok"), (" ", "/health"), ("", "/health")])
def test_health_path(monkeypatch, raw, want):
    assert _cfg(monkeypatch, CK_HEALTH_PATH=raw).health_path == want


def test_url_path_non_text():
    assert fusion_deliver._url_path(None, "/d") == "/d"
    assert fusion_deliver._url_path(7, "/d") == "/d"


def test_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FUSION_STATE_PATH", f'"{tmp_path}/s.json"')
    assert fusion_deliver.default_state_path() == Path(f"{tmp_path}/s.json")
    for blank in ("", "  ", '""'):
        monkeypatch.setenv("AVRONE_FUSION_STATE_PATH", blank)
        assert fusion_deliver.default_state_path(tmp_path) == tmp_path / "fusion" / "delivery-state.json"
        assert fusion_deliver.default_state_path() is None
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AVRONE_FUSION_STATE_PATH", "~/st.json")
    assert fusion_deliver.default_state_path() == tmp_path / "st.json"


def test_tokens_stay_verbatim(monkeypatch):
    """Fusion tokens are compared byte for byte by the relay; round 88 does not touch them."""
    monkeypatch.delenv("CK_FUSION_TOKEN", raising=False)
    monkeypatch.setenv("FUSION_TOKEN", " tok ")
    assert fusion_deliver.config_from_env().token == " tok "


# --- fusion watch endpoint -------------------------------------------------------------------

def test_fusion_watch_quoted_urls_dial_clean(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server, fusion_peer
    seen = {}

    class W:
        def watch_once(self, **kw):
            seen.update(kw)
            return {"watched": True}

    monkeypatch.setattr(fusion_peer, "shared_watch", lambda p: (seen.__setitem__("path", p), W())[1])
    monkeypatch.setattr(server, "require_admin", lambda t: None)
    monkeypatch.setenv("FUSION_BOARD_URL", '"http://b/x"')
    monkeypatch.setenv("FUSION_EVENTS_URL", "'http://e/x'")
    monkeypatch.setenv("FUSION_HEALTH_URL", " http://h/x \n")
    monkeypatch.setenv("AVRONE_FUSION_WATCH_PATH", '"/tmp/w.json"')
    r = TestClient(server.app).post("/api/fusion/watch")
    assert r.status_code == 200, r.text
    assert seen["board_url"] == "http://b/x"
    assert seen["events_url"] == "http://e/x"
    assert seen["health_url"] == "http://h/x"
    assert seen["path"] == "/tmp/w.json"


@pytest.mark.parametrize("blank", ['""', "  ", "''"])
def test_fusion_watch_blank_quoted_url_is_unset(monkeypatch, blank):
    from fastapi.testclient import TestClient
    from avrone import server
    monkeypatch.setattr(server, "require_admin", lambda t: None)
    monkeypatch.setenv("FUSION_BOARD_URL", blank)
    monkeypatch.setenv("FUSION_EVENTS_URL", "http://e")
    monkeypatch.setenv("FUSION_HEALTH_URL", "http://h")
    r = TestClient(server.app).post("/api/fusion/watch")
    assert r.json() == {"watched": False, "reason": "unset"}
