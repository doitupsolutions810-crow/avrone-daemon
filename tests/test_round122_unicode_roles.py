"""Round 122: roles in non-Latin scripts get their own identity.

v0.114: sanitize_agent_id dropped every non-ASCII character, so every Chinese, Greek, Cyrillic,
Arabic... role became "agent": one shared home dir, one shared memory namespace and one agent
pack for all of them, across runs. Mixed roles collided on their ASCII part ("数据 Analyst" and
"代码 Analyst" were both "analyst"), and "Rédacteur" lost its letter ("r-dacteur").
"""
from __future__ import annotations

import asyncio

import pytest

from avrone import agentfs, memstore
from tests.test_round120_agent_memory_identity import MemLLM, _core

ROLES = ["研究员", "代码审查员", "Ελεγκτής", "Аналитик", "محلل", "数据 Analyst", "代码 Analyst",
         "Rédacteur", "Redacteur"]


def test_non_latin_roles_get_distinct_ids():
    ids = [agentfs.sanitize_agent_id(r) for r in ROLES]
    assert len(set(ids)) == len(ids), ids
    assert "agent" not in ids


@pytest.mark.parametrize("role", ROLES + ["Builder", "Code Reviewer", "fact_checker", "", None, "???", "🔥 Hacker"])
def test_ids_are_safe_stable_and_idempotent(role):
    a = agentfs.sanitize_agent_id(role)
    assert a == agentfs.sanitize_agent_id(role)
    assert agentfs.sanitize_agent_id(a) == a
    assert memstore.is_local(a) and len(a) <= agentfs.MAX_ID_LEN
    assert all(c.isascii() for c in a) and "/" not in a and "." not in a


def test_ascii_roles_keep_their_ids():
    assert agentfs.sanitize_agent_id("Builder") == "builder"
    assert agentfs.sanitize_agent_id("Code Reviewer") == "code-reviewer"
    assert agentfs.sanitize_agent_id("🔥 Hacker") == "hacker"  # symbols are decoration, not identity
    assert agentfs.sanitize_agent_id("") == "agent"


def test_case_and_normalization_map_to_one_agent():
    nfd = "Re\u0301dacteur"  # e + combining acute
    assert agentfs.sanitize_agent_id("Rédacteur") == agentfs.sanitize_agent_id("RÉDACTEUR") \
        == agentfs.sanitize_agent_id(nfd)


def test_long_non_latin_role_fits():
    a = agentfs.sanitize_agent_id("研究" * 200 + " analyst " * 20)
    assert len(a) <= agentfs.MAX_ID_LEN and memstore.is_local(a)


def test_cjk_agents_in_separate_runs_do_not_share_memory(tmp_path):
    core = _core(tmp_path, MemLLM(["研究员"]))
    asyncio.run(core.squad("t", n=1, run_id="r1"))
    core.llm = MemLLM(["代码审查员"])
    core.llm.plan = core.llm.plan.replace('"f0"', '"g0"')
    asyncio.run(core.squad("t", n=1, run_id="r2"))
    spaces = memstore.MemoryStore(core.settings.data_dir / "memory.jsonl").namespaces()
    vals = sorted(v["note"]["value"] for ns, v in spaces.items() if "note" in v)
    assert vals == ["from f0", "from g0"], sorted(spaces)
    assert "agent" not in spaces
