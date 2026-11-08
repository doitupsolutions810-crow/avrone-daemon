"""Agent packs: a portable, verifiable bundle of one agent's home.

An agent's home is its persistent dir (``workspace/agents/<id>/``) plus its durable memory
namespace (``<id>`` in ``data_dir/memory.jsonl``). ``export_pack`` writes both into a
deterministic ``tar.gz``:

    manifest.json   {"format", "agent_id", "origin", "home", "created", "total_bytes",
                     "files": [{"path", "size", "sha256", "mtime"}],
                     "memory": {"namespaces", "entries", "sha256"}}
    memory.json     [{"ns", "key", "value", "ts", "agent"}...]
    home/<relpath>  every regular file of the agent dir

Determinism: entries sorted, tar headers normalised (uid/gid 0, no owner names, mode 0644),
gzip header mtime 0, and ``created`` defaults to the newest content timestamp (file mtimes and
memory ts), so the same home always yields byte-identical bundles.

Export refuses symlinks, devices, fifos and sockets anywhere in the dir, and a home over the
agent-fs quota (AVRONE_AGENT_FS_QUOTA_MB).

Import (``import_pack``) is defensive: bundle size, member count and declared sizes are capped
before anything is extracted; every member must be ``manifest.json``, ``memory.json`` or a
regular ``home/<relpath>`` with no absolute path, no ``..`` and no link/device type; the set of
files must match the manifest exactly and every sha256 must verify. Files are extracted into a
temp dir beside the target and swapped in atomically, and only into ``agents/<manifest agent_id>``.

Overwrite rule: an existing, non-empty home is replaced ONLY when it is the same agent (same
agent id AND same ``home`` node, i.e. where the agent was born) and the pack is strictly newer
than what is there. Memory merges newest-wins into the agent's own local namespace.
Provenance of imported homes is kept in ``data_dir/agentpacks/index.json``.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat as _st
import tarfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from . import agentfs, memstore
from .agentfs_tools import dir_usage, quota_bytes

FORMAT = "avrone-agentpack/1"
MANIFEST, MEMORY = "manifest.json", "memory.json"
HOME_PREFIX = "home/"
MAX_MEMBERS = 20000
INDEX_DIR = "agentpacks"
NAME_MAX_BYTES = 255       # one path component (APFS, ext4, exFAT)
PATH_MAX_BYTES = 1000      # whole relative path, under macOS PATH_MAX (1024) with room for the root
MTIME_MIN, MTIME_MAX = 0, 2**33  # 1970 .. year 2242: a wider manifest value is clamped on install


class PackError(ValueError):
    """A pack that cannot be exported or imported safely."""


_IMPORT_LOCKS: dict[str, threading.Lock] = {}
_IMPORT_GUARD = threading.Lock()
# index.json holds EVERY agent's record, but imports only hold their own per-agent lock: two imports
# (or an import and a migrate_agent record) of different agents read-modify-wrote the index
# concurrently and the later os.replace dropped the other agent's home record (which then made a
# later import of that agent refuse with "belongs to another agent"). They also shared one temp name.
_INDEX_LOCK = threading.Lock()


def _import_lock(target: Path) -> threading.Lock:
    with _IMPORT_GUARD:
        return _IMPORT_LOCKS.setdefault(str(target), threading.Lock())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _memory_path(data_dir: Path) -> Path:
    return Path(data_dir) / "memory.jsonl"


def pack_dir(data_dir: Path) -> Path:
    return Path(data_dir) / INDEX_DIR


INDEX_MAX_BYTES = 16 * 1024 * 1024

def _index_path(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "index.json"


def load_index(data_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        from .safeio import read_capped  # a FIFO index.json hung every pack tool
        raw = read_capped(_index_path(data_dir), INDEX_MAX_BYTES)
        d = json.loads(raw.decode("utf-8")) if raw is not None else {}
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _save_index(data_dir: Path, idx: dict[str, Any]) -> None:
    p = _index_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".index.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(idx, indent=2, sort_keys=True))
    os.replace(tmp, p)


def record(data_dir: Path, agent_id: str, **kw: Any) -> None:
    with _INDEX_LOCK:
        idx = load_index(data_dir)
        idx.setdefault(agent_id, {}).update(kw)
        _save_index(data_dir, idx)


def home_node(data_dir: Path, agent_id: str, local_node: str) -> str:
    """Where an agent was born: the recorded home of an imported agent, else this node."""
    return str(load_index(data_dir).get(agent_id, {}).get("home") or local_node)


# ------------------------------------------------------------------ export
def _walk(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    """Every regular file under root as (posix relpath, path, lstat); refuses anything else."""
    out = []
    for cur, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs) + sorted(files):
            p = Path(cur) / name
            try:
                st = os.lstat(p)
            except FileNotFoundError:  # deleted while we walked: a busy agent, not an error
                continue
            rel = p.relative_to(root).as_posix()
            if os.path.islink(p):
                raise PackError(f"refusing symlink in agent home: {rel}")
            if _st.S_ISDIR(st.st_mode):
                continue
            if not _st.S_ISREG(st.st_mode):
                raise PackError(f"refusing special file (device/fifo/socket) in agent home: {rel}")
            out.append((rel, p, st))
    out.sort(key=lambda t: t[0])
    return out


def _memory_records(data_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    mp = _memory_path(data_dir)
    if not mp.exists() or not memstore.is_local(agent_id):
        return []
    space = memstore.MemoryStore(mp).load(agent_id)
    recs = [{"ns": agent_id, "key": str(r["key"]), "value": str(r.get("value", "")),
             "ts": float(r.get("ts", 0)), "agent": str(r.get("agent", ""))[:100]} for r in space.values()]
    recs.sort(key=lambda r: (r["key"], r["ts"]))
    return recs


def content_stamp(workspace: Path, data_dir: Path, agent_id: str) -> int:
    """Newest content timestamp (int seconds) of an agent's home: file mtimes + memory ts."""
    d = agentfs.agents_root(workspace) / agentfs.sanitize_agent_id(agent_id)
    stamp = 0
    if d.is_dir() and not d.is_symlink():
        for cur, _dirs, files in os.walk(d, followlinks=False):
            for f in files:
                try:
                    stamp = max(stamp, int(os.lstat(os.path.join(cur, f)).st_mtime))
                except OSError:
                    continue
    for r in _memory_records(data_dir, agentfs.sanitize_agent_id(agent_id)):
        stamp = max(stamp, int(r["ts"]))
    return stamp


