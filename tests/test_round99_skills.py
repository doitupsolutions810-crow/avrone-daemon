"""Round 99: skill listing and loading under hostile or large skill directories.

v0.91: skill_load read every skill (up to 500 x 256 KB) to find one; skill_list read each whole
file for a one-line description and sorted a glob of the whole directory; _read_skill checked
is_file() then opened the path, so a FIFO swapped in between hung the tool.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from avrone import agentkit


@pytest.fixture
def skills(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(d))
    return d


def _ctx(tmp):
    return S(core=S(settings=S(data_dir=tmp / "d"), codex=S(emit=lambda *a, **k: None)), run_id="r", name="A")


def test_skill_load_reads_only_the_named_skill(tmp_path, skills, monkeypatch):
    for i in range(30):
        (skills / f"s{i}.md").write_text(f"# skill {i}\nbody {i}")
    calls = []
    real = agentkit._read_skill

    def spy(f, *a):
        calls.append(Path(f).name)
        return real(f, *a)
    monkeypatch.setattr(agentkit, "_read_skill", spy)
    out = asyncio.run(agentkit.t_skill_load(_ctx(tmp_path), {"name": "s7"}))
    assert "body 7" in out
    assert set(calls) <= {"s7.md", "SKILL.md"} and len(calls) <= 3


def test_dir_skill_still_wins_over_flat(tmp_path, skills):
    (skills / "a").mkdir()
    (skills / "a" / "SKILL.md").write_text("# dir version")
    (skills / "a.md").write_text("# flat version")
    out = asyncio.run(agentkit.t_skill_load(_ctx(tmp_path), {"name": "a"}))
    assert "dir version" in out


def test_symlink_outside_dir_not_loadable(tmp_path, skills):
    outside = tmp_path / "secret.md"
    outside.write_text("# secret")
    os.symlink(outside, skills / "leak.md")
    out = asyncio.run(agentkit.t_skill_load(_ctx(tmp_path), {"name": "leak"}))
    assert out.startswith("ERROR: no skill") and "secret" not in out.split("Available")[0]


def test_list_reads_only_the_head(tmp_path, skills, monkeypatch):
    big = "---\ndescription: big one\n---\n" + "x" * 200_000
    (skills / "big.md").write_text(big)
    sizes = []
    from avrone import safeio
    real = safeio.open_read

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()

        def fileno(self):
            return self.fh.fileno()

        def read(self, n=-1):
            b = self.fh.read(n)
            sizes.append(len(b))
            return b
    monkeypatch.setattr(safeio, "open_read", lambda p, follow=False: Spy(real(p, follow)))
    out = asyncio.run(agentkit.t_skill_list(_ctx(tmp_path), {}))
    assert "big: big one" in out and max(sizes) <= agentkit.SKILL_HEAD_BYTES


def test_description_past_the_head_still_found(tmp_path, skills):
    (skills / "late.md").write_text("\n" * (agentkit.SKILL_HEAD_BYTES + 10) + "# Late title\n")
    (skills / "fm.md").write_text("---\n" + "#c\n" * 6000 + "description: deep fm\n---\nbody")
    out = asyncio.run(agentkit.t_skill_list(_ctx(tmp_path), {}))
    assert "late: Late title" in out and "fm: deep fm" in out


def test_listing_scan_is_bounded(tmp_path, skills, monkeypatch):
    for i in range(50):
        (skills / f"junk{i}.txt").write_text("")
    monkeypatch.setattr(agentkit, "SKILLS_SCAN_MAX", 10)
    (skills / "zz.md").write_text("# z")
    agentkit._skills(_ctx(tmp_path))  # no exception, bounded work
    assert len(agentkit._skill_files(skills)) <= 10


def test_fifo_swapped_in_does_not_hang(tmp_path, skills, monkeypatch):
    fifo = skills / "f.md"
    os.mkfifo(fifo)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("r", agentkit._read_skill(fifo)), daemon=True)
    th.start()
    th.join(3)
    if th.is_alive():
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        th.join(3)
        pytest.fail("_read_skill hung on a FIFO")
    assert res["r"] is None
