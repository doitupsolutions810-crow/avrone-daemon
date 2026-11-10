import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone import tools_introspect as ti
from avrone.config import Settings
from avrone.tools import ToolRegistry

SECRET = "sk-THISISAVERYSECRETVALUE1234567890"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("AVRONE_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", SECRET)
    monkeypatch.setenv("AVRONE_LLM_API_KEY", SECRET)
    monkeypatch.setenv("AVRONE_LLM_BASE_URL", "https://user:hunter2pass@llm.example/v1?api_key=abc123&x=1")
    monkeypatch.setenv("AVRONE_SQUAD_SIZE", "5")
    monkeypatch.setenv("AVRONE_FLEET_PARENT", "https://parent.example:7777")
    monkeypatch.setenv("AVRONE_WEIRD_NOTE", "bearer abcdefghijklmnop")  # token-shaped under a harmless name
    monkeypatch.setenv("NOT_AVRONE_SECRET", "outside-prefix")
    monkeypatch.setenv("AVRONE_PROVIDER", "custom")
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    ctx = SimpleNamespace(core=SimpleNamespace(settings=s, tools=reg), run_id="r1", name="Worker-1", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, s=s)


def call(env, **args):
    return run(env.reg.call(env.ctx, "env_report", args))


def test_registered(env):
    t = env.reg.get("env_report")
    assert t is not None and t.source == "builtin"
    assert env.reg.check("env_report", {"prefix": 3})[0] == "bad_args"


def test_text_report_redacts_everything_secret(env):
    out = call(env)
    assert SECRET not in out and "hunter2pass" not in out and "abc123" not in out
    assert "abcdefghijklmnop" not in out
    assert "AVRONE_ADMIN_TOKEN = ***set***" in out
    assert "AVRONE_LLM_API_KEY = ***set***" in out
    assert "admin_token = ***set***" in out and "llm_api_key = ***set***" in out
    assert "https://***@llm.example/v1?api_key=***&x=1" in out
    assert "AVRONE_WEIRD_NOTE = ***redacted (token-shaped value)***" in out
    assert "AVRONE_SQUAD_SIZE = 5" in out and "squad_size = 5" in out
    assert "AVRONE_FLEET_PARENT = https://parent.example:7777" in out
    assert "NOT_AVRONE_SECRET" not in out and "outside-prefix" not in out


def test_json_and_prefix(env):
    d = json.loads(call(env, format="json", prefix="llm"))
    assert d["prefix"] == "AVRONE_LLM"
    assert set(d["env"]) == {"AVRONE_LLM_API_KEY", "AVRONE_LLM_BASE_URL"}
    assert d["settings"] and all(k.startswith("llm") for k in d["settings"])
    assert d["settings"]["llm_api_key"] == "***set***"
    assert SECRET not in json.dumps(d)
    d2 = json.loads(call(env, format="json", prefix="AVRONE_ADMIN"))
    assert d2["env"] == {"AVRONE_ADMIN_TOKEN": "***set***"}
    assert d2["settings"] == {"admin_token": "***set***", "admin_token_previous": "unset"}


@pytest.mark.parametrize("bad", ["../x", "a b", "AVRONE-*", "X" * 100, "$HOME"])
def test_bad_prefix(env, bad):
    assert call(env, prefix=bad).startswith("ERROR: prefix")


def test_unset_secret_and_empty(env, monkeypatch):
    monkeypatch.delenv("AVRONE_ADMIN_TOKEN")
    env.s.admin_token = ""
    out = call(env, prefix="ADMIN")
    assert "admin_token = unset" in out and "(none set)" in out


def test_bounded(env, monkeypatch):
    monkeypatch.setattr(ti, "ENV_REPORT_MAX_VARS", 3)
    for i in range(10):
        monkeypatch.setenv(f"AVRONE_ZZ_{i}", "v" * 5000)
    d = json.loads(call(env, prefix="ZZ", format="json"))
    assert len(d["env"]) == 3 and d["env_total"] == 10 and d["env_truncated"] == 7
    assert all(len(v) <= ti.ENV_REPORT_MAX_VALUE + 3 for v in d["env"].values())
    assert "7 more not shown" in call(env, prefix="ZZ")
    assert len(call(env)) <= ti.ENV_REPORT_MAX_CHARS + 20


@pytest.mark.parametrize("name,value,hidden", [
    ("AVRONE_X", "ghp_abcdefghijklmnopqrstuvwxyz", True),
    ("AVRONE_X", "AKIAABCDEFGHIJKLMNOP", True),
    ("AVRONE_X", "-----BEGIN RSA PRIVATE KEY-----", True),
    ("AVRONE_X", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0", True),
    ("AVRONE_DB_PASSWORD", "plain", True),
    ("AVRONE_SESSION_ID", "plain", True),
    ("AVRONE_X", "python:3.12-slim", False),
    ("AVRONE_X", "/workspace/skeleton_directory_name", False),
])
def test_redact_value(name, value, hidden):
    out = ti.redact_value(name, value)
    assert (value not in out) is hidden


def test_control_chars_and_no_settings(monkeypatch):
    assert "\n" not in ti.redact_value("AVRONE_X", "a\nb\x00c")
    reg = ToolRegistry()
    ti.register_introspect_tools(reg)
    ctx = SimpleNamespace(core=SimpleNamespace(), run_id="r", name="A", depth=0)
    out = run(reg.call(ctx, "env_report", {}))
    assert "settings in effect:\n  (none)" in out
