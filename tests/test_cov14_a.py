"""Round 14 coverage: secret_scan internals - unreadable files, env lookup failure, fix_runs skips
(non-jsonl, symlink, unchanged, unreadable) and tmp cleanup when the replace fails."""
import os
from pathlib import Path

import pytest

from avrone import ctxspawn
from avrone import tools_secrets as ts


def test_kinds_env_lookup_failure(monkeypatch):
    monkeypatch.setattr(ctxspawn, "_env_secret_values", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert ts._kinds("Authorization: Bearer abcdefghijklmn") and ts._kinds("plain text") == []


def test_scan_unreadable_file(tmp_path, monkeypatch):
    (tmp_path / "a.log").write_text("password=abcdefgh1\n")
    real_open = Path.open

    def bad_open(self, *a, **k):
        if self.name == "a.log":
            raise PermissionError("no")
        return real_open(self, *a, **k)
    monkeypatch.setattr(Path, "open", bad_open)
    import avrone.safeio as _sio  # round 90: scan opens through safeio.open_read
    real_read = _sio.open_read
    monkeypatch.setattr(_sio, "open_read", lambda p, *a, **k: (_ for _ in ()).throw(PermissionError("no"))
                        if Path(p).name == "a.log" else real_read(p, *a, **k))
    assert ts.scan(tmp_path) == {"files": 0, "hits": [], "partial": [], "truncated": False}  # round 157: + partial


def test_fix_runs_skips(tmp_path):
    (tmp_path / "notes.txt").write_text("password=abcdefgh1\n")
    (tmp_path / "clean.jsonl").write_text('{"a": 1}\n')
    (tmp_path / "real.jsonl").write_text("password=abcdefgh1\n")
    (tmp_path / "link.jsonl").symlink_to(tmp_path / "real.jsonl")
    n = ts.fix_runs(tmp_path, {"notes.txt", "clean.jsonl", "link.jsonl", "missing.jsonl"})
    assert n == 0 and "abcdefgh1" in (tmp_path / "real.jsonl").read_text()


def test_fix_runs_unreadable_and_replace_failure(tmp_path, monkeypatch):
    p = tmp_path / "r.jsonl"
    p.write_text("password=abcdefgh1\n")
    real_read = Path.read_text

    def bad_read(self, *a, **k):
        if self.name == "r.jsonl":
            raise OSError("io")
        return real_read(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", bad_read)
    import avrone.safeio as _sio  # round 91: fix_runs reads through safeio.read_capped
    real_capped = _sio.read_capped

    def bad_capped(path, *a, **k):
        if Path(path).name == "r.jsonl":
            raise OSError("io")
        return real_capped(path, *a, **k)
    monkeypatch.setattr(_sio, "read_capped", bad_capped)
    assert ts.fix_runs(tmp_path, {"r.jsonl"}) == 0
    monkeypatch.setattr(Path, "read_text", real_read)
    monkeypatch.setattr(_sio, "read_capped", real_capped)

    def boom(src, dst):
        raise KeyboardInterrupt
    monkeypatch.setattr(ts.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        ts.fix_runs(tmp_path, {"r.jsonl"})
    assert sorted(x.name for x in tmp_path.iterdir()) == ["r.jsonl"]  # tmp removed


def test_roots_workspace_from_runner(tmp_path):
    from types import SimpleNamespace
    ctx = SimpleNamespace(core=SimpleNamespace(runner=SimpleNamespace(workspace=str(tmp_path)), settings=None))
    assert ts._roots(ctx, "workspace") == tmp_path and ts._roots(ctx, "runs") is None
    assert ts._roots(SimpleNamespace(core=None), "workspace") is None
