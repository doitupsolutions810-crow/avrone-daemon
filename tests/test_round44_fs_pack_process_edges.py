"""Round 44: filesystem escapes, hostile agent-pack archives, and several processes on one data dir.

  * fleet replay cache across PROCESSES: a request accepted by worker A must be refused by worker B
    (separate FleetManager objects sharing a data dir), and neither save may erase the other's nonces;
  * checkpoint lost updates: two processes putting different steps into one checkpoint keep every
    step; a cleared scope is not resurrected from disk; a claim in one process blocks the other and
    is released when that process dies;
  * agentfs.confine / tools._safe_path against seeded hostile paths and real symlinks pointing out;
  * agent-pack import of booby-trapped tar.gz bundles: symlinks, hardlinks, devices, dirs,
    duplicates, case collisions, traversal, a gzip bomb, truncated streams, wrong hashes.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import multiprocessing as mp
import os
import random
import string
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentfs, agentpack, checkpoint
from avrone.agentfs import AgentFSError
from avrone.agentpack import PackError, read_pack
from avrone.fleet import FleetManager, GatewayRefused, sign
from avrone.tools import _safe_path

ITERS = max(50, int(os.environ.get("FUZZ_ITERS", "400") or 400))
FORK = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
TOKEN = "round44-fleet-token-" + "q" * 32


# ------------------------------------------------------------------ fleet: two workers, one data dir
def _fm(tmp_path: Path) -> FleetManager:
    os.environ["AVRONE_FLEET_TOKEN"] = TOKEN
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(tmp_path)))
    try:
        return FleetManager(core, data_dir=tmp_path)
    except TypeError:
        return FleetManager(core)


@pytest.fixture
def fleet_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOKEN)
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path))
    return tmp_path


def _headers(body: bytes, path: str = "/fleet/run") -> dict[str, str]:
    return sign(TOKEN, "POST", path, body)


def test_replay_refused_by_the_other_worker(fleet_env):
    a, b = _fm(fleet_env), _fm(fleet_env)
    if getattr(a, "_nonce_path", None) is None or a._nonce_path != b._nonce_path:
        pytest.skip("FleetManager not bound to a shared data dir in this configuration")
    body = b'{"task":"x"}'
    h = _headers(body)
    a.verify("POST", "/fleet/run", body, h)
    with pytest.raises(GatewayRefused) as e:
        b.verify("POST", "/fleet/run", body, h)
    assert e.value.code == "replay"


def test_two_workers_never_erase_each_others_nonces(fleet_env):
    a, b = _fm(fleet_env), _fm(fleet_env)
    if getattr(a, "_nonce_path", None) is None:
        pytest.skip("no shared data dir")
    sent = []
    for i in range(40):
        body = json.dumps({"i": i}).encode()
        h = _headers(body)
        (a if i % 2 else b).verify("POST", "/fleet/run", body, h)
        sent.append((body, h))
    on_disk = json.loads(a._nonce_path.read_text())
    assert all(h["x-avrone-nonce"] in on_disk for _, h in sent)
    c = _fm(fleet_env)  # a third process starting later refuses every one of them
    for body, h in sent:
        with pytest.raises(GatewayRefused):
            c.verify("POST", "/fleet/run", body, h)


def _proc_verify(data_dir: str, body: bytes, headers: dict, q) -> None:
    os.environ["AVRONE_FLEET_TOKEN"] = TOKEN
    os.environ["AVRONE_DATA_DIR"] = data_dir
    fm = _fm(Path(data_dir))
    try:
        fm.verify("POST", "/fleet/run", body, headers)
        q.put("accepted")
    except GatewayRefused as e:
        q.put(e.code)


@FORK
def test_replay_race_across_forked_processes(fleet_env):
    probe = _fm(fleet_env)
    if getattr(probe, "_nonce_path", None) is None:
        pytest.skip("no shared data dir")
    ctx = mp.get_context("fork")
    for round_ in range(5):
        body = json.dumps({"r": round_}).encode()
        h = _headers(body)
        q = ctx.Queue()
        ps = [ctx.Process(target=_proc_verify, args=(str(fleet_env), body, h, q)) for _ in range(6)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        got = [q.get(timeout=5) for _ in ps]
        assert got.count("accepted") == 1, got  # the same signed request runs exactly once
        assert set(got) <= {"accepted", "replay"}


def test_skewed_clock_and_window_edges(fleet_env):
    fm = _fm(fleet_env)
    from avrone.fleet import WINDOW_S
    now = int(time.time())
    for off, ok in [(-WINDOW_S - 5, False), (WINDOW_S + 5, False), (-WINDOW_S + 3, True),
                    (WINDOW_S - 3, True), (10**9, False), (-now, False)]:
        body = json.dumps({"off": off}).encode()
        h = sign(TOKEN, "POST", "/fleet/run", body, ts=str(now + off))
        if ok:
            fm.verify("POST", "/fleet/run", body, h)
        else:
            with pytest.raises(GatewayRefused) as e:
                fm.verify("POST", "/fleet/run", body, h)
            assert e.value.code in ("stale", "bad_timestamp")


def test_nonce_file_hostile_contents(fleet_env):
    fm = _fm(fleet_env)
    if getattr(fm, "_nonce_path", None) is None:
        pytest.skip("no shared data dir")
    for junk in ['{"a":"b"}', '{"' + "x" * 300 + '":9e99}', "[1,2]", "{" * 5000, '{"ok_nonce":true}',
                 '{"bad nonce!":9e99}', "\x00\xff"]:
        fm._nonce_path.write_text(junk, errors="surrogateescape")
        body = os.urandom(8).hex().encode()
        fm.verify("POST", "/fleet/run", body, _headers(body))  # never raises on a hostile cache file


# ------------------------------------------------------------------ checkpoint: processes
def _proc_put(path: str, prefix: str, n: int) -> None:
    cp = checkpoint.Checkpoint(path)
    for i in range(n):
        assert cp.put(f"{prefix}{i}", {"v": i})


@FORK
def test_checkpoint_two_processes_keep_every_step(tmp_path):
    path = str(tmp_path / "ck.json")
    ctx = mp.get_context("fork")
    ps = [ctx.Process(target=_proc_put, args=(path, f"w{w}-", 40)) for w in range(3)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
        assert p.exitcode == 0
    keys = set(checkpoint.Checkpoint(path).keys())
    assert keys == {f"w{w}-{i}" for w in range(3) for i in range(40)}, len(keys)


def test_checkpoint_stale_store_does_not_erase_or_resurrect(tmp_path):
    path = tmp_path / "ck.json"
    a = checkpoint.Checkpoint(path)
    b = checkpoint.Checkpoint(checkpoint._Store(path))  # a second process's view (its own store)
    a.put("a1", 1)
    b.put("b1", 2)                 # b never saw a1: it must not erase it
    assert set(checkpoint.Checkpoint(checkpoint._Store(path)).keys()) == {"a1", "b1"}
    sa = a.scope("s")
    sa.put("x", 1)
    sa.clear()                     # cleared here...
    b.put("b2", 3)                 # ...and b's stale memory never held s/x, disk never resurrects it
    final = set(checkpoint.Checkpoint(checkpoint._Store(path)).keys())
    assert final == {"a1", "b1", "b2"}, final


def _proc_hold_claim(path: str, ready, done) -> None:
    rel = checkpoint.claim(path)
    ready.set()
    done.wait(20)
    rel()


def _proc_claim_and_die(path: str, ready) -> None:
    checkpoint.claim(path)
    ready.set()
    os._exit(0)  # crash without releasing


@FORK
def test_claim_is_exclusive_across_processes_and_freed_on_crash(tmp_path):
    path = str(tmp_path / "job.json")
    ctx = mp.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_proc_hold_claim, args=(path, ready, done))
    p.start()
    assert ready.wait(10)
    with pytest.raises(checkpoint.CheckpointConflict):
        checkpoint.claim(path)
    done.set()
    p.join(10)
    checkpoint.claim(path)()       # free again once the holder released

    ready2 = ctx.Event()
    p2 = ctx.Process(target=_proc_claim_and_die, args=(path, ready2))
    p2.start()
    assert ready2.wait(10)
    p2.join(10)
    checkpoint.claim(path)()       # the kernel dropped the dead process's claim


# ------------------------------------------------------------------ paths and symlinks
def _hostile_path(r: random.Random) -> str:
    pieces = ["..", ".", "", "a", "b c", "~", "~root", "C:", "c:\\x", "\\\\srv\\s", "/etc", "\x00",
              "%2e%2e", "\u2025", "\uff0e\uff0e", "a\u202eb", "\ud800", "😀", "con", "nul", "x" * 300, "...",
              ". .", ".. ", " ..", "a/../..", "a\\..\\..", "./.."]
    sep = r.choice(["/", "\\", "//", "/./"])
    return sep.join(r.choice(pieces) for _ in range(r.randint(1, 6)))


@pytest.mark.parametrize("seed", [44, 4444, 20261105])
def test_confine_and_safe_path_never_escape(tmp_path, seed):
    r = random.Random(seed)
    base = tmp_path / "agent"
    base.mkdir()
    rb = base.resolve()
    for i in range(ITERS):
        raw = _hostile_path(r)
        try:
            p = agentfs.confine(base, raw)
        except (AgentFSError, ValueError, OSError):
            pass
        else:
            assert p == rb or p.is_relative_to(rb), (seed, i, raw, p)
        try:
            q = _safe_path(base, raw)
        except (ValueError, OSError):
            pass
        else:
            assert q == rb or q.is_relative_to(rb), (seed, i, raw, q)


def test_symlinks_inside_the_dir_cannot_lead_out(tmp_path):
    base = tmp_path / "agent"
    (base / "sub").mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "k.txt").write_text("s")
    os.symlink(outside, base / "out")                 # dir link to a sibling
    os.symlink("/", base / "root")                    # link to /
    os.symlink("../../secret/k.txt", base / "sub" / "rel")  # relative file link
    os.symlink(base / "loop", base / "loop")          # self loop
    for raw in ["out/k.txt", "out", "root/etc/passwd", "sub/rel", "loop", "loop/x"]:
        for fn in (agentfs.confine, _safe_path):
            try:
                p = fn(base, raw)
            except (AgentFSError, ValueError, OSError, RuntimeError):
                continue
            assert p.is_relative_to(base.resolve()), (fn.__name__, raw, p)


# ------------------------------------------------------------------ hostile agent packs
def _member(tf: tarfile.TarFile, name: str, data: bytes = b"", kind: bytes = tarfile.REGTYPE, link: str = "") -> None:
    ti = tarfile.TarInfo(name)
    ti.type, ti.linkname, ti.mtime, ti.mode = kind, link, 1_700_000_000, 0o644
    ti.size = len(data) if kind == tarfile.REGTYPE else 0
    tf.addfile(ti, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def _pack(files: dict[str, bytes], extra=None, manifest_patch=None, memory=b"[]") -> bytes:
    man = {"format": agentpack.FORMAT, "agent_id": "a1", "origin": "node1", "home": "node1",
           "created": 1_700_000_000, "total_bytes": sum(map(len, files.values())),
           "files": [{"path": k, "size": len(v), "sha256": hashlib.sha256(v).hexdigest(), "mode": 0o644}
                     for k, v in files.items()],
           "memory": {"sha256": hashlib.sha256(memory).hexdigest()}}
    man.update(manifest_patch or {})
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        _member(tf, agentpack.MANIFEST, json.dumps(man).encode())
        _member(tf, agentpack.MEMORY, memory)
        for k, v in files.items():
            _member(tf, agentpack.HOME_PREFIX + k, v)
        for args in extra or []:
            _member(tf, *args)
    return gzip.compress(raw.getvalue())


def test_good_pack_parses():
    man, mem, blobs = read_pack(_pack({"notes.md": b"hi", "d/x.py": b"print(1)"}))
    assert set(blobs) == {"notes.md", "d/x.py"}


H = agentpack.HOME_PREFIX


@pytest.mark.parametrize("label,bundle", [
    ("symlink", lambda: _pack({}, extra=[(H + "evil", b"", tarfile.SYMTYPE, "/etc/passwd")])),
    ("hardlink", lambda: _pack({}, extra=[(H + "evil", b"", tarfile.LNKTYPE, "/etc/passwd")])),
    ("chardev", lambda: _pack({}, extra=[(H + "dev", b"", tarfile.CHRTYPE)])),
    ("fifo", lambda: _pack({}, extra=[(H + "fifo", b"", tarfile.FIFOTYPE)])),
    ("dir", lambda: _pack({}, extra=[(H + "d", b"", tarfile.DIRTYPE)])),
    ("traversal", lambda: _pack({"../../x": b"1"})),
    ("absolute", lambda: _pack({"/etc/x": b"1"})),
    ("backslash", lambda: _pack({"a\\..\\x": b"1"})),
    ("drive", lambda: _pack({"C:x": b"1"})),
    ("ctrl", lambda: _pack({"a\nb": b"1"})),
    ("case-collision", lambda: _pack({"Notes.md": b"1", "notes.md": b"2"})),
    ("nfc-collision", lambda: _pack({"caf\u00e9": b"1", "cafe\u0301": b"2"})),
    ("file-and-dir", lambda: _pack({"a": b"1", "a/b": b"2"})),
    ("dup-manifest", lambda: _pack({}, extra=[(agentpack.MANIFEST, b"{}")])),
    ("unexpected", lambda: _pack({}, extra=[("other/x", b"1")])),
    ("hash", lambda: _pack({"x": b"1"}, manifest_patch={"files": [{"path": "x", "size": 1, "sha256": "0" * 64}]})),
    ("mode", lambda: _pack({"x": b"1"}, manifest_patch={"files": [
        {"path": "x", "size": 1, "sha256": hashlib.sha256(b"1").hexdigest(), "mode": 0o4755}]})),
    ("agent-id", lambda: _pack({}, manifest_patch={"agent_id": "../root"})),
    ("origin", lambda: _pack({}, manifest_patch={"origin": "Node One!"})),
    ("created-bool", lambda: _pack({}, manifest_patch={"created": True})),
    ("memory-tamper", lambda: _pack({}, manifest_patch={"memory": {"sha256": "0" * 64}})),
    ("memory-not-list", lambda: _pack({}, memory=b'{"a":1}')),
    ("unlisted-file", lambda: _pack({}, extra=[(H + "ghost", b"boo")])),
    ("not-gzip", lambda: b"PK\x03\x04 definitely a zip"),
    ("empty", lambda: b""),
    ("truncated", lambda: _pack({"x": b"1" * 5000})[:-40]),
])
def test_hostile_pack_refused(label, bundle):
    with pytest.raises(PackError):
        read_pack(bundle())


def test_gzip_bomb_refused_without_inflating_it(monkeypatch):
    monkeypatch.setattr(agentpack, "quota_bytes", lambda: 256 * 1024)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        ti = tarfile.TarInfo(H + "bomb")
        ti.size = 64 * 1024 * 1024
        tf.addfile(ti, io.BytesIO(b"\x00" * ti.size))
    bomb = gzip.compress(raw.getvalue(), compresslevel=9)
    assert len(bomb) < 256 * 1024  # tiny on the wire
    t0 = time.monotonic()
    with pytest.raises(PackError):
        read_pack(bomb)
    assert time.monotonic() - t0 < 5


@pytest.mark.parametrize("seed", [44, 4545])
def test_random_byte_flips_in_a_good_pack(seed):
    r = random.Random(seed)
    good = bytearray(_pack({"notes.md": b"hello " * 50, "src/a.py": b"x = 1\n"}))
    for _ in range(max(60, ITERS // 4)):
        b = bytearray(good)
        for _ in range(r.randint(1, 4)):
            b[r.randrange(len(b))] = r.randrange(256)
        try:
            read_pack(bytes(b))
        except PackError:
            continue  # the only acceptable failure
