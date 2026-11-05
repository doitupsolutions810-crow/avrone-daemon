"""Round 66: evolve worktree creation raced with itself, and .git case variants slipped past _in_repo.

- _ensure_worktree(ctx, pid) checked `wt.exists()` and then cloned into it. Two concurrent calls for
  one proposal (an evolve_loop step plus evolve_propose, or two agents) both cloned into the same dir:
  the second clone failed, fell back to the plain copy, and that copy's rmtree deleted the first
  call's clone under it. A crash mid-clone left a half-made dir that `wt.exists()` then returned
  forever (no branch, missing files). Now the clone is built in a private temp dir and renamed into
  place (atomic), under a per-proposal lock; a loser of the rename discards its copy.
- _in_repo refused `.git/...` but not `.GIT/config` or `.git./config`, which are the same dir on a
  case-insensitive (macOS default) or Windows filesystem; `.git/config` can set core.fsmonitor and
  make git run commands. Any first component that folds to `.git` (or a Windows short name GIT~n)
  is refused now.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_round57_mcp_http_redirect_edges import _evolve_core  # noqa: E402

from avrone import evolve  # noqa: E402
from avrone.trinity import AgentContext  # noqa: E402


def _git_repo(repo):
    subprocess.run("git init -q && git add -A && git -c user.email=a@a -c user.name=t commit -qm base",
                   shell=True, cwd=repo, check=True, capture_output=True)


def _branch(wt):
    return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt, capture_output=True,
                          text=True).stdout.strip()


@pytest.mark.parametrize("git", [True, False], ids=["clone", "plain-copy"])
def test_concurrent_ensure_worktree_builds_one_good_clone(tmp_path, monkeypatch, git):
    core, repo = _evolve_core(tmp_path, monkeypatch)
    for f in (repo / "workspace" / "agents" / "a" / "pipe", repo / "pkg" / "stray.fifo"):
        f.unlink()
    for i in range(300):
        (repo / "pkg" / f"f{i}.py").write_text("x = 1\n" * 20)
    if git:
        _git_repo(repo)
    ctx = AgentContext(core=core, run_id="e", name="Ev", depth=0)

    async def go():
        return await asyncio.gather(*(evolve._ensure_worktree(ctx, "p1") for _ in range(4)))
    outs = asyncio.run(go())
    wt = outs[0][0]
    assert all(o == outs[0] for o in outs)
    assert _branch(wt) == "evolve/p1"
    assert (wt / "pkg" / "m.py").read_text() == "def f():\n    return 1\n"
    assert len(list((wt / "pkg").glob("f*.py"))) == 300
    assert not [p for p in wt.parent.iterdir() if p.name.startswith(".tmp-wt-")]  # no temp left over


def test_failed_build_leaves_no_half_made_worktree(tmp_path, monkeypatch):
    core, repo = _evolve_core(tmp_path, monkeypatch)
    _git_repo(repo)
    ctx = AgentContext(core=core, run_id="e", name="Ev", depth=0)
    real = evolve._sh
    calls = {"n": 0}

    async def boom(c, cmd, cwd, timeout=600):
        calls["n"] += 1
        if cmd.startswith("git checkout"):
            raise RuntimeError("runner died mid-build")
        return await real(c, cmd, cwd, timeout)
    monkeypatch.setattr(evolve, "_sh", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(evolve._ensure_worktree(ctx, "p2"))
    data = evolve._data(ctx)
    assert not (data / "wt-p2").exists()
    assert not [p for p in data.iterdir() if p.name.startswith(".tmp-wt-")]
    monkeypatch.setattr(evolve, "_sh", real)
    wt, _ = asyncio.run(evolve._ensure_worktree(ctx, "p2"))  # the next try builds it properly
    assert _branch(wt) == "evolve/p2"


@pytest.mark.parametrize("rel", [".GIT/config", ".Git/hooks/pre-commit", ".git./config", ".git /config",
                                 "GIT~1/config", "git~2/config", "./.GiT/config"])
def test_in_repo_refuses_git_dir_aliases(tmp_path, rel):
    wt = tmp_path / "wt-x"
    (wt / ".git").mkdir(parents=True)
    with pytest.raises(ValueError):
        evolve._in_repo(wt, rel)


@pytest.mark.parametrize("rel", [".gitignore", ".github/workflows/ci.yml", "src/.git_helpers.py", "gitx/a"])
def test_in_repo_still_allows_lookalikes(tmp_path, rel):
    wt = tmp_path / "wt-x"
    wt.mkdir()
    assert evolve._in_repo(wt, rel) == (wt / rel).resolve()
