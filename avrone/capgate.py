"""capgate: capability-scoped tool calls + a tamper-evident receipt chain (opt-in).

This is the first piece of the capability-authority control plane folded into AVRONE itself.
capability-authority's hard rules carry over, scaled down to one node:

  * no ambient power: with the gate on, an agent runs a tool only when it holds a capability
    whose rights cover that tool;
  * amplification forbidden: a child agent's capability is an ATTENUATION of its parent's
    (a subset of rights, never a later expiry, delegation depth + 1);
  * every executed or refused act leaves a receipt, and receipts form a hash chain signed
    with the node key, so editing, reordering or deleting one line is detectable.

Off by default (AVRONE_CAPGATE=off) so existing deployments behave exactly as before.

Env:
  AVRONE_CAPGATE          off | audit | enforce   (audit = receipts only, never refuses)
  AVRONE_CAPGATE_KEY      HMAC key for capabilities + receipts (defaults to AVRONE_FLEET_TOKEN,
                          else a random per-process key, which makes receipts unverifiable
                          across restarts; set it in production)
  AVRONE_CAPGATE_POLICY   JSON {"<role or agent name>": ["tool", "glob*", ...], "*": [...]}
                          the rights a ROOT agent is issued; default {"*": ["*"]}
  AVRONE_CAPGATE_TTL      seconds a root capability lives (default 3600, 30..86400)
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MODES = ("off", "audit", "enforce")
MAX_RIGHTS = 64
MAX_DEPTH = 16
GENESIS = "0" * 64
_PROC_KEY = secrets.token_hex(32)
_LOCK = threading.Lock()


class CapDenied(PermissionError):
    pass


def mode() -> str:
    m = (os.environ.get("AVRONE_CAPGATE") or "off").strip().lower()
    return m if m in MODES else "off"


def _key() -> bytes:
    k = (os.environ.get("AVRONE_CAPGATE_KEY") or os.environ.get("AVRONE_FLEET_TOKEN") or "").strip()
    return (k or _PROC_KEY).encode()


def ttl() -> int:
    try:
        n = int(os.environ.get("AVRONE_CAPGATE_TTL", "3600"))
    except (TypeError, ValueError):
        n = 3600
    return max(30, min(n, 86400))


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()


def _mac(obj: Any) -> str:
    return hmac.new(_key(), _canon(obj), hashlib.sha256).hexdigest()


def _clean_rights(rights: Any) -> list[str]:
    if isinstance(rights, str):
        rights = [rights]
    if not isinstance(rights, (list, tuple)):
        return []
    out: list[str] = []
    for r in rights:
        if isinstance(r, str) and r.strip() and len(r) <= 128 and r.strip() not in out:
            out.append(r.strip())
        if len(out) >= MAX_RIGHTS:
            break
    return out


def policy() -> dict[str, list[str]]:
    raw = os.environ.get("AVRONE_CAPGATE_POLICY", "")
    try:
        obj = json.loads(raw) if raw.strip() else {"*": ["*"]}
    except (json.JSONDecodeError, RecursionError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    # a malformed policy grants NOTHING (fail closed), never everything
    return {str(k): _clean_rights(v) for k, v in obj.items()}


def rights_for(name: str | None, role: str | None = None) -> list[str]:
    p = policy()
    for k in (name, role):
        if k and k in p:
            return p[k]
    return p.get("*", [])


# ------------------------------------------------------------------ capabilities
def issue(audience: str, rights: Any, ttl_s: int | None = None, run_id: str = "") -> dict[str, Any]:
    now = time.time()
    cap = {"id": uuid.uuid4().hex, "audience": str(audience or "*")[:120], "rights": _clean_rights(rights),
           "issued_at": now, "expires_at": now + (ttl_s if ttl_s is not None else ttl()),
           "parent_id": None, "depth": 0, "run_id": str(run_id or "")[:80]}
    cap["sig"] = _mac(cap)
    return cap


def _covers(granted: list[str], want: str) -> bool:
    return any(g == want or fnmatch.fnmatchcase(want, g) for g in granted)


def attenuate(cap: dict[str, Any], audience: str, rights: Any = None) -> dict[str, Any]:
    """A child capability: rights must each be covered by the parent's; expiry never extends."""
    ok, why = check_sig(cap)
    if not ok:
        raise CapDenied(f"parent capability invalid: {why}")
    parent_rights = list(cap.get("rights") or [])
    want = parent_rights if rights is None else _clean_rights(rights)
    for r in want:
        # a glob is only allowed through if the parent holds the identical glob or "*"
        if not (r in parent_rights or "*" in parent_rights or
                (not any(ch in r for ch in "*?[") and _covers(parent_rights, r))):
            raise CapDenied(f"amplification forbidden: {r!r} is not within the parent's rights")
    depth = int(cap.get("depth") or 0) + 1
    if depth > MAX_DEPTH:
        raise CapDenied("delegation too deep")
    child = {"id": uuid.uuid4().hex, "audience": str(audience or "*")[:120], "rights": want,
             "issued_at": time.time(), "expires_at": float(cap["expires_at"]),
             "parent_id": cap.get("id"), "depth": depth, "run_id": cap.get("run_id", "")}
    child["sig"] = _mac(child)
    return child


