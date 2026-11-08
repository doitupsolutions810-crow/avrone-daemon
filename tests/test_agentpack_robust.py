"""Round 33: agent migration packs (avrone/agentpack.py). Covers names that collide on a
case-insensitive Mac filesystem, control characters, path length limits, executable bits surviving
migration, mtimes the target filesystem can't hold, and files that vanish or can't be read while
packing. Tamper and traversal refusals must still hold."""
import gzip
import hashlib
import io
import json
import os
import tarfile
import unicodedata
from pathlib import Path

import pytest

from avrone import agentfs, agentpack
from avrone.agentpack import PackError, fold_path


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_NODE_ID"):
        monkeypatch.delenv(k, raising=False)


def home(ws, aid="builder"):
    d = agentfs.agents_root(ws) / aid
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha(b):
    return hashlib.sha256(b).hexdigest()


def craft(files, aid="builder", origin="edge", home_="edge", created=2_000_000_000, extra=None):
    """A hand-built bundle: files = {rel: bytes} or {rel: (bytes, entry-overrides)}."""
    mem = b"[]"
    entries, blobs = [], []
    for rel, v in files.items():
        data, over = (v if isinstance(v, tuple) else (v, {}))
        e = {"path": rel, "size": len(data), "sha256": sha(data), "mtime": 1_700_000_000, **over}
        entries.append(e)
        blobs.append(("home/" + rel, data))
    man = {"format": agentpack.FORMAT, "agent_id": aid, "origin": origin, "home": home_, "created": created,
           "total_bytes": sum(len(d) for _, d in blobs), "files": entries,
           "memory": {"namespaces": [], "entries": 0, "sha256": sha(mem)}, **(extra or {})}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in [(agentpack.MANIFEST, json.dumps(man).encode()), (agentpack.MEMORY, mem)] + blobs:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def imp(tmp_path, bundle, node="router"):
    return agentpack.import_pack(tmp_path / "ws2", tmp_path / "data2", bundle, node)


def installed(tmp_path, aid="builder"):
    return agentfs.agents_root(tmp_path / "ws2") / aid


# ------------------------------------------------------------------ fold_path
@pytest.mark.parametrize("a,b", [("Notes.md", "notes.md"), ("README", "readme"),
                                 (unicodedata.normalize("NFD", "café.txt"), "café.txt"), ("STRASSE", "strasse")])
def test_fold_path_equal(a, b):
    assert fold_path(a) == fold_path(b)


def test_fold_path_distinct():
    assert fold_path("a.txt") != fold_path("b.txt")


# ------------------------------------------------------------------ case / normalization collisions
@pytest.mark.parametrize("a,b", [("Notes.md", "notes.md"), ("dir/A.txt", "dir/a.txt"), ("X/f", "x/f"),
                                 (unicodedata.normalize("NFD", "café"), unicodedata.normalize("NFC", "café"))])
def test_case_collision_refused(tmp_path, a, b):
    with pytest.raises(PackError, match="case-insensitive"):
        imp(tmp_path, craft({a: b"1", b: b"2"}))
    assert not installed(tmp_path).exists()


def test_file_vs_dir_conflict_any_case(tmp_path):
    with pytest.raises(PackError, match="under a file"):
        imp(tmp_path, craft({"Tools": b"x", "tools/run.sh": b"y"}))


def test_file_vs_dir_conflict_same_case_still_refused(tmp_path):
    with pytest.raises(PackError, match="under a file"):
        imp(tmp_path, craft({"a": b"x", "a/b": b"y"}))


def test_distinct_names_still_import(tmp_path):
    res = imp(tmp_path, craft({"Notes.md": b"1", "notes2.md": b"2", "dir/a.txt": b"3", "Dir2/a.txt": b"4"}))
    assert res["files"] == 4
    assert (installed(tmp_path) / "Notes.md").read_bytes() == b"1"


def test_export_with_case_twins_fails_import_clearly(tmp_path):
    d = home(tmp_path / "ws")
    (d / "Plan.md").write_text("one")
    (d / "plan.md").write_text("two")
    bundle, _ = agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")
    with pytest.raises(PackError, match="rename one on the sending node"):
        imp(tmp_path, bundle)


# ------------------------------------------------------------------ control chars / length
@pytest.mark.parametrize("name", ["a\nb.txt", "evil\x1b[2J.txt", "tab\there", "del\x7f", "dir\r/x"])
def test_control_characters_refused(tmp_path, name):
    with pytest.raises(PackError, match="control character"):
        imp(tmp_path, craft({name: b"x"}))


def test_component_too_long(tmp_path):
    with pytest.raises(PackError, match="too long"):
        imp(tmp_path, craft({"a" * 256: b"x"}))


def test_component_at_limit_ok(tmp_path):
    assert imp(tmp_path, craft({"a" * 255: b"x"}))["files"] == 1


def test_multibyte_component_counts_bytes(tmp_path):
    with pytest.raises(PackError, match="too long"):
        imp(tmp_path, craft({"é" * 128: b"x"}))  # 256 bytes, 128 chars


