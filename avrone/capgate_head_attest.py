"""capgate_head_attest: forward capgate's signed head to attested-history (the Descent IX bridge).

Descent IX (the Anchor Floor): capgate's whole receipt chain is anchored in ONE key
(AVRONE_CAPGATE_KEY, defaulting to AVRONE_FLEET_TOKEN, else a random per-process key the
capgate docstring admits is unverifiable across restarts). Depth through a shared anchor does
not compound; it transmits — a thousand receipts signed with one key are one trial wearing a
thousand namespaces. This bridge attests the signed HEAD (count + head hash + the verify_chain
walk) as an entry in attested-history's append-only, git-anchored log: a second, INDEPENDENT,
human-anchored cause. Beating the node key no longer beats the history.

The bridge is a ZERO-HEAT edge: every check is deterministic (p = 1, S = 0), so it moves the
anchor floor (Psi_sys = CONE + heat(p_a^m)) without spending trial heat — and it MINTS its
cause only when an independent verify pass accepts the entry: authorship transmits,
measurement compounds (Descent X, criterion 8).

Off by default so existing deployments behave exactly as before.

Env:
  AVRONE_CAPGATE_ATTEST           on | off   (default off; "1"/"true"/"yes" also enable)
  AVRONE_ATTESTED_HISTORY_LOG     path to history.jsonl (default <data_dir>/attested/history.jsonl)

Entry = attested-history schema v1 exactly: {seq, ts, source, kind, digest, provenance_depth,
residual, capability_id, human_root, raw, prev_hash}; digest covers the canonical JSON of the
entry minus digest; prev_hash chains to the previous entry's digest; unmeasured must be named.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # package mode
    from . import capgate
except ImportError:  # script mode: python avrone/capgate_head_attest.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from avrone import capgate

SOURCE = "avrone-daemon"
KIND = "capgate_head"
GENESIS = "0" * 64
MAX_LOG_BYTES = 1 << 24  # fail closed beyond 16 MiB rather than scan forever
_OPEN_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
NL = chr(10)


def enabled() -> bool:
    v = (os.environ.get("AVRONE_CAPGATE_ATTEST") or "").strip().lower()
    return v in ("on", "1", "true", "yes")


def history_path(data_dir: str | Path) -> Path:
    env = (os.environ.get("AVRONE_ATTESTED_HISTORY_LOG") or "").strip()
    return Path(env) if env else Path(data_dir) / "attested" / "history.jsonl"


def _canon(obj: Any) -> bytes:
    # round 46: allow_nan=False. A NaN anywhere in an entry was written as bare NaN, which is not
    # JSON: other attested-history readers refuse the line, and this reader now does too.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str,
                      allow_nan=False).encode()


def _no_constants(name: str) -> Any:
    raise ValueError(f"non-JSON constant {name}")


def _no_dup_keys(pairs: list) -> dict:
    """A repeated key: Python keeps the last value and other readers (jq, Go, JS) may keep the
    first, so one line could verify here and say something else elsewhere. Refused."""
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _strict_loads(ln: bytes) -> Any:
    return json.loads(ln, object_pairs_hook=_no_dup_keys, parse_constant=_no_constants)


def _digest(obj: Any) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def _open_plain(p: Path, mode: str = "rb", create: bool = False):
    """The history log (or its lock/torn sidecar), opened as a regular file only.

    open() followed a symlink planted at the path and blocked forever on a FIFO there
    (capgate rounds 78/79): O_NOFOLLOW|O_NONBLOCK, then fstat must say regular."""
    flags = {"rb": os.O_RDONLY, "rb+": os.O_RDWR, "ab": os.O_WRONLY | os.O_APPEND}[mode] | _OPEN_FLAGS
    if create:
        flags |= os.O_CREAT
    fd = os.open(p, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{p.name} is not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _log_lock(p: Path):
    """One writer at a time across threads and processes (the round-79 fork lesson)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p) + ".lock", os.O_RDWR | os.O_CREAT | _OPEN_FLAGS, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("history.jsonl.lock is not a regular file")
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):  # no flock (Windows / odd FS): best effort
            pass
        yield
    finally:
        os.close(fd)


