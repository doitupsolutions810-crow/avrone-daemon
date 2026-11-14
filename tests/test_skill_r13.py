"""Round 13 skills: exposure-hardening and deploy-readiness are valid, name only real tools, env vars
and sibling skills, and the hardening path flips a node from AT RISK to GUARDED."""
import asyncio
import re
from pathlib import Path

import pytest

from avrone import evolve, server
from avrone import tools_hostview as hv
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {"exposure-hardening": ["exposure_check", "api_surface", "research_note"],
          "deploy-readiness": ["router_status", "exposure_check", "state_health", "capabilities_report",
                               "env_report", "proc_status", "research_note"]}


@pytest.mark.parametrize("name", sorted(SKILLS))
def test_valid_and_grounded(name):
    body = (ROOT / "skills" / name / "SKILL.md").read_text()
    assert evolve.check_skill(name, body) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for t in SKILLS[name]:
        assert t in body and reg.get(t) is not None, t
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py")) + (ROOT / "docker-compose.yml").read_text()
    for env in set(re.findall(r"AVRONE_[A-Z_]+", body)):
        assert env in src, env
    for sib in re.findall(r"\b(local-model-ops|state-recovery|exposure-hardening)\b", body):
        assert (ROOT / "skills" / sib / "SKILL.md").exists(), sib


def test_hardening_path(monkeypatch):
    monkeypatch.setenv("AVRONE_HOST", "0.0.0.0")
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "all")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "admin_token", "")
    assert hv.exposure()["verdict"] == "AT RISK"
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "admin")       # step 3 mapping
    monkeypatch.setattr(server.settings, "admin_token", "Xk9mP2vL8qR4wZ7nB3cD5fG6hJ1tY0uE")
    out = asyncio.run(hv.t_exposure_check(None, {}))
    assert out.startswith("exposure_check: GUARDED") and "agent/run routes" not in out