def test_whole_path_too_long(tmp_path):
    rel = "/".join(["d" * 100] * 11)
    with pytest.raises(PackError, match="too long"):
        imp(tmp_path, craft({rel: b"x"}))


def test_unicode_name_ok(tmp_path):
    assert imp(tmp_path, craft({"notes/日本語.md": b"x"}))["files"] == 1


def test_traversal_still_refused(tmp_path):
    with pytest.raises(PackError):
        imp(tmp_path, craft({"../escape": b"x"}))


# ------------------------------------------------------------------ executable bits
def test_exec_bit_survives_round_trip(tmp_path):
    d = home(tmp_path / "ws")
    (d / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(d / "run.sh", 0o755)
    (d / "notes.md").write_text("plain")
    bundle, man = agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")
    by = {e["path"]: e for e in man["files"]}
    assert by["run.sh"]["mode"] == 0o755 and "mode" not in by["notes.md"]
    imp(tmp_path, bundle)
    assert os.stat(installed(tmp_path) / "run.sh").st_mode & 0o111
    assert not os.stat(installed(tmp_path) / "notes.md").st_mode & 0o111


def test_owner_only_exec_is_exported_as_755(tmp_path):
    d = home(tmp_path / "ws")
    (d / "t").write_text("x")
    os.chmod(d / "t", 0o700)
    _, man = agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")
    assert man["files"][0]["mode"] == 0o755


@pytest.mark.parametrize("bad", [0o4755, 0o777, 0o2755, True, "755", -1, 0o600])
def test_bad_mode_refused(tmp_path, bad):
    with pytest.raises(PackError, match="bad mode"):
        imp(tmp_path, craft({"x": (b"x", {"mode": bad})}))


def test_mode_644_accepted(tmp_path):
    imp(tmp_path, craft({"x": (b"x", {"mode": 0o644})}))
    assert not os.stat(installed(tmp_path) / "x").st_mode & 0o111


def test_old_packs_without_mode_still_import(tmp_path):
    assert imp(tmp_path, craft({"x": b"x"}))["files"] == 1


# ------------------------------------------------------------------ mtimes
def test_negative_mtime_clamped(tmp_path):
    imp(tmp_path, craft({"x": (b"x", {"mtime": -(2**39)})}))
    assert os.stat(installed(tmp_path) / "x").st_mtime >= 0


def test_far_future_mtime_clamped(tmp_path):
    imp(tmp_path, craft({"x": (b"x", {"mtime": 2**40 - 1})}))
    assert os.stat(installed(tmp_path) / "x").st_mtime <= agentpack.MTIME_MAX


@pytest.mark.parametrize("exc", [OSError(22, "Invalid argument"), OverflowError("too big"), ValueError("x")])
def test_utime_failure_is_not_fatal(tmp_path, monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(agentpack.os, "utime", boom)
    res = imp(tmp_path, craft({"x": b"data"}))
    assert res["files"] == 1 and (installed(tmp_path) / "x").read_bytes() == b"data"


# ------------------------------------------------------------------ export while files change
def test_file_vanishing_during_walk_is_skipped(tmp_path, monkeypatch):
    d = home(tmp_path / "ws")
    (d / "keep").write_text("k")
    (d / "gone").write_text("g")
    real = os.lstat

    def lstat(p, *a, **k):
        if str(p).endswith("gone"):
            raise FileNotFoundError(p)
        return real(p, *a, **k)
    monkeypatch.setattr(agentpack.os, "lstat", lstat)
    _, man = agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")
    assert [e["path"] for e in man["files"]] == ["keep"]


def test_file_vanishing_before_read_names_it(tmp_path, monkeypatch):
    d = home(tmp_path / "ws")
    (d / "log.txt").write_text("x")
    from avrone import safeio
    real = safeio.read_capped  # round 107: agentpack reads through read_capped

    def rb(self, *a, **k):
        self = Path(self)
        if self.name == "log.txt":
            raise FileNotFoundError(str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(safeio, "read_capped", rb)
    with pytest.raises(PackError, match="log.txt vanished while packing"):
        agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")


def test_unreadable_file_names_it(tmp_path, monkeypatch):
    d = home(tmp_path / "ws")
    (d / "secret").write_text("x")
    from avrone import safeio
    real = safeio.read_capped  # round 107: agentpack reads through read_capped

    def rb(self, *a, **k):
        self = Path(self)
        if self.name == "secret":
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)
    monkeypatch.setattr(safeio, "read_capped", rb)
    with pytest.raises(PackError, match="cannot read secret: PermissionError"):
        agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")


# ------------------------------------------------------------------ tamper checks unchanged
def test_hash_mismatch_still_refused(tmp_path):
    with pytest.raises(PackError, match="tampered"):
        imp(tmp_path, craft({"x": (b"x", {"sha256": "0" * 64})}))


def test_round_trip_content_identical(tmp_path):
    d = home(tmp_path / "ws")
    (d / "sub").mkdir()
    (d / "sub" / "a.bin").write_bytes(bytes(range(256)))
    bundle, _ = agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")
    imp(tmp_path, bundle)
    assert (installed(tmp_path) / "sub" / "a.bin").read_bytes() == bytes(range(256))
