"""Multi-round deep analysis: successive waves, a hard cap, early stop on convergence."""
import asyncio

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.llm import LLMError, _mock_reply
from avrone.sovereign import LocalRunner
from avrone.trinity import (DEEP_ANGLES, MAX_DEEP_ROUNDS, TrinityCore, _corrections,
                            _reviewer_failed, deep_confidence, resolve_deep_rounds)


def _report(corrections: str, bottom: str = "Corrected answer.") -> str:
    return ("## Verdict\nCorrected. Confidence: High\n\n"
            f"## Corrections\n{corrections}\n\n## Additions\n- x\n\n## Counterpoints\n- y\n\n"
            f"## Sources\n- https://example.com\n\n## Bottom line\n{bottom}")


class ScriptedLLM:
    """The mock model for planner/workers/reviewers; deep-lead replies come from a script.

    Once the script runs out the last reply repeats. `boom` maps a reviewer role to an
    exception it raises, "hang" (never answers) or a junk reply string."""

    def __init__(self, lead_replies, boom=None):
        self.lead_replies = list(lead_replies)
        self.boom = boom or {}
        self.lead_inputs: list[str] = []

    async def chat(self, messages, **kw):
        system = messages[0]["content"]
        if "You are the DEEP LEAD" in system:
            self.lead_inputs.append(messages[1]["content"])
            i = min(len(self.lead_inputs) - 1, len(self.lead_replies) - 1)
            r = self.lead_replies[i]
            if isinstance(r, BaseException):
                raise r
            return r
        for role, what in self.boom.items():
            if f"the {role} reviewer" in system:
                if what == "hang":
                    await asyncio.sleep(3600)
                if isinstance(what, str):
                    return what  # a junk reply
                raise what
        return _mock_reply(messages)


def _core(tmp_path, llm, **over):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = 3
    for k, v in over.items():
        setattr(s, k, v)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir))


def _run(core, **kw):
    kw.setdefault("run_id", "dr")
    return asyncio.run(core.deep("Is X true?", "X is true since 2020.", n=kw.pop("n", 3), **kw))


@pytest.fixture(autouse=True)
def _no_env_rounds(monkeypatch):
    monkeypatch.delenv("AVRONE_DEEP_ROUNDS", raising=False)


# ---------------------------------------------------------------- knobs
def test_resolve_rounds_precedence_and_clamp(monkeypatch):
    assert resolve_deep_rounds(None) == 1
    assert resolve_deep_rounds(3) == 3
    assert resolve_deep_rounds(0) == 1 and resolve_deep_rounds(-4) == 1
    assert resolve_deep_rounds(99) == MAX_DEEP_ROUNDS == 5
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", "4")
    assert resolve_deep_rounds(None) == 4
    assert resolve_deep_rounds(2) == 2  # explicit beats env

    class S:
        deep_rounds = 3
    assert resolve_deep_rounds(None, S()) == 3  # settings beat env
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", "lots")
    assert resolve_deep_rounds(None) == 1
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", "50")
    assert resolve_deep_rounds(None) == MAX_DEEP_ROUNDS


def test_corrections_parser():
    assert _corrections(_report("None")) == []
    assert _corrections(_report("- None found by the mock Fact-Checker.")) == []
    assert _corrections(_report("N/A")) == []
    assert _corrections("no sections at all") == []
    got = _corrections(_report("- X is true since **2021**, not 2020.\n* Y  is   wrong\n1. Z\n- Y is wrong."))
    assert got == ["x is true since **2021**, not 2020", "y is wrong", "z"]


# ---------------------------------------------------------------- rounds
def test_default_is_single_round_with_mock_model(tmp_path):
    llm = ScriptedLLM([_report("- fix A")])  # a correction, but rounds default to 1
    r = _run(_core(tmp_path, llm))
    assert r["rounds_run"] == 1 and r["max_rounds"] == 1 and r["stop_reason"] == "cap"
    assert len(r["agents"]) == 3 and all(a["round"] == 1 for a in r["agents"])
    assert len(llm.lead_inputs) == 1 and "FIRST-WAVE ANSWER TO REVIEW" in llm.lead_inputs[0]


