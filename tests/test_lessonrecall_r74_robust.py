"""Round 74: lesson_recall args, blank data_dir, non-text fields, combining-mark floods."""
from __future__ import annotations

import asyncio
import json
import os
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from avrone import tools_lessonrecall as lr


@pytest.fixture(autouse=True)
def _lessons_on(monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")


def ctx(dd):
    return NS(core=NS(settings=NS(data_dir=dd)))


def run(args, dd):
    return asyncio.run(lr.t_lesson_recall(ctx(dd), args))


def store(d: Path, recs):
    now = time.time()
    with open(d / "memory.jsonl", "w") as f:
        for i, r in enumerate(recs):
            f.write(json.dumps({"ns": "lessons", "ts": now - i, **r}) + "\n")
    return d


@pytest.fixture
def lessons(tmp_path):
    return store(tmp_path, [{"key": "k1", "value": "retry docker pulls"},
                            {"key": "k2", "value": "cache docker layers"}])


# ---------------------------------------------------------------- non-dict args
@pytest.mark.parametrize("args", [None, [], ["topic"], "docker", 5, True])
def test_non_dict_args_list_newest(lessons, args):
    out = run(args, lessons)
    assert out.startswith("lesson_recall: 2 of 2 stored lessons") and "k1" in out


# ---------------------------------------------------------------- blank data_dir is not the cwd
@pytest.mark.parametrize("dd", ["", " ", "\t\n"])
def test_blank_data_dir_never_reads_cwd(lessons, dd, monkeypatch):
    monkeypatch.chdir(lessons)  # a memory.jsonl sits in the cwd
    out = run({}, dd)
    assert "no data_dir" in out and "k1" not in out


def test_none_data_dir(lessons):
    assert "no data_dir" in run({}, None)


def test_path_data_dir_still_works(lessons):
    assert "k1" in run({}, str(lessons)) and "k1" in run({}, Path(lessons))


# ---------------------------------------------------------------- non-text task / key
@pytest.mark.parametrize("task", [{"t": 1}, ["a", "b"], 5, True, None])
def test_non_text_task_not_shown_as_repr(tmp_path, task):
    store(tmp_path, [{"key": "k", "value": "docker lesson", "task": task}])
    out = run({"topic": "docker"}, tmp_path)
    line = out.split("\n")[1]
    assert line.endswith(": docker lesson") and "{" not in line and "[" not in line.split("]", 1)[1]
    assert "(" not in line


def test_text_task_shown(tmp_path):
    store(tmp_path, [{"key": "k", "value": "docker lesson", "task": "build image"}])
    assert "(build image)" in run({"topic": "docker"}, tmp_path)


# ---------------------------------------------------------------- combining-mark floods
def _max_run(s):
    best = run_ = 0
    for ch in s:
        if unicodedata.category(ch) in ("Mn", "Me"):
            run_ += 1
            best = max(best, run_)
        else:
            run_ = 0
    return best


@pytest.mark.parametrize("field", ["value", "key", "task"])
def test_zalgo_flood_capped(tmp_path, field):
    flood = "docker" + "\u0301\u0302\u0303\u20dd" * (10 if field == "key" else 200)  # keys are length-capped
    rec = {"key": "k", "value": "docker lesson"}
    rec[field] = flood
    store(tmp_path, [rec])
    out = run({}, tmp_path)
    assert _max_run(out) <= lr.MAX_MARKS
    assert "docker" in out


@pytest.mark.parametrize("text", [
    "cafe\u0301 notes", "Vie\u0323\u0302t docker", "\u05e9\u05c1\u05b8\u0591\u05dc\u05d5\u05b9\u05dd docker",
    "\u0915\u094d\u0937 docker", "na\u00efve docker",
])
def test_real_accents_untouched(tmp_path, text):
    store(tmp_path, [{"key": "k", "value": text}])
    assert text in run({}, tmp_path)


def test_calm_unit():
    assert lr._calm("a" + "\u0301" * 10 + "b" + "\u0301" * 3) == "a" + "\u0301" * lr.MAX_MARKS + "b" + "\u0301" * 3
    assert lr._calm("") == ""
    assert lr._calm("\u0301" * 9) == "\u0301" * lr.MAX_MARKS


def test_flood_does_not_eat_digest(tmp_path):
    store(tmp_path, [{"key": "k", "value": "docker " + "\u0301" * 5000 + " tail-word-kept"}])
    assert "tail-word-kept" in run({}, tmp_path)


# ---------------------------------------------------------------- unchanged behaviour
def test_topic_non_text_still_refused(lessons):
    assert run({"topic": 5}, lessons) == "ERROR: topic must be text"


def test_limit_bool_refused(lessons):
    assert run({"limit": True}, lessons).startswith("ERROR: limit")


def test_dir_store_is_error_not_crash(tmp_path):
    (tmp_path / "memory.jsonl").mkdir()
    assert run({}, tmp_path).startswith("ERROR: could not read lessons")