def _tarinfo(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode = size, mtime, 0o644
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.type = tarfile.REGTYPE
    return ti


def export_pack(workspace: Path, data_dir: Path, agent_id: str, origin: str, home: str | None = None,
                created: int | None = None) -> tuple[bytes, dict[str, Any]]:
    """Bundle an agent's home. Returns (tar.gz bytes, manifest)."""
    sid = agentfs.sanitize_agent_id(agent_id)
    if sid != str(agent_id):
        raise PackError(f"invalid agent id {agent_id!r}")
    d = agentfs.agents_root(workspace) / sid
    if d.is_symlink():
        raise PackError("agent dir must not be a symlink")
    files = _walk(d) if d.is_dir() else []
    total = sum(st.st_size for _, _, st in files)
    quota = quota_bytes()
    if total > quota:
        raise PackError(f"agent home is {total} bytes; over the agent-fs quota of {quota} bytes")
    blobs: list[tuple[str, bytes, int]] = []
    entries = []
    for rel, p, st in files:
        try:
            # round 107: read_bytes() followed a symlink swapped in after _walk's lstat (packing a
            # host file into the export) and blocked forever on a swapped-in FIFO
            from .safeio import FileTooBig, read_capped
            try:
                data = read_capped(p, st.st_size)
            except FileTooBig:
                raise PackError(f"{rel} changed while packing; retry when the agent is idle") from None
            if data is None:
                if not os.path.lexists(p):
                    raise FileNotFoundError(str(p))
                raise PackError(f"{rel} changed while packing; retry when the agent is idle")
        except FileNotFoundError:
            raise PackError(f"{rel} vanished while packing; retry when the agent is idle") from None
        except OSError as e:  # unreadable file: name it instead of failing the whole migrate with a 500
            raise PackError(f"cannot read {rel}: {type(e).__name__}: {e.strerror or e}") from None
        if len(data) != st.st_size:
            raise PackError(f"{rel} changed while packing")
        mt = int(st.st_mtime)
        blobs.append((HOME_PREFIX + rel, data, mt))
        ent = {"path": rel, "size": len(data), "sha256": _sha(data), "mtime": mt}
        if st.st_mode & 0o111:  # executable scripts/tools stay executable on the other node
            ent["mode"] = 0o755
        entries.append(ent)
    mem = _memory_records(data_dir, sid)
    mem_bytes = json.dumps(mem, sort_keys=True, separators=(",", ":")).encode()
    if created is None:
        created = max([e["mtime"] for e in entries] + [int(r["ts"]) for r in mem] + [0])
    manifest = {"format": FORMAT, "agent_id": sid, "origin": str(origin), "home": str(home or origin),
                "created": int(created), "total_bytes": total, "files": entries,
                "memory": {"namespaces": [sid] if mem else [], "entries": len(mem), "sha256": _sha(mem_bytes)}}
    man_bytes = json.dumps(manifest, sort_keys=True, indent=1).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data, mt in [(MANIFEST, man_bytes, int(created)), (MEMORY, mem_bytes, int(created))] + blobs:
            tf.addfile(_tarinfo(name, len(data), mt), io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=6) as gz:
        gz.write(raw.getvalue())
    return out.getvalue(), manifest


# ------------------------------------------------------------------ import
def _check_rel(rel: str) -> str:
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise PackError(f"bad path in pack: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or "\\" in rel:
        raise PackError(f"path traversal in pack: {rel!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in rel):
        # newline/escape bytes in a file name break every log line and terminal listing that shows it
        raise PackError(f"control character in pack path: {rel!r}")
    try:
        raw = rel.encode("utf-8")
    except UnicodeEncodeError:
        raise PackError(f"pack path is not valid UTF-8: {rel!r}") from None
    if len(raw) > PATH_MAX_BYTES or any(len(p.encode()) > NAME_MAX_BYTES for p in parts):
        raise PackError(f"pack path too long for the target filesystem: {rel[:80]!r}...")
    return rel


def fold_path(rel: str) -> str:
    """How a case-insensitive, normalization-insensitive filesystem (macOS APFS/HFS+ default,
    exFAT) sees a path: NFC + casefold."""
    return unicodedata.normalize("NFC", rel).casefold()


def zlib_error() -> type[Exception]:
    import zlib
    return zlib.error


class _CappedStream(io.RawIOBase):
    """Decompressed view of the bundle that refuses to yield more than ``cap`` bytes in total.
    The per-member quota checks run on header-declared sizes, but tarfile reads some headers
    (PAX extended headers, GNU long names) into memory before any member is returned: a 290 KB
    bundle carrying a 300 MB PAX header used ~1.7 GB of RAM. Capping the stream itself bounds
    every read, whatever tarfile does with it."""

    def __init__(self, src: Any, cap: int):
        self._src, self._cap, self.total = src, cap, 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        want = min(len(b), self._cap - self.total + 1)
        data = self._src.read(want) if want > 0 else self._src.read(1)
        if data and self.total + len(data) > self._cap:
            raise PackError("pack expands past the quota (decompression bomb?)")
        n = len(data)
        b[:n] = data
        self.total += n
        return n


def read_pack_file(p: Path) -> bytes:
    """A pack's bytes, read with O_NOFOLLOW|O_NONBLOCK and capped at the bundle limit (round 86:
    read_bytes() loaded a multi-GB file whole before read_pack could refuse it, and a FIFO named
    <agent>-<ts>.tar.gz parked a worker thread forever in pack_list)."""
    from .safeio import FileTooBig, read_capped
    try:
        data = read_capped(p, quota_bytes() + 1024 * 1024)
    except FileTooBig:
        raise PackError("bundle is over the quota") from None
    if data is None:
        raise PackError("not a regular file (symlink, FIFO or device)")
    return data


def read_pack(bundle: bytes) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, bytes]]:
    """Parse and fully verify a bundle in memory. Returns (manifest, memory records, {rel: bytes})."""
    quota = quota_bytes()
    if len(bundle) > quota + 1024 * 1024:
        raise PackError(f"bundle is {len(bundle)} bytes; over the quota")
    declared = 0
    count = 0
    blobs: dict[str, bytes] = {}
    man = mem = None
    try:
        import gzip
        # stream mode ("r|"): no seeking, so every byte tarfile sees passes through the cap; tar
        # framing (512-byte headers + padding per member) gets generous headroom on top of the quota
        cap = quota + 1024 * 1024 + 1024 * (MAX_MEMBERS + 4)
        capped = io.BufferedReader(_CappedStream(gzip.GzipFile(fileobj=io.BytesIO(bundle)), cap))
        tf = tarfile.open(fileobj=capped, mode="r|")
        # Walk members lazily, checking the count and declared bytes as each header is read: the
        # old getmembers() first decompressed the whole stream (a gzip bomb within the bundle cap).
        while True:
            m = tf.next()
            if m is None:
                break
            count += 1
            if count > MAX_MEMBERS:
                raise PackError("too many members in pack")
            if not m.isreg():
                raise PackError(f"refusing non-regular member (link/device/dir) {m.name!r}")
            declared += m.size
            if declared > quota + 1024 * 1024:
                raise PackError("pack contents exceed the quota")
            f = tf.extractfile(m)
            data = f.read() if f else b""
            if m.name in (MANIFEST, MEMORY):
                if (man if m.name == MANIFEST else mem) is not None:  # was: the last copy silently won
                    raise PackError(f"duplicate member {m.name!r}")
                if m.name == MANIFEST:
                    man = data
                else:
                    mem = data
            elif m.name.startswith(HOME_PREFIX):
                rel = _check_rel(m.name[len(HOME_PREFIX):])
                if rel in blobs:
                    raise PackError(f"duplicate member {rel!r}")
                blobs[rel] = data
            else:
                raise PackError(f"unexpected member {m.name!r}")
    except (tarfile.TarError, OSError, EOFError, zlib_error()) as e:
        raise PackError(f"not a valid agent pack: {e}") from None
    if man is None or mem is None:
        raise PackError("pack has no manifest/memory")
    try:
        manifest = json.loads(man)
        memory = json.loads(mem)
    except (ValueError, RecursionError):  # RecursionError: absurdly nested JSON
        raise PackError("manifest/memory is not JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise PackError("unknown pack format")
    aid = manifest.get("agent_id")
    if not isinstance(aid, str) or agentfs.sanitize_agent_id(aid) != aid:
        raise PackError("bad agent_id in manifest")
    for k in ("origin", "home"):
        if not isinstance(manifest.get(k), str) or not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", manifest[k]):
            raise PackError(f"bad {k} in manifest")
    if not isinstance(manifest.get("created"), int) or isinstance(manifest.get("created"), bool):
        raise PackError("bad created in manifest")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PackError("bad files list in manifest")
    listed = {}
    for e in files:
        if not isinstance(e, dict) or not isinstance(e.get("path"), str):
            raise PackError("bad file entry")
        rel = _check_rel(e["path"])
        if rel in listed:
            raise PackError(f"duplicate manifest entry {rel!r}")
        if isinstance(e.get("size"), bool) or not isinstance(e.get("size"), int):
            raise PackError(f"bad size for {rel!r}")
        md = e.get("mode")
        if md is not None and (isinstance(md, bool) or md not in (0o644, 0o755)):
            raise PackError(f"bad mode for {rel!r} (only 0644 or 0755)")
        mt = e.get("mtime")
        if mt is not None and (isinstance(mt, bool) or not isinstance(mt, int) or not -2**40 < mt < 2**40):
            raise PackError(f"bad mtime for {rel!r}")
        listed[rel] = e
    if set(listed) != set(blobs):
        raise PackError("pack files do not match the manifest")
    total = 0
    for rel, e in listed.items():
        data = blobs[rel]
        if len(data) != e.get("size") or _sha(data) != e.get("sha256"):
            raise PackError(f"hash/size mismatch for {rel!r} (tampered pack)")
        total += len(data)
    if total > quota:
        raise PackError(f"pack holds {total} bytes; over the agent-fs quota of {quota} bytes")
    folded: dict[str, str] = {}
    for rel in blobs:
        f = fold_path(rel)
        if f in folded:
            # on a Mac (case-insensitive APFS) "Notes.md" and "notes.md" are ONE file: the second
            # write silently replaced the first, so the import "succeeded" with a file missing
            raise PackError(f"path conflict in pack: {folded[f]!r} and {rel!r} are the same file on a "
                            "case-insensitive filesystem (macOS); rename one on the sending node")
        folded[f] = rel
    for rel in blobs:  # a file may not also be a directory ("a" and "a/b"), in any case
        parts = rel.split("/")
        for i in range(1, len(parts)):
            if fold_path("/".join(parts[:i])) in folded:
                raise PackError(f"path conflict in pack: {rel!r} is under a file")
    if not 0 <= manifest["created"] < 2**40:
        raise PackError("bad created in manifest")
    if _sha(mem) != (manifest.get("memory") or {}).get("sha256"):
        raise PackError("memory hash mismatch (tampered pack)")
    if not isinstance(memory, list):
        raise PackError("memory must be a list")
    return manifest, memory, blobs


def import_pack(workspace: Path, data_dir: Path, bundle: bytes, local_node: str,
                expect_agent: str | None = None, expect_origin: str | None = None) -> dict[str, Any]:
    """Verify and install a bundle into agents/<agent_id>. Raises PackError on any refusal."""
    manifest, memory, blobs = read_pack(bundle)
    aid = manifest["agent_id"]
    if expect_agent is not None and aid != expect_agent:
        raise PackError(f"pack is for agent {aid!r}, not {expect_agent!r}")
    if expect_origin is not None and manifest["origin"] != expect_origin:
        raise PackError(f"pack origin {manifest['origin']!r} does not match sender {expect_origin!r}")
    root = agentfs.agents_root(workspace)
    # One import per agent dir at a time: two concurrent imports of the same agent both passed
    # the "strictly newer" check and raced the swap (and could collide on the temp dir name).
    # The memory merge and home record stay inside the lock too: a second importer that ran between
    # the swap and record() saw the files but no home record and refused with "belongs to another agent".
    with _import_lock(root / aid):
        try:
            _install(root, workspace, data_dir, manifest, blobs, aid, local_node)
        except OSError as e:  # e.g. a name too long for this filesystem: a refusal, not a 500
            raise PackError(f"could not install agent pack: {type(e).__name__}: {e}"[:300]) from None
        stats = {"merged": 0, "skipped": 0, "rejected": 0}
        good = [r for r in memory if isinstance(r, dict)]
        stats["rejected"] += len(memory) - len(good)
        if good:
            stats = {k: stats[k] + v for k, v in
                     memstore.MemoryStore(_memory_path(data_dir)).merge(aid, good, source=manifest["origin"]).items()}
        if manifest["home"] != local_node:
            record(data_dir, aid, home=manifest["home"], origin=manifest["origin"], created=manifest["created"],
                   imported=int(time.time()))
    return {"agent_id": aid, "origin": manifest["origin"], "home": manifest["home"],
            "created": manifest["created"], "files": len(blobs), "bytes": manifest["total_bytes"],
            "memory": stats}


def _install(root: Path, workspace: Path, data_dir: Path, manifest: dict[str, Any],
             blobs: dict[str, bytes], aid: str, local_node: str) -> None:
    if root.is_symlink():
        raise PackError("agents root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    target = root / aid
    if target.is_symlink():
        raise PackError("agent dir must not be a symlink")
    existing = target.is_dir() and any(target.iterdir())
    have_mem = bool(_memory_records(data_dir, aid))
    if existing or have_mem:
        local_home = home_node(data_dir, aid, local_node)
        if manifest["home"] != local_home:
            raise PackError(f"agent dir {aid!r} belongs to another agent (home {local_home}, "
                            f"pack home {manifest['home']}); refusing to overwrite")
        stamp = content_stamp(workspace, data_dir, aid)
        if manifest["created"] <= stamp:
            raise PackError(f"pack (created {manifest['created']}) is not newer than the local home "
                            f"({stamp}); refusing to overwrite")
    tmp = root / f".import-{aid}-{os.getpid()}-{secrets.token_hex(6)}"
    old = None
    try:
        tmp.mkdir(mode=0o700)
        by_path = {e["path"]: e for e in manifest["files"]}
        for rel, data in sorted(blobs.items()):
            dest = (tmp / rel).resolve()
            if not dest.is_relative_to(tmp.resolve()):
                raise PackError(f"path escapes the agent dir: {rel!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if by_path[rel].get("mode") == 0o755:
                os.chmod(dest, 0o755)
            mt = int(by_path[rel].get("mtime") or manifest["created"])
            mt = max(MTIME_MIN, min(MTIME_MAX, mt))
            try:  # a timestamp the filesystem can't hold (exFAT starts in 1980) is cosmetic, not fatal
                os.utime(dest, (mt, mt))
            except (OSError, OverflowError, ValueError):
                pass
        if target.exists():
            old = root / f".old-{aid}-{os.getpid()}-{secrets.token_hex(6)}"
            os.replace(target, old)
        os.replace(tmp, target)
        os.chmod(target, 0o700)
    except BaseException:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        if old is not None and old.exists() and not target.exists():
            os.replace(old, target)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