def check_sig(cap: Any) -> tuple[bool, str]:
    if not isinstance(cap, dict) or not isinstance(cap.get("sig"), str):
        return False, "not a capability"
    body = {k: v for k, v in cap.items() if k != "sig"}
    if not hmac.compare_digest(_mac(body), cap["sig"]):
        return False, "bad signature"
    return True, ""


def verify(cap: Any, tool: str, caller: str, now: float | None = None) -> tuple[bool, str]:
    ok, why = check_sig(cap)
    if not ok:
        return False, why
    t = time.time() if now is None else now
    try:
        if t > float(cap["expires_at"]):
            return False, "expired"
    except (KeyError, TypeError, ValueError):
        return False, "no expiry"
    if cap.get("audience") not in (caller, "*"):
        return False, f"capability is for {cap.get('audience')!r}, not {caller!r}"
    if not _covers(list(cap.get("rights") or []), tool):
        return False, f"no right to {tool!r}"
    return True, ""


# ------------------------------------------------------------------ receipts
def _digest(v: Any) -> str:
    return hashlib.sha256(_canon(v)).hexdigest()


def receipts_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "capgate" / "receipts.jsonl"


def _last_hash(p: Path) -> str:
    if not p.exists():
        return GENESIS
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().splitlines()
        for ln in reversed(lines):
            try:
                return json.loads(ln)["hash"]
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return GENESIS


def record(data_dir: str | Path, *, caller: str, tool: str, args: Any, outcome: str,
           output: str = "", cap_id: str | None = None, run_id: str = "") -> dict[str, Any]:
    """Append one receipt to the chain. Stores digests of args/output, never their contents."""
    p = receipts_path(data_dir)
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "caller": str(caller)[:120], "tool": str(tool)[:120],
               "outcome": str(outcome)[:40], "args_sha": _digest(args), "out_sha": _digest(output),
               "cap": cap_id, "run": str(run_id)[:80], "prev": _last_hash(p)}
        rec["hash"] = _digest(rec)
        rec["sig"] = _mac(rec)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return rec


def verify_chain(data_dir: str | Path) -> dict[str, Any]:
    """Walk the chain: every line parses, links to the previous hash, hashes and signs correctly."""
    p = receipts_path(data_dir)
    if not p.exists():
        return {"ok": True, "count": 0, "head": GENESIS}
    prev, n = GENESIS, 0
    with open(p, "rb") as f:
        for i, raw in enumerate(f, 1):
            try:
                rec = json.loads(raw)
                body = {k: v for k, v in rec.items() if k not in ("hash", "sig")}
                if rec["prev"] != prev:
                    return {"ok": False, "count": n, "line": i, "error": "broken link (line removed or reordered)"}
                if _digest(body) != rec["hash"]:
                    return {"ok": False, "count": n, "line": i, "error": "hash mismatch (line edited)"}
                if not hmac.compare_digest(_mac({**body, "hash": rec["hash"]}), str(rec["sig"])):
                    return {"ok": False, "count": n, "line": i, "error": "bad signature (forged or wrong key)"}
            except (ValueError, KeyError, TypeError):
                return {"ok": False, "count": n, "line": i, "error": "unreadable line"}
            prev, n = rec["hash"], n + 1
    return {"ok": True, "count": n, "head": prev}


# ------------------------------------------------------------------ the gate
def capability_for(ctx: Any) -> dict[str, Any]:
    """The context's capability, minting a root one from policy on first use."""
    cap = getattr(ctx, "capability", None)
    if cap is None:
        name = getattr(ctx, "name", "") or "agent"
        cap = issue(name, rights_for(name, getattr(ctx, "role", None)), run_id=getattr(ctx, "run_id", ""))
        try:
            ctx.capability = cap
        except AttributeError:
            pass
    return cap


def gate(ctx: Any, tool: str) -> tuple[bool, str, str | None]:
    """(allowed, reason, cap_id). In audit mode a failing check is reported but allowed."""
    m = mode()
    if m == "off":
        return True, "", None
    cap = capability_for(ctx)
    ok, why = verify(cap, tool, getattr(ctx, "name", "") or "agent")
    if ok or m == "audit":
        return True, ("" if ok else f"audit: {why}"), cap.get("id")
    return False, why, cap.get("id")


def data_dir_of(ctx: Any) -> Path | None:
    core = getattr(ctx, "core", None)
    s = getattr(core, "settings", None)
    d = getattr(s, "data_dir", None)
    # only a real path: a mocked or misconfigured settings object must never become a directory name
    return Path(d) if isinstance(d, (str, Path)) and str(d) else None


def attenuate_for_child(parent: dict[str, Any], child_name: str, role: str | None = None) -> dict[str, Any]:
    """The child's capability: the policy's rights for its name/role when the parent covers them,
    else the parent's own rights. Never more than the parent (attenuate enforces it)."""
    p = policy()
    if child_name in p or (role and role in p):
        try:
            return attenuate(parent, child_name, rights_for(child_name, role))
        except CapDenied:
            pass  # policy would amplify: fall back to the parent's rights
    return attenuate(parent, child_name, None)