def _check_entry(e: Any, prev_digest: str | None, where: str, want_seq: int = 0) -> None:
    if not isinstance(e, dict):
        raise OSError(f"history log {where}: not an entry object — append-only, never rewritten")
    # round 46: seq was never checked. A digest-valid line with seq "x", true, 1.5 or Infinity made
    # the next append raise ValueError/OverflowError out of int(); a skipped or repeated seq passed.
    seq = e.get("seq")
    if type(seq) is not int or seq != want_seq:
        raise OSError(f"history log {where}: seq {seq!r} where {want_seq} was due — append-only, never rewritten")
    if _digest({k: v for k, v in e.items() if k != "digest"}) != e.get("digest"):
        raise OSError(f"history log {where}: digest mismatch (entry edited) — append-only, never rewritten")
    if e.get("prev_hash") != prev_digest:
        raise OSError(f"history log {where}: chain break (entry removed or reordered) — append-only, never rewritten")


def _cut_torn(p: Path, torn: bytes) -> None:
    """A crash mid-append left bytes after the last newline: keep them as evidence, cut them off."""
    size = p.stat().st_size
    with _open_plain(p, "rb+") as f:
        f.truncate(max(0, size - len(torn)))
        f.flush()
        os.fsync(f.fileno())
    with _open_plain(Path(str(p) + ".torn"), "ab", create=True) as t:
        t.write(torn + NL.encode())
        t.flush()
        os.fsync(t.fileno())


def _last_good(p: Path) -> dict | None:
    """The newest complete entry, healing a torn trailing fragment; failing closed on any
    middle corruption. The log is verified as it is read: digests recomputed, links checked."""
    if not p.exists():
        return None
    with _open_plain(p, "rb") as f:
        data = f.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise OSError(f"history log over {MAX_LOG_BYTES} bytes — refuse, name it, never guess")
    raw_lines = data.split(NL.encode())
    torn = b"" if data.endswith(NL.encode()) else (raw_lines.pop() if raw_lines else b"")
    prev_digest, last, n = None, None, 0
    for i, ln in enumerate(raw_lines, 1):
        if not ln.strip():
            continue
        try:
            e = _strict_loads(ln)
        except (ValueError, RecursionError):
            raise OSError(f"history log line {i} unreadable — append-only, never rewritten") from None
        _check_entry(e, prev_digest, f"line {i}", n)
        prev_digest, last, n = e["digest"], e, n + 1
    if torn and torn.strip():
        _cut_torn(p, torn)
    return last


def _residual(ok: bool) -> dict:
    """A scale-true residual row. The chain walk is deterministic: a pass is p = 1, S = 0
    (correlation-immune floor, Descent VIII); a fail is 1 failure in 1 trial, Jeffreys-smoothed
    p_J = (0 + 0.5)/(1 + 1) = 0.25, S = heat(0.25) = 0.1875. A per-process key makes the head
    unverifiable across restarts: that state is UNMEASURED and must be named, never implied."""
    key_shared = bool((os.environ.get("AVRONE_CAPGATE_KEY") or os.environ.get("AVRONE_FLEET_TOKEN") or "").strip())
    if not key_shared:
        return {"S": None, "orientation": "unmeasured", "cone": None}
    if ok:
        return {"S": 0.0, "orientation": "pass", "cone": False}
    return {"S": 0.1875, "orientation": "fail", "cone": False}


def _short(v: Any, empty: str, n: int = 200) -> str:
    """A note or error as text: bytes decoded, at most n characters, never cut inside a
    surrogate pair (a lone half is not encodable as UTF-8 by strict readers)."""
    if v is None or v == "":
        return empty
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    t = str(v)
    t = t.encode("utf-16", "surrogatepass").decode("utf-16", "replace")  # lone surrogates -> U+FFFD
    return t[:n]


