from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from avrone import iris_memory


def reset_module():
    iris_memory._memory = None
    iris_memory._checked = False
    iris_memory._warned.clear()


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    reset_module()
    for key in (
        "AVRONE_IRIS_ENABLED", "AVRONE_IRIS_OWNER_ID",
        "IRIS_AGENT_MEMORY_URL", "IRIS_AGENT_MEMORY_STORE_ID",
        "IRIS_AGENT_MEMORY_API_KEY", "AVRONE_ENV", "ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    reset_module()


def fake_iris(monkeypatch):
    state = {"owner": None, "session_id": None, "turns": [], "query": None}

    class Conversation:
        def user(self, text):
            state["turns"].append(("user", text))

        def assistant(self, text):
            state["turns"].append(("assistant", text))

    class Memory:
        def conversation(self, *, owner, session_id):
            state["owner"] = owner
            state["session_id"] = session_id
            return Conversation()

        def recall(self, query, *, owner):
            state["query"] = (query, owner)
            return SimpleNamespace(as_prompt=lambda: "- extracted fact")

    class AgentMemory:
        @classmethod
        def from_env(cls):
            return Memory()

    parent = ModuleType("redis_iris")
    parent.__path__ = []
    child = ModuleType("redis_iris.agent_memory")
    child.AgentMemory = AgentMemory
    monkeypatch.setitem(sys.modules, "redis_iris", parent)
    monkeypatch.setitem(sys.modules, "redis_iris.agent_memory", child)
    for key, value in {
        "AVRONE_IRIS_ENABLED": "true",
        "AVRONE_IRIS_OWNER_ID": "operator-account-42",
        "IRIS_AGENT_MEMORY_URL": "https://iris.example.invalid",
        "IRIS_AGENT_MEMORY_STORE_ID": "test-store",
        "IRIS_AGENT_MEMORY_API_KEY": "test-key",
    }.items():
        monkeypatch.setenv(key, value)
    return state


def test_iris_is_disabled_by_default():
    assert iris_memory.record_turn("r1", "prompt", "answer") is False
    assert iris_memory.recall_prompt("preferences") == ""


def test_record_turn_uses_owner_scoped_session(monkeypatch):
    state = fake_iris(monkeypatch)
    assert iris_memory.record_turn("run123", "task text", "answer text") is True
    assert state["owner"] == "operator-account-42"
    assert state["session_id"] == "avrone-run123"
    assert state["turns"] == [("user", "task text"), ("assistant", "answer text")]


def test_semantic_recall_is_owner_scoped(monkeypatch):
    state = fake_iris(monkeypatch)
    assert iris_memory.recall_prompt("deployment preferences") == "- extracted fact"
    assert state["query"] == ("deployment preferences", "operator-account-42")


def test_missing_owner_or_service_config_fails_open(monkeypatch):
    monkeypatch.setenv("AVRONE_IRIS_ENABLED", "true")
    monkeypatch.setenv("IRIS_AGENT_MEMORY_URL", "https://iris.example.invalid")
    monkeypatch.setenv("IRIS_AGENT_MEMORY_STORE_ID", "test-store")
    monkeypatch.setenv("IRIS_AGENT_MEMORY_API_KEY", "test-key")
    assert iris_memory.record_turn("r", "p", "a") is False