def test_rounds_run_and_feed_forward(tmp_path):
    llm = ScriptedLLM([_report("- fix A", "Answer v2."), _report("- fix B", "Answer v3."),
                       _report("- None")])
    core = _core(tmp_path, llm)
    r = _run(core, rounds=5)
    assert r["rounds_run"] == 3 and r["converged"] and r["stop_reason"] == "converged"
    assert [h["new_corrections"] for h in r["rounds"]] == [["fix a"], ["fix b"], []]
    # round 2 reviews round 1's bottom line and knows what is already fixed
    assert "Answer v2." in llm.lead_inputs[1] and "- fix a" in llm.lead_inputs[1]
    assert "Answer v3." in llm.lead_inputs[2] and "- fix b" in llm.lead_inputs[2]
    assert len(r["agents"]) == 9 and [a["round"] for a in r["agents"]][::3] == [1, 2, 3]
    log = core.codex.replay("dr")
    kinds = [e["kind"] for e in log]
    assert kinds[0] == "run.start" and log[0]["data"]["max_rounds"] == 5 and kinds[-1] == "run.done"
    assert kinds.count("deep.round.start") == 3 and kinds.count("deep.round.done") == 3
    assert kinds.count("run.done") == 1 and kinds.count("agent.spawn") == 9
    done = [e["data"] for e in log if e["kind"] == "deep.round.done"]
    assert [d["round"] for d in done] == [1, 2, 3] and [d["converged"] for d in done] == [False, False, True]
    summary = next(e["data"] for e in log if e["kind"] == "deep.done")
    assert summary["rounds_run"] == 3 and summary["stop_reason"] == "converged"


def test_cap_is_enforced(tmp_path):
    # every round finds something new: the loop must stop at the hard cap anyway
    llm = ScriptedLLM([_report(f"- fix {i}") for i in range(20)])
    r = _run(_core(tmp_path, llm), rounds=50)
    assert r["max_rounds"] == MAX_DEEP_ROUNDS and r["rounds_run"] == MAX_DEEP_ROUNDS
    assert len(llm.lead_inputs) == MAX_DEEP_ROUNDS and not r["converged"] and r["stop_reason"] == "cap"


def test_env_sets_rounds_and_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", "2")
    llm = ScriptedLLM([_report(f"- fix {i}") for i in range(9)])
    r = _run(_core(tmp_path, llm))
    assert r["rounds_run"] == 2 and r["max_rounds"] == 2


def test_early_stop_on_first_round_convergence_with_mock_model(tmp_path):
    # the stock mock deep lead reports "None found": converged after one wave
    class Mock:
        async def chat(self, messages, **kw):
            return _mock_reply(messages)

    r = _run(_core(tmp_path, Mock()), rounds=4)
    assert r["rounds_run"] == 1 and r["converged"] and r["stop_reason"] == "converged"


def test_repeated_corrections_count_as_converged(tmp_path):
    llm = ScriptedLLM([_report("- fix A"), _report("- Fix   A.")])  # same point, reworded case/space
    r = _run(_core(tmp_path, llm), rounds=5)
    assert r["rounds_run"] == 2 and r["converged"]
    assert r["rounds"][1]["corrections"] == ["fix a"] and r["rounds"][1]["new_corrections"] == []


def test_one_failing_reviewer_does_not_sink_the_wave(tmp_path):
    llm = ScriptedLLM([_report("- fix A"), _report("None")],
                      boom={"Gap-Finder": RuntimeError("reviewer socket exploded")})
    core = _core(tmp_path, llm)
    r = _run(core, rounds=3)
    assert r["rounds_run"] == 2 and r["converged"]
    for rnd in (1, 2):
        res = [a["result"] for a in r["agents"] if a["round"] == rnd]
        assert "failed" in res[1] and "socket exploded" in res[1]
        assert "Fact-Checker review" in res[0] and "Contrarian review" in res[2]
    # the lead still merged the healthy reviews in every round
    assert all(i.count("review: first answer checked") == 2 for i in llm.lead_inputs)
    assert all(a["status"] == "done" for a in core.active.values())


