"""Hardening round 63: research_note / lessons must never store a credential assignment, and
pack_list lists packs in a stable order."""
import asyncio
import json
import os
from types import SimpleNamespace as S

import pytest

from avrone import tools_researchnote as rn

LEAKS = [
    ("password=hunter2xyz", "hunter2xyz"),
    ("DB_PASSWORD: s3cr3tPass!", "s3cr3tPass"),
    ("api_key = abcd1234efgh5678", "abcd1234efgh5678"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("x-api-key: 0123456789abcdef0123", "0123456789abcdef0123"),
    ("aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("client_secret: 9f8e7d6c5b4a39281706", "9f8e7d6c5b4a39281706"),
]


def run(c):
    return asyncio.run(c)


@pytest.fixture
def lessons(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_LESSONS", "1")
    return tmp_path


def note(tmp_path, text, kind="finding", topic=None, run_id="r63"):
    ctx = S(core=S(settings=S(data_dir=tmp_path)), run_id=run_id, name="a1", role="")
    args = {"text": text, "kind": kind}
    if topic is not None:
        args["topic"] = topic
    return run(rn.t_research_note(ctx, args))


def stored(tmp_path):
    out = ""
    for f in tmp_path.rglob("*.jsonl"):
        out += f.read_text(errors="replace")
    return out


@pytest.mark.parametrize("text,secret", LEAKS)
def test_redact_text_masks_assignments(text, secret):
    assert secret not in rn.redact_text(text)


@pytest.mark.parametrize("text,secret", LEAKS)
def test_note_never_stores_secret(lessons, text, secret):
    out = note(lessons, f"found creds in config: {text} (rotate them)")
    assert "NEWLY LEARNED" in out and "masked" in out
    assert secret not in out
    assert secret not in stored(lessons)


@pytest.mark.parametrize("text,secret", LEAKS[:3])
def test_conclusion_and_topic_masked(lessons, text, secret):
    out = note(lessons, "the deploy failed on auth", kind="conclusion", topic=text)
    assert secret not in out and secret not in stored(lessons)


@pytest.mark.parametrize("text,secret", LEAKS[:2])
def test_question_posted_masked(lessons, text, secret):
    out = note(lessons, f"is {text} still valid?", kind="question")
    assert "not stored" in out and secret not in out and secret not in stored(lessons)


@pytest.mark.parametrize("prose", [
    "The key: retries must be bounded.", "Use a token bucket for rate limits.",
    "The password field is validated server-side.", "keys are lower-case hex",
    "set the API key in env, not code", "the Authorization header was missing",
])
def test_plain_prose_untouched(prose):
    assert rn.redact_text(prose) == prose


def test_existing_token_shapes_still_masked():
    for s in ["Bearer\tabc.def", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "postgres://admin:pw@db/x",
              "sk-\u200bABCDEFGHIJKLMNOP1234"]:
        out = rn.redact_text(s)
        assert out != s and "abc.def" not in out and "pw@" not in out


def test_redactor_failure_never_fails_note(lessons, monkeypatch):
    from avrone import ctxspawn

    def boom(_s):
        raise RuntimeError("x")
    monkeypatch.setattr(ctxspawn, "redact", boom)
    out = rn.redact_text("ghp_abcdefghijklmnopqrstuvwxyz0123456789 ok")
    assert "ghp_" not in out and out.endswith(" ok")


def test_masking_growth_still_capped(lessons):
    text = ("password=x " * 400)[: rn.RESEARCH_NOTE_MAX_TEXT]
    out = note(lessons, text)
    assert out.startswith("ERROR: after masking") or "posted" in out
    assert "password=x " not in stored(lessons)


def test_lesson_recall_masks_previously_stored_secret(lessons):
    from avrone import bridge as bm
    from avrone import tools_lessonrecall as lr
    with bm.lessons_lock(lessons):
        bm._store(lessons).save(bm.LESSONS_NS, "l-0123456789abcdef", "old note password=hunter2xyz",
                               run_id="old", task="t")
    out = run(lr.t_lesson_recall(S(core=S(settings=S(data_dir=lessons))), {}))
    assert "l-0123456789abcdef" in out and "hunter2xyz" not in out


def test_same_text_dedupes_after_masking(lessons):
    a = note(lessons, "creds: password=one11111")
    b = note(lessons, "creds: password=two22222")
    assert "NEWLY LEARNED" in a and "already known" in b  # both mask to the same lesson


# ---- pack_list order -------------------------------------------------------------------------------

def test_pack_list_sorted_by_name(tmp_path, monkeypatch):
    from avrone import agentpack
    from avrone import tools_packpeer as pp
    d = agentpack.pack_dir(tmp_path)
    d.mkdir(parents=True)
    names = ["zeta.tar.gz", "alpha.tar.gz", "mid.tar.gz", "beta.tar.gz"]
    for n in names:
        (d / n).write_bytes(b"not a pack")
    out = run(pp.t_pack_list(S(core=S(settings=S(data_dir=tmp_path))), {}))
    rows = [ln.split(" ")[1] for ln in out.split("\n") if ln.startswith("- ")]
    assert rows == sorted(names)


def test_pack_list_first_page_stable(tmp_path, monkeypatch):
    from avrone import agentpack
    from avrone import tools_packpeer as pp
    monkeypatch.setattr(pp, "MAX_LIST", 3)
    d = agentpack.pack_dir(tmp_path)
    d.mkdir(parents=True)
    for i in (9, 3, 7, 1, 5):
        (d / f"p{i}.tar.gz").write_bytes(b"x")
    c = S(core=S(settings=S(data_dir=tmp_path)))
    outs = {run(pp.t_pack_list(c, {})) for _ in range(3)}
    assert len(outs) == 1
    rows = [ln.split(" ")[1] for ln in outs.pop().split("\n") if ln.startswith("- ")]
    assert rows == ["p1.tar.gz", "p3.tar.gz", "p5.tar.gz"]