def attest_head(data_dir: str | Path, *, note: str = "") -> dict:
    """Append one attested-history entry carrying capgate's signed head state. Zero-heat:
    deterministic checks only. The entry MINTS its second-cause status only when an
    independent verify pass of the log accepts it (authorship transmits, measurement compounds)."""
    dd = Path(data_dir)
    p = history_path(dd)
    walk = capgate.verify_chain(dd)
    ok = bool(walk.get("ok"))
    residual = _residual(ok)
    raw = {"count": int(walk.get("count") or 0), "head": str(walk.get("head") or GENESIS),
           "verify_ok": ok, "verify_error": (_short(walk.get("error"), "unknown") if not ok else None),
           "key_scope": ("shared" if residual["orientation"] != "unmeasured" else "per_process_unverifiable_across_restarts"),
           "head_file_present": capgate._head_path(capgate.receipts_path(dd)).exists(),
           "note": _short(note, "")}
    with _log_lock(p):
        prev = _last_good(p)
        entry = {"seq": (prev["seq"] + 1) if prev else 0, "ts": round(time.time(), 3),
                 "source": SOURCE, "kind": KIND, "digest": "", "provenance_depth": 1,
                 "residual": residual, "capability_id": None, "human_root": None,
                 "raw": raw, "prev_hash": (str(prev["digest"]) if prev else None)}
        entry["digest"] = _digest({k: v for k, v in entry.items() if k != "digest"})
        with _open_plain(p, "ab", create=True) as f:
            f.write(_canon(entry) + NL.encode())
            f.flush()
            os.fsync(f.fileno())
    return entry


