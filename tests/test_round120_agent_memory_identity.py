"""Round 120: an agent's memory namespace follows its persistent identity (agent id).

v0.112: memory lived under memstore.agent_namespace(name), and name is the role with every
non-alphanumeric dropped and cut to 16 chars ("Code Reviewer" -> "codereviewer", "Research
Analyst Lead" -> "researchanalystl"), while the agent's home dir and agent packs used the
agent id ("code-reviewer", "research-analyst-lead"). So export_pack shipped an empty memory for
those roles, import_pack merged memory into a namespace the agent never reads, and a long role
shared memory with any other role sharing its first 16 letters.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from avrone import agentpack, memstore
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import TrinityCore


class MemLLM:
    """Each worker saves one memory keyed by its focus, then finishes."""

    def __init__(self, roles):
        self.plan = json.dumps({"subtasks": [{"role": r, "focus": f"f{i}"} for i, r in enumerate(roles)]})
        self.saved = set()

    async def chat(self, messages, **kw):
        system = messages[0]["content"]
        if "You are the PLANNER" in system:
            return self.plan
        if "You are the LEAD" in system:
            return "merged"
        for m in messages:
            if "saved memory" in str(m.get("content", "")):
                return '{"final": "done"}'
        focus = system.split("Your focus: ", 1)[1].split("\n", 1)[0].strip() if "Your focus: " in system else "x"
        return json.dumps({"tool": "memory_save", "args": {"key": "note", "value": f"from {focus}"}})


def _core(tmp_path, llm):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.workspace.mkdir(parents=True, exist_ok=True)
    s.max_steps = 3
    core = TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir))
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(core.tools)
    return core


def _spaces(core):
    return memstore.MemoryStore(core.settings.data_dir / "memory.jsonl").namespaces()


@pytest.mark.parametrize("role,aid", [("Code Reviewer", "code-reviewer"),
                                      ("Research Analyst Lead", "research-analyst-lead"),
                                      ("fact_checker", "fact_checker")])
def test_memory_lands_in_agent_id_namespace(tmp_path, role, aid):
    core = _core(tmp_path, MemLLM([role]))
    asyncio.run(core.squad("t", n=1, run_id="r1"))
    spaces = _spaces(core)
    assert aid in spaces and spaces[aid]["note"]["value"] == "from f0", sorted(spaces)


def test_export_pack_carries_the_agents_memory(tmp_path):
    core = _core(tmp_path, MemLLM(["Code Reviewer"]))
    asyncio.run(core.squad("t", n=1, run_id="r1"))
    bundle = agentpack.export_pack(core.settings.workspace, core.settings.data_dir, "code-reviewer", origin="n1")
    if isinstance(bundle, tuple):
        bundle = bundle[0]
    manifest, mem, _ = agentpack.read_pack(bundle)
    assert manifest["memory"]["entries"] == 1 and mem[0]["value"] == "from f0"


def test_long_roles_sharing_a_prefix_do_not_share_memory(tmp_path):
    core = _core(tmp_path, MemLLM(["Research Analyst Lead", "Research Analyst Lackey"]))
    asyncio.run(core.squad("t", n=2, run_id="r1"))
    spaces = _spaces(core)
    a = spaces.get("research-analyst-lead", {}).get("note", {}).get("value")
    b = spaces.get("research-analyst-lackey", {}).get("note", {}).get("value")
    assert {a, b} == {"from f0", "from f1"}, sorted(spaces)


def test_legacy_namespace_is_carried_over(tmp_path):
    """Memory a v0.112 node saved under the old name-derived namespace is still recalled."""
    core = _core(tmp_path, MemLLM(["Code Reviewer"]))
    store = memstore.MemoryStore(core.settings.data_dir / "memory.jsonl")
    store.save("codereviewer", "old", "legacy fact", agent="CodeReviewer-1")
    asyncio.run(core.squad("t", n=1, run_id="r1"))
    spaces = _spaces(core)
    assert spaces["code-reviewer"]["old"]["value"] == "legacy fact"
    assert spaces["codereviewer"]["old"]["value"] == "legacy fact"  # copied, never moved


def test_simple_roles_keep_their_namespace(tmp_path):
    core = _core(tmp_path, MemLLM(["Builder"]))
    asyncio.run(core.squad("t", n=1, run_id="r1"))
    assert "builder" in _spaces(core)
