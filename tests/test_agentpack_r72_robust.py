"""Round 72: agentpack restore/export edges.

- manifest "memory" that is not an object was an AttributeError (500), not a refusal
- manifest "total_bytes" missing was a KeyError after the files were installed; a non-int was echoed back
- C1 controls, U+2028/2029 and bidi overrides passed the path check (now avrone.textsafe)
- export produced packs no node could import (bad UTF-8, backslash, control/bidi chars, case twins)
- a regular file or FIFO at agents/<id> was swapped aside and leaked as a stray .old-* entry
- install OSErrors carried the host's absolute path to the sending peer
- a non-bytes bundle was a TypeError
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from avrone import agentfs
from avrone import agentpack as ap
from avrone.agentpack import PackError


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build(files=None, mutate=None, mem=b"[]"):
    files = {"a.txt": b"hi"} if files is None else files
    man = {"format": ap.FORMAT, "agent_id": "ag1", "origin": "n1", "home": "n1", "created": 100,
           "total_bytes": sum(len(v) for v in files.values()),
           "files": [{"path": k, "size": len(v), "sha256": _sha(v), "mtime": 50} for k, v in files.items()],
           "memory": {"namespaces": [], "entries": 0, "sha256": _sha(mem)}}
    if mutate:
        mutate(man)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        items = [("manifest.json", json.dumps(man).encode()), ("memory.json", mem)]
        items += [("home/" + k, v) for k, v in files.items()]
        for n, d in items:
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            tf.addfile(ti, io.BytesIO(d))
    return gzip.compress(raw.getvalue())


def env(tmp_path: Path):
    ws = tmp_path / "ws"
    dd = tmp_path / "data"
    ws.mkdir(exist_ok=True)
    dd.mkdir(exist_ok=True)
    return ws, dd


def imp(tmp_path, bundle, **kw):
    ws, dd = env(tmp_path)
    return ap.import_pack(ws, dd, bundle, "n2", **kw)


# ---------------------------------------------------------------- manifest memory / total_bytes
@pytest.mark.parametrize("val", [[1], "abc", 5, True, None])
def test_manifest_memory_not_object_refused(tmp_path, val):
    with pytest.raises(PackError, match="bad memory in manifest"):
        imp(tmp_path, build(mutate=lambda m: m.update(memory=val)))


@pytest.mark.parametrize("val", [[1], "abc", 5])
def test_read_pack_memory_not_object_refused(val):
    with pytest.raises(PackError, match="bad memory"):
        ap.read_pack(build(mutate=lambda m: m.update(memory=val)))


def test_missing_total_bytes_imports_and_reports_real_bytes(tmp_path):
    r = imp(tmp_path, build({"a.txt": b"hello"}, mutate=lambda m: m.pop("total_bytes")))
    assert r["bytes"] == 5 and r["files"] == 1


@pytest.mark.parametrize("val", ["x", "5", 1.5, True, False, -1, [], {}])
def test_bad_total_bytes_refused(tmp_path, val):
    with pytest.raises(PackError, match="bad total_bytes"):
        imp(tmp_path, build(mutate=lambda m: m.update(total_bytes=val)))


def test_lying_total_bytes_reports_verified_count(tmp_path):
    r = imp(tmp_path, build({"a.txt": b"abc", "b.txt": b"defg"}, mutate=lambda m: m.update(total_bytes=10**9)))
    assert r["bytes"] == 7


def test_total_bytes_failure_installs_nothing(tmp_path):
    ws, dd = env(tmp_path)
    with pytest.raises(PackError):
        ap.import_pack(ws, dd, build(mutate=lambda m: m.update(total_bytes="x")), "n2")
    assert not (agentfs.agents_root(ws) / "ag1").exists()


# ---------------------------------------------------------------- path characters
@pytest.mark.parametrize("name", ["a\x85b", "a\x9bb", "x\u2028y", "x\u2029y", "invoice\u202etxt.exe",
                                  "a\u200eb", "a\u200fb", "a\u061cb", "a\u2066b", "a\u2069b", "a\x80b"])
def test_unsafe_unicode_path_refused(tmp_path, name):
    with pytest.raises(PackError, match="control character"):
        imp(tmp_path, build({name: b"x"}))


@pytest.mark.parametrize("name", ["dir/a\u202eb", "a\u2028/b"])
def test_unsafe_unicode_in_any_component(tmp_path, name):
    with pytest.raises(PackError, match="control character"):
        imp(tmp_path, build({name: b"x"}))


@pytest.mark.parametrize("name", ["café.md", "日本語/ノート.txt", "emoji-😀.txt", "a b.txt", "ünï/çødé"])
def test_ordinary_unicode_still_imports(tmp_path, name):
    r = imp(tmp_path, build({name: b"x"}))
    assert r["files"] == 1
    ws, _ = env(tmp_path)
    assert (agentfs.agents_root(ws) / "ag1" / name).read_bytes() == b"x"


def test_zwj_emoji_name_imports(tmp_path):
    name = "fam-\U0001F468\u200d\U0001F469.txt"  # ZWJ is not a format-control bidi mark
    assert imp(tmp_path, build({name: b"x"}))["files"] == 1


# ---------------------------------------------------------------- export refuses unimportable names
def _home(tmp_path):
    ws, dd = env(tmp_path)
    d = agentfs.agents_root(ws) / "ag1"
    d.mkdir(parents=True)
    return ws, dd, d


@pytest.mark.parametrize("raw,msg", [
    (b"\xff.txt", "not valid UTF-8"),
    (b"a\\b", "path traversal"),
    (b"a\nb", "control character"),
    (b"a\x1b[2J", "control character"),
    (b"a\xc2\x85b", "control character"),
    (b"a\xe2\x80\xaeb", "control character"),
    (b"a\xe2\x80\xa8b", "control character"),
])
def test_export_refuses_names_no_node_can_import(tmp_path, raw, msg):
    ws, dd, d = _home(tmp_path)
    with open(os.fsencode(str(d)) + b"/" + raw, "wb") as f:
        f.write(b"x")
    with pytest.raises(PackError, match=msg):
        ap.export_pack(ws, dd, "ag1", "n1")


def test_export_error_says_another_node(tmp_path):
    ws, dd, d = _home(tmp_path)
    (d / "a\\b").write_text("x")
    with pytest.raises(PackError, match="another node cannot take"):
        ap.export_pack(ws, dd, "ag1", "n1")


def test_export_refuses_case_twins_in_subdir(tmp_path):
    ws, dd, d = _home(tmp_path)
    (d / "sub").mkdir()
    (d / "sub" / "X.md").write_text("1")
    (d / "sub" / "x.md").write_text("2")
    with pytest.raises(PackError, match="case-insensitive"):
        ap.export_pack(ws, dd, "ag1", "n1")


def test_export_refuses_nfc_nfd_twins(tmp_path):
    ws, dd, d = _home(tmp_path)
    (d / "caf\u00e9.md").write_text("1")
    (d / "cafe\u0301.md").write_text("2")
    with pytest.raises(PackError, match="case-insensitive"):
        ap.export_pack(ws, dd, "ag1", "n1")


def test_export_refuses_file_dir_case_conflict(tmp_path):
    ws, dd, d = _home(tmp_path)
    (d / "Notes").write_text("file")
    (d / "notes").mkdir()
    (d / "notes" / "a.md").write_text("x")
    with pytest.raises(PackError, match="path conflict"):
        ap.export_pack(ws, dd, "ag1", "n1")


def test_export_good_home_round_trips(tmp_path):
    ws, dd, d = _home(tmp_path)
    (d / "café.md").write_text("ok")
    (d / "sub").mkdir()
    (d / "sub" / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(d / "sub" / "run.sh", 0o755)
    bundle, man = ap.export_pack(ws, dd, "ag1", "n1")
    ws2 = tmp_path / "other"
    (ws2 / "d").mkdir(parents=True)
    r = ap.import_pack(ws2, ws2 / "d", bundle, "n2")
    assert r["files"] == 2 and r["bytes"] == man["total_bytes"]
    assert os.stat(agentfs.agents_root(ws2) / "ag1" / "sub" / "run.sh").st_mode & 0o111


def test_export_empty_home_ok(tmp_path):
    ws, dd = env(tmp_path)
    bundle, man = ap.export_pack(ws, dd, "ag1", "n1")
    assert man["files"] == [] and man["total_bytes"] == 0


# ---------------------------------------------------------------- target not a directory
def test_target_regular_file_refused_and_kept(tmp_path):
    ws, dd = env(tmp_path)
    root = agentfs.agents_root(ws)
    root.mkdir(parents=True)
    (root / "ag1").write_text("precious")
    with pytest.raises(PackError, match="not a directory"):
        ap.import_pack(ws, dd, build(), "n2")
    assert (root / "ag1").read_text() == "precious"
    assert sorted(os.listdir(root)) == ["ag1"]


def test_target_fifo_refused_no_litter(tmp_path):
    ws, dd = env(tmp_path)
    root = agentfs.agents_root(ws)
    root.mkdir(parents=True)
    os.mkfifo(root / "ag1")
    with pytest.raises(PackError, match="not a directory"):
        ap.import_pack(ws, dd, build(), "n2")
    assert sorted(os.listdir(root)) == ["ag1"]


def test_target_dangling_symlink_still_refused(tmp_path):
    ws, dd = env(tmp_path)
    root = agentfs.agents_root(ws)
    root.mkdir(parents=True)
    os.symlink(tmp_path / "nowhere", root / "ag1")
    with pytest.raises(PackError, match="symlink"):
        ap.import_pack(ws, dd, build(), "n2")


def test_empty_target_dir_is_replaced(tmp_path):
    ws, dd = env(tmp_path)
    root = agentfs.agents_root(ws)
    (root / "ag1").mkdir(parents=True)
    assert ap.import_pack(ws, dd, build(), "n2")["files"] == 1
    assert [n for n in os.listdir(root) if n.startswith(".")] == []


# ---------------------------------------------------------------- install errors carry no host path
def test_install_oserror_has_no_host_path(tmp_path):
    ws, dd = env(tmp_path)
    root = agentfs.agents_root(ws)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("not a dir")
    with pytest.raises(PackError) as ei:
        ap.import_pack(ws, dd, build(), "n2")
    msg = str(ei.value)
    assert "could not install" in msg and str(tmp_path) not in msg and "/" not in msg


def test_memory_store_dir_error_has_no_host_path(tmp_path):
    ws, dd = env(tmp_path)
    (dd / "memory.jsonl").mkdir()
    with pytest.raises(PackError) as ei:
        ap.import_pack(ws, dd, build(), "n2")
    assert str(tmp_path) not in str(ei.value)


# ---------------------------------------------------------------- bundle type
@pytest.mark.parametrize("val", ["abc", None, 5, ["x"], {"a": 1}])
def test_non_bytes_bundle_refused(val):
    with pytest.raises(PackError, match="bundle must be bytes"):
        ap.read_pack(val)


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview])
def test_bytes_like_bundle_accepted(wrap):
    man, _, blobs = ap.read_pack(wrap(build()))
    assert man["agent_id"] == "ag1" and blobs == {"a.txt": b"hi"}


def test_non_bytes_import_refused(tmp_path):
    with pytest.raises(PackError, match="bundle must be bytes"):
        imp(tmp_path, "not bytes")


# ---------------------------------------------------------------- unchanged good behavior
def test_check_names_accepts_generator():
    ap._check_names(r for r in ["a", "b/c"])
    with pytest.raises(PackError, match="under a file"):
        ap._check_names(r for r in ["a", "A/c"])


def test_non_dict_memory_records_counted_rejected(tmp_path):
    r = imp(tmp_path, build(mem=b'[1, "x", null]'))
    assert r["memory"]["rejected"] == 3
