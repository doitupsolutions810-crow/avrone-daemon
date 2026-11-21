"""Round 167: checkpoint describe()/list_managed() previews are redacted (secrets were served raw)."""
from __future__ import annotations

from avrone import checkpoint as c


def _secret() -> str:
    return "xai-" + "Q7" * 20  # built at run time (push protection)


def test_task_and_answer_previews_redacted(tmp_path):
    p = c.managed_path(tmp_path, "k1")
    pw = "Hunter2" + "Secret9"
    ck = c.Checkpoint(p)
    ck.begin(f"deploy with {_secret()} --password {pw}", "squad", "r1")
    ck.put("result", {"answer": f"used key {_secret()}"})
    full = c.describe(p, full=True)
    for text in (full["task"], full["answer_preview"], c.list_managed(tmp_path)[0]["task"]):
        assert _secret() not in text and "Q7Q7Q7" not in text
        assert pw not in text
    assert full["task"].startswith("deploy with")


def test_secret_at_cut_point_not_partly_shown(tmp_path):
    p = c.managed_path(tmp_path, "k2")
    ck = c.Checkpoint(p)
    ck.begin("x" * 100 + " " + _secret(), "squad", "r1")
    assert "Q7Q7" not in c.describe(p)["task"]


def test_stored_steps_untouched(tmp_path):
    p = c.managed_path(tmp_path, "k3")
    ck = c.Checkpoint(p)
    ck.put("step", {"a": _secret()})
    assert c.Checkpoint(p).get("step") == {"a": _secret()}  # resume sees exact values
