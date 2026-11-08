"""Round 15 coverage: run_logs stat failure, evolve worktree symlink loop, privileged target not allowed."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import evolve
from avrone import fleet as fleet_mod
from avrone import tools_limits as tl


def test_usage_skips_unstatable(tmp_path, monkeypatch):
    (tmp_path / "a.jsonl").write_text("x")
    (tmp_path / "b.jsonl").write_text("yy")
    import os as _os
    real = _os.scandir

    class _E:  # round 103: usage() lstats through scandir entries now
        def __init__(self, e):
            self._e, self.name, self.path = e, e.name, e.path

        def stat(self, follow_symlinks=True):
            if self.name == "a.jsonl":
                raise OSError("eio")
            return self._e.stat(follow_symlinks=follow_symlinks)

    class _It:
        def __init__(self, d):
            self._it = real(d)

        def __enter__(self):
            return self

        def __iter__(self):
            return (_E(e) for e in self._it)

        def __exit__(self, *a):
            self._it.close()
    monkeypatch.setattr(tl.os, "scandir", _It)
    u = tl.usage(tmp_path)
    assert u["count"] == 1 and u["bytes"] == 2


def test_proposal_worktree_symlink_loop(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    ctx = SimpleNamespace(core=core, run_id="r", name="E", depth=0)
    d = evolve._data(ctx)
    loop = d / "wt-loop"
    loop.symlink_to(loop)  # resolve() -> RuntimeError / OSError (symlink loop)
    (d / "p.json").write_text(json.dumps({"pid": "p", "worktree": str(loop)}))
    mp, meta, wt, err = evolve._proposal(ctx, "p")
    assert wt is None and "no worktree" in err and meta["pid"] == "p"


def test_privileged_target_not_in_allow(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setenv("AVRONE_FLEET_PRIVILEGED", "allow")
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "other-node")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    c = MagicMock()
    c.active = {}
    fm = fleet_mod.FleetManager(c, tmp_path)
    assert fm.privileged
    with pytest.raises(fleet_mod.GatewayRefused) as e:
        fm.check_outbound("run", "priv:shell", 0, "edge-1", privileged=True)
    assert e.value.code == "target_not_allowed"