def attest_if_enabled(data_dir: str | Path, *, note: str = "") -> dict | None:
    """The hook a runtime calls after capgate.record(): a no-op unless explicitly enabled."""
    if not enabled():
        return None
    return attest_head(data_dir, note=note)


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    import shutil
    import tempfile

    P = F = 0

    def record(name: str, ok: bool, detail: str = "") -> None:
        nonlocal P, F
        P, F = (P + 1, F) if ok else (P, F + 1)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    tmp = Path(tempfile.mkdtemp(prefix="capgate_attest_"))
    dd = tmp / "data"
    attested = tmp / "attested"
    try:
        os.environ.pop("AVRONE_CAPGATE_ATTEST", None)
        os.environ.pop("AVRONE_FLEET_TOKEN", None)
        record("off by default", not enabled())
        os.environ["AVRONE_CAPGATE_ATTEST"] = "on"
        os.environ["AVRONE_CAPGATE_KEY"] = "selftest-key"
        record("on when named", enabled())

        log = attested / "history.jsonl"
        os.environ["AVRONE_ATTESTED_HISTORY_LOG"] = str(log)
        record("empty chain walks ok", bool(capgate.verify_chain(dd).get("ok")))
        e0 = attest_head(dd, note="first")
        record("first entry seq 0", e0["seq"] == 0)
        record("first prev_hash null", e0["prev_hash"] is None)
        record("residual pass S=0.0", e0["residual"]["S"] == 0.0)
        record("residual orientation pass", e0["residual"]["orientation"] == "pass")
        record("provenance_depth 1 (measured ancestor)", e0["provenance_depth"] == 1)
        record("entry digest covers entry", _digest({k: v for k, v in e0.items() if k != "digest"}) == e0["digest"])

        capgate.record(dd, caller="selftest", tool="t", args={"a": 1}, outcome="exec", output="ok")
        capgate.record(dd, caller="selftest", tool="t", args={"a": 2}, outcome="refuse", output="denied")
        e1 = attest_head(dd, note="second")
        record("second seq 1", e1["seq"] == 1)
        record("second chains to first digest", e1["prev_hash"] == e0["digest"])
        record("count attested matches chain", e1["raw"]["count"] == 2)
        record("head attested matches chain", e1["raw"]["head"] == capgate.verify_chain(dd)["head"])
        record("verify_chain ok after bridge", capgate.verify_chain(dd)["ok"] is True)

        with log.open("ab") as f:
            f.write(b'{"seq": 2, "tor')  # crash mid-append
        e2 = attest_head(dd, note="after-torn")
        record("torn tail healed, seq continues", e2["seq"] == 2)
        record("torn evidence kept", (attested / "history.jsonl.torn").exists())

        lines = log.read_text().splitlines()
        # round 46: the log is compact JSON ("seq":0); '"seq": 0' never matched, so this check
        # edited nothing and failed
        lines[0] = lines[0].replace('"seq":0', '"seq":9')
        log.write_text(NL.join(lines) + NL)
        try:
            attest_head(dd, note="must-refuse")
            record("middle corruption fails closed", False)
        except OSError:
            record("middle corruption fails closed", True)

        fifo_log = attested / "fifo.jsonl"
        os.mkfifo(fifo_log)
        os.environ["AVRONE_ATTESTED_HISTORY_LOG"] = str(fifo_log)
        try:
            attest_head(dd, note="must-refuse")
            record("FIFO at log path refused", False)
        except OSError:
            record("FIFO at log path refused", True)

        target = tmp / "elsewhere.jsonl"
        target.write_text("x")
        sym_log = attested / "link.jsonl"
        os.symlink(target, sym_log)
        os.environ["AVRONE_ATTESTED_HISTORY_LOG"] = str(sym_log)
        try:
            attest_head(dd, note="must-refuse")
            record("symlink at log path refused", False)
        except OSError:
            record("symlink at log path refused", True)

        os.environ.pop("AVRONE_CAPGATE_KEY", None)
        os.environ["AVRONE_ATTESTED_HISTORY_LOG"] = str(attested / "unmeasured.jsonl")
        eu = attest_head(dd, note="per-process key")
        record("per-process key named unmeasured", eu["residual"]["orientation"] == "unmeasured" and eu["residual"]["S"] is None)
        record("key scope named", str(eu["raw"]["key_scope"]).startswith("per_process"))

        os.environ["AVRONE_CAPGATE_KEY"] = "selftest-key"
        os.environ["AVRONE_ATTESTED_HISTORY_LOG"] = str(attested / "failres.jsonl")
        rp = capgate.receipts_path(dd)
        rlines = rp.read_text().splitlines()
        rlines[0] = rlines[0].replace('"outcome":"exec"', '"outcome":"edited"')
        rp.write_text(NL.join(rlines) + NL)
        ef = attest_head(dd, note="forged receipt")
        record("forged chain measured fail", ef["residual"]["orientation"] == "fail")
        record("fail row heat is scale-true 0.1875", ef["residual"]["S"] == 0.1875)
        record("verify_error carried in raw", bool(ef["raw"]["verify_error"]))

        os.environ.pop("AVRONE_ATTESTED_HISTORY_LOG", None)
        attest_head(dd, note="default path")
        record("default path under data_dir", (dd / "attested" / "history.jsonl").exists())

        os.environ.pop("AVRONE_CAPGATE_ATTEST", None)
        record("attest_if_enabled is a no-op when off", attest_if_enabled(dd) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"=== capgate_head_attest selftest: {P} passed, {F} failed ===")
    return 0 if F == 0 else 1


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("--selftest", "-s"):
        return _selftest()
    if not argv:
        print(__doc__)
        return 2
    if not enabled():
        print("capgate head attestation is off (AVRONE_CAPGATE_ATTEST); nothing done, nothing implied")
        return 0
    try:
        e = attest_head(argv[0], note=(argv[1] if len(argv) > 1 else ""))
    except OSError as err:  # round 46: a refused log printed a traceback
        print(f"capgate head attestation refused: {err}", file=sys.stderr)
        return 1
    print(json.dumps({"seq": e["seq"], "digest": e["digest"], "residual": e["residual"], "raw": e["raw"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