def test_lead_failure_stops_rounds_without_claiming_convergence(tmp_path):
    llm = ScriptedLLM([_report("- fix A"), LLMError("lead down")])
    r = _run(_core(tmp_path, llm), rounds=4)
    assert r["rounds_run"] == 2 and not r["converged"] and r["stop_reason"] == "lead_failed"
    assert r["report"].startswith("Deep lead failed") and "Raw reviews" in r["report"]


def test_deep_without_first_answer_still_multi_round(tmp_path):
    llm = ScriptedLLM([_report("- fix A"), _report("None")])
    r = asyncio.run(_core(tmp_path, llm).deep("build a todo app", n=2, rounds=3, run_id="nf"))
    assert r["first_answer"].startswith("FINAL ANSWER") and r["first_run_id"] == "nf-wave1"
    assert r["rounds_run"] == 2 and [a["role"] for a in r["agents"][:2]] == [a["role"] for a in DEEP_ANGLES[:2]]


# ---------------------------------------------------------------- robustness + reporting
def test_hung_reviewer_times_out_and_rest_merge(tmp_path):
    llm = ScriptedLLM([_report("None")], boom={"Contrarian": "hang"})
    # Healthy workers also write durable events; allow scheduler and disk jitter.
    core = _core(tmp_path, llm, agent_timeout=2.0)
    r = asyncio.run(asyncio.wait_for(core.deep("q", "a", n=3, rounds=3, run_id="h"), 10))
    res = [a["result"] for a in r["agents"]]
    assert "timed out" in res[2] and not r["agents"][2]["ok"] and r["agents"][0]["ok"]
    assert llm.lead_inputs[0].count("review: first answer checked") == 2
    assert r["reviewers_ok"] == 2 and r["reviewers_failed"] == 1
    assert "Reviewers: 2 of 3 returned usable reviews." in r["report"]
    assert all(a["status"] == "done" for a in core.active.values())


def test_crashing_reviewer_noted_in_report_and_confidence_discounted(tmp_path):
    # 2 of 3 reviewers crash: the lead still merges, but confidence drops one level
    llm = ScriptedLLM([_report("None")], boom={"Fact-Checker": RuntimeError("x"), "Gap-Finder": KeyError("y")})
    r = _run(_core(tmp_path, llm), rounds=2)
    assert len(llm.lead_inputs) == 1 and r["converged"]
    assert r["confidence"] == "Med" and r["report"].rstrip().endswith("Confidence: Med")


def test_junk_reviewer_json_is_nudged_then_accepted(tmp_path):
    junk = '```json\n{"final": "unterminated, "tool": }\n```'
    llm = ScriptedLLM([_report("None")], boom={"Gap-Finder": junk})
    core = _core(tmp_path, llm)
    r = _run(core, rounds=2)
    kinds = [e["kind"] for e in core.codex.replay("dr")]
    assert kinds.count("action.malformed") == 2  # bounded nudges, then the reply is kept as prose
    gap = r["agents"][1]["result"]
    assert "unterminated" in gap and r["agents"][1]["ok"]
    assert r["rounds_run"] == 1 and r["reviewers_ok"] == 3 and kinds[-1] == "run.done"


def test_junk_lead_without_sections_counts_as_converged(tmp_path):
    llm = ScriptedLLM(["lol no markdown here {\"tool\": 1}"])
    r = _run(_core(tmp_path, llm), rounds=3)
    assert r["rounds_run"] == 1 and r["converged"] and r["confidence"] == "Med"


