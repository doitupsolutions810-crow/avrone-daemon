"""Round 76: research_note / bridge data_dir and argument hardening."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from avrone import bridge as bm
from avrone import tools_researchnote as rn


@pytest.fixture(autouse=True)
def _lessons_on_in_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def ctx_for(dd, run_id="r76"):
    return NS(core=NS(settings=NS(data_dir=dd)), run_id=run_id, name="Agent1")


def run(args, ctx):
    return asyncio.run(rn.t_research_note(ctx, args))


@pytest.mark.parametrize("args", [None, [], ["text"], "a real note", 7, True])
def test_non_dict_args(args):
    assert run(args, ctx_for(None)) == "ERROR: text is required (a non-empty string)"


@pytest.mark.parametrize("dd", ["", " ", "\t\n", "   \u3000".strip(" ") or "\u3000"])
def test_blank_data_dir_writes_nothing_in_cwd(dd):
    out = run({"text": "a finding about blank dirs"}, ctx_for(dd))
    assert "posted finding" in out
    assert "lessons: no data_dir on this node" in out
    assert os.listdir(".") == []


@pytest.mark.parametrize("dd", [5, 1.5, {"a": 1}, ["x"], True, b"bytes"])
def test_non_path_data_dir_is_none(dd):
    out = run({"text": "a finding with odd data_dir"}, ctx_for(dd))
    assert "lessons: no data_dir on this node" in out
    assert os.listdir(".") == []


def test_real_data_dir_still_learns(tmp_path):
    d = tmp_path / "data"
    out = run({"text": "real data dir finding"}, ctx_for(str(d), "r76real"))
    assert "NEWLY LEARNED" in out
    assert (d / "memory.jsonl").exists() and (d / "bridge").is_dir()


def test_pathlike_data_dir_learns(tmp_path):
    d = tmp_path / "pl"
    assert "NEWLY LEARNED" in run({"text": "pathlike finding"}, ctx_for(d, "r76pl"))


@pytest.mark.parametrize("ctx", [NS(), NS(core=None), NS(run_id="x")])
def test_ctx_without_core(ctx):
    assert run({"text": "orphan note"}, ctx) == "ERROR: no agent core on this context (nothing posted)"


def test_bridge_open_oserror_is_reported(tmp_path):
    f = tmp_path / "file"
    f.write_text("not a dir")
    out = run({"text": "note"}, ctx_for(str(f), "r76os"))
    assert out.startswith("ERROR: could not open the run bridge (")
    assert str(tmp_path) not in out


@pytest.mark.parametrize("dd,want", [
    ("", None), ("  ", None), (None, None), (0, None), ({}, None), (b"x", None),
    ("/data", "/data"), ("rel", "rel"),
])
def test_usable_data_dir(dd, want):
    assert bm.usable_data_dir(dd) == want


def test_usable_data_dir_pathlike():
    p = Path("/data")
    assert bm.usable_data_dir(p) is p


class BadPath(os.PathLike):
    def __fspath__(self):
        raise TypeError("no")


class BlankPath(os.PathLike):
    def __fspath__(self):
        return "  "


@pytest.mark.parametrize("p", [BadPath(), BlankPath()])
def test_usable_data_dir_bad_pathlike(p):
    assert bm.usable_data_dir(p) is None


@pytest.mark.parametrize("dd", ["", "  ", 3])
def test_bridge_blank_is_in_memory(dd):
    b = bm.Bridge("r76mem", dd)
    assert b.path is None
    b.post("a", "finding", "hello")
    assert len(b) == 1 and os.listdir(".") == []


@pytest.mark.parametrize("dd", ["", " "])
def test_load_posts_blank_is_none(dd):
    os.makedirs("bridge")
    Path("bridge", "r76lp.jsonl").write_text("")
    assert bm.load_posts(dd, "r76lp") is None


@pytest.mark.parametrize("dd", ["", " "])
def test_relevant_lessons_blank_reads_nothing(dd, tmp_path):
    real = tmp_path / "real"
    run({"text": "docker networking needs a bridge network", "kind": "conclusion"}, ctx_for(str(real), "r76rl"))
    os.symlink(real / "memory.jsonl", "memory.jsonl")
    assert bm.relevant_lessons(str(real), "docker networking")
    assert bm.relevant_lessons(dd, "docker networking") == []
    assert bm.lessons_block(dd, "docker networking") == ""


@pytest.mark.parametrize("dd", ["", " "])
def test_distill_blank_learns_nothing(dd):
    b = bm.Bridge("r76d", None)
    b.post("a", "conclusion", "a conclusion worth keeping")
    assert bm.distill_lessons(dd, b, "task") == []
    assert os.listdir(".") == []