def test_every_reviewer_failing_skips_lead_and_reports_unverified(tmp_path):
    boom = {a["role"]: RuntimeError("down") for a in DEEP_ANGLES}
    llm = ScriptedLLM([_report("- should never be used")], boom=boom)
    r = _run(_core(tmp_path, llm), rounds=4)
    assert llm.lead_inputs == []  # nothing to weigh, so no lead call
    assert r["rounds_run"] == 1 and r["stop_reason"] == "no_reviews" and not r["converged"]
    assert r["confidence"] == "Low" and "Unverified: every reviewer failed" in r["report"]
    assert "X is true since 2020." in r["report"] and "## Reviewer errors" in r["report"]
    assert "stopped: no usable reviews came back" in r["report"]


def test_zero_reviewers_is_graceful(tmp_path):
    llm = ScriptedLLM([_report("- unused")])
    core = _core(tmp_path, llm)
    r = _run(core, n=0, rounds=3)
    assert r["agents"] == [] and llm.lead_inputs == []
    assert r["stop_reason"] == "no_reviews" and r["confidence"] == "Low"
    assert "Unverified: no reviewers were assigned" in r["report"]
    for section in ("## Verdict", "## Corrections", "## Bottom line", "## Deep rounds"):
        assert section in r["report"]
    kinds = [e["kind"] for e in core.codex.replay("dr")]
    assert "agent.spawn" not in kinds and kinds[-1] == "run.done"


def test_lead_crashing_with_non_llm_error_keeps_reviews(tmp_path):
    llm = ScriptedLLM([RuntimeError("lead exploded")])
    r = _run(_core(tmp_path, llm), rounds=3)
    assert r["stop_reason"] == "lead_failed" and r["confidence"] == "Low"
    assert "lead exploded" in r["report"] and "### Reviewer 1: Fact-Checker" in r["report"]


def test_empty_lead_reply_is_a_lead_failure(tmp_path):
    llm = ScriptedLLM(["   "])
    r = _run(_core(tmp_path, llm), rounds=3)
    assert r["stop_reason"] == "lead_failed" and "empty reply" in r["report"]


def test_report_footer_notes_rounds_and_confidence(tmp_path):
    llm = ScriptedLLM([_report("- fix A"), _report("- fix B"), _report("None")])
    core = _core(tmp_path, llm)
    r = _run(core, rounds=5)
    assert "## Deep rounds\nRounds run: 3 of 5 (converged: no new corrections in round 3)." in r["report"]
    assert r["confidence"] == "High" and r["answer"] == r["report"]
    assert r["report"].index("## Bottom line") < r["report"].index("## Deep rounds")
    done = next(e["data"] for e in core.codex.replay("dr") if e["kind"] == "deep.done")
    assert done["confidence"] == "High" and done["reviewers_ok"] == 9


def test_cap_hit_still_correcting_lowers_confidence(tmp_path):
    llm = ScriptedLLM([_report(f"- fix {i}") for i in range(5)])
    r = _run(_core(tmp_path, llm), rounds=2)
    assert r["stop_reason"] == "cap" and r["confidence"] == "Med"
    assert "stopped at the round cap, still finding corrections" in r["report"]


def test_confidence_and_failure_helpers():
    hi = _report("None")
    assert deep_confidence(hi, True, 3, 0, True, 1) == "High"
    assert deep_confidence(hi, True, 1, 1, True, 1) == "Med"
    assert deep_confidence(hi, True, 1, 2, False, 3) == "Low"
    assert deep_confidence(hi, False, 3, 0, True, 1) == "Low"
    assert deep_confidence(hi, True, 0, 0, True, 1) == "Low"
    assert deep_confidence("## Verdict\nExtended. Confidence: **Medium**", True, 2, 0, True, 1) == "Med"
    assert deep_confidence("no verdict", True, 2, 0, False, 1) == "Med"
    assert _reviewer_failed("[Gap-Finder-3 failed: timed out after 1s]")
    assert _reviewer_failed("[agent 2 failed: RuntimeError: x]") and _reviewer_failed("  ")
    assert not _reviewer_failed("The claim [ref 1] failed: no, it holds.")
    assert not _reviewer_failed("Mock Fact-Checker review: first answer checked.")
