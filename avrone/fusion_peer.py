"""Fusion peer: the blackboard the relay reads, and a watchdog over the hops it uses.

This module does not replace ``avrone.bridge``. Squad posts stay on the per-run
bridge. ``GET/POST /api/blackboard`` is a fan-in in front of those bridges so
GreatScale-Avrone-Grok-Fusion-Bridge can poll this daemon and write mood and
health mirrors back. ``FusionWatch`` is the piece the relay does not have: one
cycle that reports a dead hop, a bad token, an oversized body, a duplicate, or
a peer restart, and does not raise when the bridge is down.

The watchdog dials only when a caller asks. Nothing here starts from import.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .bridge import KINDS, Bridge, BridgeError, _ID_RE, _clip, _refs, _remote_ts, for_core
from .safeio import FileTooBig, atomic_write, read_capped
from .textsafe import UNSAFE_RUN

FUSION_MAX_BODY = 4096
MAX_FORWARD = 200
MAX_DISK_BRIDGES = 256
_ORDER_NAME = "_fanin_order.json"
_STATE_CAP = 1_000_000
_MIRROR_PREFIXES = ("ck-mood-", "ck-health-", "ck-evidence-")

_GUARD_LOCK = threading.Lock()
# threading.RLock is a factory, not a type. isinstance needs the instance type.
_RLOCK_TYPE = type(threading.RLock())
_last_watch: dict[str, Any] | None = None
_shared: "FusionWatch | None" = None
_shared_key: str | None = None
_shared_lock = threading.Lock()


def last_watch() -> dict[str, Any] | None:
    """The last ``watch_once`` report, or None if this process has not watched."""
    if _last_watch is None:
        return None
    return dict(_last_watch)


def shared_watch(state_path: str | None = None) -> "FusionWatch":
    """The process-wide watchdog. A different state path starts a fresh one."""
    global _shared, _shared_key
    key = state_path or ""
    with _shared_lock:
        if _shared is None or _shared_key != key:
            _shared = FusionWatch(state_path)
            _shared_key = key
        return _shared


def normalize_post(data: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a ``POST /api/blackboard`` object. ``(post, None)`` or ``(None, detail)``."""
    if not isinstance(data, dict):
        return None, "body must be JSON"
    kind = str(data.get("kind") or "finding").strip().lower()
    if kind not in KINDS:
        return None, f"kind must be one of {', '.join(KINDS)}"
    text = _clip(data.get("text"))
    if not text:
        return None, "text is required"
    raw_id = data.get("id")
    if raw_id in (None, ""):
        pid = uuid.uuid4().hex[:12]
    elif not isinstance(raw_id, str) or not _ID_RE.match(raw_id):
        # A JSON number or bool is not an id. str(True) is "True", which would
        # collide with a real post and hide the type error.
        return None, "bad post id"
    else:
        pid = raw_id
    run_id = str(data.get("run_id") or "run")[:80] or "run"
    return {
        "id": pid,
        "run_id": run_id,
        "agent": str(data.get("agent") or "agent")[:80],
        "role": str(data.get("role") or "")[:60],
        "kind": kind,
        "text": text,
        "refs": _refs(data.get("refs")),
        "ts": _remote_ts(data.get("ts")),
        "origin_node": str(data.get("origin_node") or "local")[:80],
    }, None


def _data_dir(core: Any) -> Path | None:
    settings = getattr(core, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    return None if data_dir is None else Path(data_dir)


def _guard(core: Any) -> threading.RLock:
    guard = getattr(core, "_fusion_board_guard", None)
    if isinstance(guard, _RLOCK_TYPE):
        return guard
    with _GUARD_LOCK:
        guard = getattr(core, "_fusion_board_guard", None)
        if not isinstance(guard, _RLOCK_TYPE):
            guard = threading.RLock()
            try:
                core._fusion_board_guard = guard
            except AttributeError:
                return _GUARD_LOCK
        return guard


def _iter_bridges(core: Any) -> list[Bridge]:
    """Open run bridges, then persisted journals the cache has dropped."""
    guard = getattr(core, "_bridge_guard", None)
    if guard is None:
        cached = list((getattr(core, "_bridges", None) or {}).values())
    else:
        with guard:
            cached = list((getattr(core, "_bridges", None) or {}).values())
    seen: set[str] = set()
    out: list[Bridge] = []
    for bridge in cached:
        if not isinstance(bridge, Bridge):
            continue
        out.append(bridge)
        if bridge.path is not None:
            try:
                seen.add(str(bridge.path.resolve()))
            except OSError:
                pass
    data_dir = _data_dir(core)
    if data_dir is None:
        return out
    folder = data_dir / "bridge"
    if not folder.is_dir():
        return out
    try:
        paths = sorted(folder.glob("*.jsonl"))
    except OSError:
        return out
    opened = 0
    for path in paths:
        if opened >= MAX_DISK_BRIDGES:
            break
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen or path.is_symlink() or not path.is_file():
            continue
        try:
            out.append(Bridge(path.stem, data_dir))
        except (OSError, ValueError):
            continue
        seen.add(resolved)
        opened += 1
    return out


def _posts_by_id(core: Any) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for bridge in _iter_bridges(core):
        try:
            posts = bridge.posts()
        except (OSError, ValueError, TypeError):
            continue
        for post in posts:
            by_id.setdefault(post["id"], post)
    return by_id


def _id_state(core: Any, pid: str) -> str | None:
    """``stored`` when a ring still has the id, ``evicted`` when only a tombstone does."""
    evicted = False
    for bridge in _iter_bridges(core):
        lock = getattr(bridge, "_lock", None)
        posts = getattr(bridge, "_posts", {})
        tombstones = getattr(bridge, "_evicted", {})
        if lock is None:
            continue
        with lock:
            if pid in posts:
                return "stored"
            if pid in tombstones:
                evicted = True
    return "evicted" if evicted else None


def _ts_key(post: dict[str, Any]) -> tuple[float, str]:
    ts = post.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        number = 0.0
    else:
        try:
            number = float(ts)
        except (OverflowError, ValueError):
            number = 0.0
        if not math.isfinite(number):
            number = 0.0
    return (number, str(post.get("id") or ""))


def _order_path(core: Any) -> Path | None:
    data_dir = _data_dir(core)
    if data_dir is None:
        return None
    return data_dir / "bridge" / _ORDER_NAME


def _load_order(core: Any) -> "OrderedDict[str, None]":
    cached = getattr(core, "_fusion_board_order", None)
    if isinstance(cached, OrderedDict):
        return cached
    order: "OrderedDict[str, None]" = OrderedDict()
    path = _order_path(core)
    if path is not None:
        try:
            raw = read_capped(path, _STATE_CAP)
        except OSError:
            raw = None
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                data = None
            ids = data.get("ids") if isinstance(data, dict) else None
            if isinstance(ids, list):
                for item in ids:
                    if isinstance(item, str) and _ID_RE.match(item) and item not in order:
                        order[item] = None
    try:
        core._fusion_board_order = order
    except AttributeError:
        pass
    return order


def _save_order(core: Any, order: "OrderedDict[str, None]") -> None:
    path = _order_path(core)
    if path is None:
        return
    try:
        if path.is_symlink():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"ids": list(order)}))
    except OSError:
        return


def _sync_order(core: Any, by_id: dict[str, dict[str, Any]]) -> "OrderedDict[str, None]":
    """Drop gone ids. The first snapshot is oldest-ts-first; later ids append.

    Appending is what keeps ``since`` honest: a post accepted after the cursor
    is returned even when its timestamp is older than the cursor.
    """
    order = _load_order(core)
    changed = False
    for pid in list(order):
        if pid not in by_id:
            order.pop(pid, None)
            changed = True
    fresh = [pid for pid in by_id if pid not in order]
    if fresh:
        if not order:
            fresh = sorted(by_id, key=lambda pid: _ts_key(by_id[pid]))
        else:
            fresh.sort(key=lambda pid: _ts_key(by_id[pid]))
        for pid in fresh:
            order[pid] = None
        changed = True
    if changed:
        _save_order(core, order)
    return order


def blackboard_posts(core: Any, since: str | None = None) -> list[dict[str, Any]]:
    """Fan-in of every run bridge, oldest first. ``since`` matches ``Bridge.export``."""
    with _guard(core):
        by_id = _posts_by_id(core)
        order = _sync_order(core, by_id)
        posts = [dict(by_id[pid]) for pid in order if pid in by_id]
        if since:
            ids = [post["id"] for post in posts]
            if since in ids:
                posts = posts[ids.index(since) + 1:]
        return posts


def blackboard_accept(core: Any, post: dict[str, Any]) -> tuple[bool, str]:
    """Store ``post`` on its run bridge. Return ``(duplicate, id)``.

    A stored id and an id the ring has already evicted are duplicates. Neither
    is rewritten, and an evicted id is not put back.
    """
    pid = str(post.get("id") or "")
    if not _ID_RE.match(pid):
        raise BridgeError("bad post id")
    with _guard(core):
        if _id_state(core, pid) is not None:
            return True, pid
        try:
            dest = for_core(core, str(post.get("run_id") or "run"))
            stats = dest.merge_remote([post], str(post.get("origin_node") or "local"))
        except OSError as exc:
            raise BridgeError("post rejected") from exc
        if stats.get("merged") == 1:
            _sync_order(core, _posts_by_id(core))
            return False, pid
        if stats.get("skipped"):
            return True, pid
        raise BridgeError("post rejected")


def _dumps(body: Any) -> bytes | None:
    """UTF-8 JSON, or None when the value cannot be serialized.

    Lone surrogates are escaped (``ensure_ascii``) so ``encode`` cannot raise.
    NaN and circular references are refused instead of emitting non-JSON.
    """
    try:
        text = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)
        return text.encode("utf-8")
    except UnicodeEncodeError:
        try:
            text = json.dumps(body, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str)
            return text.encode("ascii")
        except (ValueError, TypeError, UnicodeEncodeError):
            return None
    except (ValueError, TypeError):
        return None


def encode_bounded(body: dict, limit: int = FUSION_MAX_BODY) -> bytes | None:
    """UTF-8 JSON of at most ``limit`` bytes, or None when it cannot be shrunk.

    Only ``payload.text`` is shortened. A cut is marked with ``...``. The count
    is the serialized document, escapes included, not the Python character length.
    """
    if not isinstance(body, dict) or limit < 1:
        return None
    raw = _dumps(body)
    if raw is None:
        return None
    if len(raw) <= limit:
        return raw
    payload = body.get("payload")
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        return None
    if len(text) > limit:
        text = text[:limit]
    lo, hi = 0, len(text)
    best: bytes | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid < len(text):
            cut = text[: mid - 3] + "..." if mid >= 3 else "..."
        else:
            cut = text
        trial = dict(body)
        trial_payload = dict(payload)
        trial_payload["text"] = cut
        trial["payload"] = trial_payload
        raw = _dumps(trial)
        if raw is not None and cut and len(raw) <= limit:
            best = raw
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def boot_value(body: Any) -> str:
    """``boot`` from a health or blackboard read. Missing, blank, or junk is empty."""
    if not isinstance(body, dict) or "boot" not in body:
        return ""
    value = body["boot"]
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return ""
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or any(char in text for char in "\r\n\x00"):
        return ""
    return text[:80]


def is_echo(post: dict) -> bool:
    """A mirror this relay (or the watchdog) already wrote. Do not forward it again."""
    origin = str(post.get("origin_node") or "")
    role = str(post.get("role") or "")
    agent = str(post.get("agent") or "")
    pid = str(post.get("id") or "")
    if origin == "fusion-bridge" or role == "cyclekernal-relay":
        return True
    if agent.lower().startswith("cyclekernal:"):
        return True
    if pid.startswith(_MIRROR_PREFIXES):
        return True
    text = post.get("text")
    if isinstance(text, str) and '"agent":"cyclekernal:' in text.replace(" ", ""):
        return True
    return False


def posts_from(body: Any) -> list[dict[str, Any]] | None:
    """Posts from a blackboard object, or a bare array. None when the read is malformed."""
    if isinstance(body, list):
        return [post for post in body if isinstance(post, dict)]
    if isinstance(body, dict) and isinstance(body.get("posts"), list):
        return [post for post in body["posts"] if isinstance(post, dict)]
    return None


def _event_ts(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return time.time()
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return time.time()
    if not math.isfinite(number) or number <= 0:
        return time.time()
    return number


def event_from_post(post: dict) -> dict[str, Any] | None:
    """The ``POST /v1/events`` document for one blackboard post."""
    pid = post.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        return None
    text = post.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    kind = str(post.get("kind") or "finding").strip().lower()
    if kind not in KINDS:
        kind = "finding"
    refs: list[str] = []
    raw_refs = post.get("refs")
    if isinstance(raw_refs, list):
        for item in raw_refs:
            if isinstance(item, str) and _ID_RE.match(item) and item not in refs:
                refs.append(item)
            if len(refs) >= 20:
                break
    run_id = str(post.get("run_id") or "run")[:80] or "run"
    return {
        "id": pid,
        "source": "avrone",
        "kind": kind,
        "run_id": run_id,
        "payload": {
            "id": pid,
            "agent": str(post.get("agent") or "agent")[:80],
            "role": str(post.get("role") or "")[:60],
            "kind": kind,
            "text": text,
            "refs": refs,
            "origin_node": str(post.get("origin_node") or "")[:80],
        },
        "ts": _event_ts(post.get("ts")),
    }


def _header_value(value: str) -> str:
    """A header value, or empty when it cannot be sent as one.

    HTTP headers are latin-1. The same unsafe class ``textsafe`` uses for one-line
    display (C0, C1, DEL, NEL, U+2028/2029, bidi controls) is rejected here too:
    U+0085 is latin-1 and would otherwise be sent as a header line break.
    A character outside latin-1 used to raise while dialing and look like an outage.
    """
    text = str(value or "")
    if not text or UNSAFE_RUN.search(text):
        return ""
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return ""
    return text


def _idempotency_key(value: str) -> str:
    key = _header_value(str(value or "").strip())
    return key[:200]


def _status_error(code: int) -> str:
    if code in (401, 403):
        return "auth"
    if code == 413:
        return "oversized"
    if code in (404, 405, 408, 429):
        # 408/429 are temporary. Remembering them would drop the post.
        return "missing" if code in (404, 405) else "retry"
    if 300 <= code < 400:
        return "redirect"
    if code >= 500:
        return "upstream"
    if code >= 400:
        return "rejected"
    return "ok"


def _call_timeout(deadline_at: float | None, timeout: float) -> float | None:
    """Seconds for the next dial, or None when this cycle must not start one."""
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = 5.0
    timeout = min(30.0, float(timeout))
    if deadline_at is None:
        return timeout
    left = deadline_at - time.monotonic()
    if left <= 0.05:
        return None
    return min(timeout, left)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 302 must not carry ``X-Admin-Token`` to the Location host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _url_closed(url: str) -> bool:
    """True when the URL must not be dialed.

    Userinfo would put a secret on the wire in a place the admin token does
    not belong. An empty host is not a peer.
    """
    if not url.startswith(("http://", "https://")) or UNSAFE_RUN.search(url) or any(char in url for char in " \t"):
        return True
    parts = urllib.parse.urlsplit(url)
    return bool(parts.username or parts.password or not parts.hostname)


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _exchange(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    token: str = "",
    idempotency_key: str = "",
    timeout: float = 5.0,
    max_read: int = 2_000_000,
) -> dict[str, Any]:
    if _url_closed(url):
        return {"ok": False, "status": None, "error": "down", "body": None}
    if token and not _header_value(token):
        # Do not dial. A CR/LF in the token is header injection, not a wrong password.
        return {"ok": False, "status": None, "error": "auth", "body": None}
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = 5.0
    timeout = min(30.0, max(0.05, float(timeout)))
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    safe_token = _header_value(token)
    if safe_token:
        headers["X-Admin-Token"] = safe_token
    safe_key = _header_value(idempotency_key)
    if safe_key:
        headers["Idempotency-Key"] = safe_key
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
    except UnicodeEncodeError:
        return {"ok": False, "status": None, "error": "auth", "body": None}
    except Exception as exc:  # noqa: BLE001 - a dead hop is a report, not a crash
        reason = getattr(exc, "reason", exc)
        error = "timeout" if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) else "down"
        return {"ok": False, "status": None, "error": error, "body": None}
    try:
        with response as resp:
            status = int(getattr(resp, "status", None) or getattr(resp, "code", 0) or 0)
            raw = resp.read(max_read + 1)
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "reason", exc)
        error = "timeout" if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) else "down"
        return {"ok": False, "status": None, "error": error, "body": None}
    if len(raw) > max_read:
        # The peer answered, but not with a body we will buffer. Retry later.
        # HTTP 413 is a small JSON error and is handled below as permanent.
        return {"ok": False, "status": status, "error": "truncated", "body": None}
    parsed: Any = None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        parsed = None
    if status >= 400 or (300 <= status < 400):
        return {"ok": False, "status": status, "error": _status_error(status), "body": parsed}
    if 200 <= status < 300:
        if parsed is None:
            return {"ok": False, "status": status, "error": "malformed", "body": None}
        return {"ok": True, "status": status, "error": None, "body": parsed}
    return {"ok": False, "status": status or None, "error": "down", "body": parsed}


class FusionWatch:
    """One supervisor for the relay's two hops.

    Ids are remembered only after a 2xx or a permanent 4xx (400, 413, other
    non-auth 4xx). Timeouts, connection failures, 401/403, 404/405, and 5xx
    leave the id unseen so a later cycle retries it. A peer ``boot`` change
    forgets what was delivered to that peer. The first boot value does not.
    """

    def __init__(self, state_path: str | os.PathLike | None = None):
        self.path = str(state_path) if state_path else ""
        self.seen: set[str] = set()
        self.boots: dict[str, str] = {}
        self.state_errors = 0
        self._version = 1
        self._cursor = ""
        self._writable = True
        self._breaker_threshold = 0
        self._breaker_cooldown = 30.0
        self._fail_counts: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._probing: dict[str, bool] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            raw = read_capped(self.path, _STATE_CAP)
        except FileTooBig:
            # Leave the file in place. Replacing it would throw away ids we did not read.
            self.state_errors += 1
            self._writable = False
            return
        if raw is None:
            if os.path.lexists(self.path):
                # Symlink, FIFO, directory, or other non-regular file. Do not follow it
                # and do not replace it.
                self.state_errors += 1
                self._writable = False
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self.state_errors += 1
            return
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            self.state_errors += 1
            self._writable = False
            return
        seen = data.get("seen")
        boots = data.get("boots")
        if not isinstance(seen, list) or not isinstance(boots, dict):
            self.state_errors += 1
            self._writable = False
            return
        self._version = data["version"]
        cursor = data.get("cursor")
        if isinstance(cursor, str) and not any(char in cursor for char in "\r\n\x00"):
            self._cursor = cursor[:80]
        self.seen = {item for item in seen if isinstance(item, str) and _ID_RE.match(item)}
        for side in ("avrone", "cyclekernal"):
            value = boots.get(side)
            if isinstance(value, str) and value.strip() and not any(c in value for c in "\r\n\x00"):
                self.boots[side] = value.strip()[:80]

    def _save(self) -> bool:
        if not self.path:
            return True
        if not self._writable or os.path.islink(self.path):
            return False
        payload = {
            "version": self._version if self._version in (1, 2) else 1,
            "seen": sorted(self.seen),
            "boots": {side: self.boots.get(side, "") for side in ("avrone", "cyclekernal")},
        }
        if payload["version"] == 2:
            payload["cursor"] = self._cursor
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(payload))
            return True
        except OSError:
            self.state_errors += 1
            return False

    def _circuit_blocked(self, url: str) -> bool:
        """Skip a host that timed out or refused a connection.

        Threshold 0 disables the breaker. A response, including a 500, does not
        open it. After the cooldown one probe is allowed.
        """
        if self._breaker_threshold <= 0:
            return False
        host = _origin(url)
        until = self._open_until.get(host)
        if until is None:
            return False
        if time.monotonic() >= until:
            if self._probing.get(host):
                return True
            self._probing[host] = True
            return False
        return True

    def _circuit_observe(self, url: str, error: str | None) -> None:
        if self._breaker_threshold <= 0:
            return
        host = _origin(url)
        if error is None:
            self._fail_counts.pop(host, None)
            self._open_until.pop(host, None)
            self._probing.pop(host, None)
            return
        if error not in ("timeout", "down"):
            return
        self._probing[host] = False
        count = self._fail_counts.get(host, 0) + 1
        self._fail_counts[host] = count
        if host in self._open_until or count >= self._breaker_threshold:
            self._open_until[host] = time.monotonic() + max(0.0, self._breaker_cooldown)

    def _dial(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if self._circuit_blocked(url):
            return {"ok": False, "status": None, "error": "circuit", "body": None}
        hop = _exchange(method, url, **kwargs)
        self._circuit_observe(url, None if hop["ok"] else hop["error"])
        return hop

    def note_boot(self, side: str, boot: str) -> bool:
        """Remember ``boot``. Return True when a *changed* boot forgot delivered ids.

        An empty boot leaves the saved state alone, so an older peer does not
        look like a restart on every poll. The first value is stored and does
        not resend.
        """
        if side not in ("avrone", "cyclekernal") or not boot:
            return False
        previous = self.boots.get(side, "")
        if previous == boot:
            return False
        self.boots[side] = boot
        if not previous:
            return False
        if side == "cyclekernal":
            self.seen = {item for item in self.seen if item.startswith(_MIRROR_PREFIXES)}
        else:
            self.seen = {item for item in self.seen if not item.startswith(_MIRROR_PREFIXES)}
        return True

    def submit(
        self,
        url: str,
        token: str,
        body: dict,
        event_id: str,
        *,
        timeout: float = 5.0,
        max_read: int = 2_000_000,
        limit: int = FUSION_MAX_BODY,
    ) -> dict[str, Any]:
        """POST one event. Never raises. Does not dial when the body cannot fit."""
        with self._lock:
            if event_id in self.seen:
                return {"ok": True, "error": None, "duplicate": True, "status": None,
                        "remembered": True, "skipped": True}
            raw = encode_bounded(body, limit)
            if raw is None:
                if _ID_RE.match(event_id):
                    self.seen.add(event_id)
                return {"ok": False, "error": "oversized", "duplicate": False, "status": None,
                        "remembered": True, "skipped": False}
            if self._circuit_blocked(url):
                return {"ok": False, "error": "circuit", "duplicate": False, "status": None,
                        "remembered": False, "skipped": False}
            hop = _exchange(
                "POST", url, body=raw, token=token,
                idempotency_key=_idempotency_key(event_id),
                timeout=timeout, max_read=max_read,
            )
            self._circuit_observe(url, None if hop["ok"] else hop["error"])
            duplicate = False
            remembered = False
            if hop["ok"]:
                parsed = hop["body"]
                duplicate = isinstance(parsed, dict) and parsed.get("duplicate") is True
                remembered = True
                error = None
            else:
                error = hop["error"] or "down"
                if error in ("oversized", "rejected"):
                    remembered = True
            if remembered and _ID_RE.match(event_id):
                self.seen.add(event_id)
            return {"ok": hop["ok"], "error": error, "duplicate": duplicate, "status": hop["status"],
                    "remembered": remembered, "skipped": False}

    def watch_once(
        self,
        *,
        board_url: str,
        events_url: str,
        health_url: str,
        board_token: str = "",
        events_token: str = "",
        timeout: float = 5.0,
        max_read: int = 2_000_000,
        deadline: float | None = None,
        breaker_threshold: int = 0,
        breaker_cooldown: float = 30.0,
    ) -> dict[str, Any]:
        """Probe health and the blackboard, then forward new posts. Never raises.

        ``deadline`` is seconds for the whole cycle. ``None``, ``0``, or a
        non-finite value disables it. A call is not started with under 50 ms left,
        so one slow hop cannot bill a full timeout for every queued post.
        """
        global _last_watch
        if deadline is not None and math.isfinite(deadline) and deadline > 0:
            deadline_at: float | None = time.monotonic() + min(120.0, float(deadline))
        else:
            deadline_at = None
        try:
            threshold = int(breaker_threshold)
        except (TypeError, ValueError):
            threshold = 0
        self._breaker_threshold = 0 if threshold < 0 else min(100, threshold)
        try:
            cooldown = float(breaker_cooldown)
        except (TypeError, ValueError):
            cooldown = 30.0
        self._breaker_cooldown = 30.0 if not math.isfinite(cooldown) else min(600.0, max(0.0, cooldown))
        with self._lock:
            health_timeout = _call_timeout(deadline_at, timeout)
            if health_timeout is None:
                health = {"ok": False, "status": None, "error": "timeout", "body": None}
            else:
                health = self._dial("GET", health_url, timeout=health_timeout, max_read=max_read)
            restart = False
            if health["ok"]:
                restart = self.note_boot("cyclekernal", boot_value(health["body"])) or restart
            board_timeout = _call_timeout(deadline_at, timeout)
            if board_timeout is None:
                board = {"ok": False, "status": None, "error": "timeout", "body": None}
            else:
                board = self._dial(
                    "GET", board_url, token=board_token, timeout=board_timeout, max_read=max_read)
            if board["ok"]:
                restart = self.note_boot("avrone", boot_value(board["body"])) or restart
            forwarded = duplicates = skipped = errors = 0
            if not health["ok"]:
                errors += 1
            board_label = "ok"
            if not board["ok"]:
                board_label = board["error"] or "down"
                errors += 1
            else:
                posts = posts_from(board["body"])
                if posts is None:
                    board_label = "malformed"
                    errors += 1
                else:
                    sent = 0
                    for post in posts:
                        if sent >= MAX_FORWARD:
                            skipped += 1
                            continue
                        call_timeout = _call_timeout(deadline_at, timeout)
                        if call_timeout is None:
                            skipped += 1
                            continue
                        try:
                            event = None if is_echo(post) else event_from_post(post)
                        except Exception:  # noqa: BLE001 - one bad row must not kill the cycle
                            skipped += 1
                            continue
                        if event is None:
                            skipped += 1
                            continue
                        if event["id"] in self.seen:
                            skipped += 1
                            continue
                        try:
                            result = self.submit(
                                events_url, events_token, event, event["id"],
                                timeout=call_timeout, max_read=max_read,
                            )
                        except Exception:  # noqa: BLE001
                            errors += 1
                            break
                        if not result.get("skipped"):
                            sent += 1
                        if result.get("duplicate"):
                            duplicates += 1
                        elif result.get("ok"):
                            forwarded += 1
                        else:
                            errors += 1
                            if result.get("error") in (
                                "down", "timeout", "auth", "upstream", "missing", "retry", "truncated", "circuit"):
                                break
            if not self._save():
                errors += 1
            report = {
                "watched": True,
                "board": board_label,
                "health": "ok" if health["ok"] else (health["error"] or "down"),
                "forwarded": forwarded,
                "duplicates": duplicates,
                "skipped": skipped,
                "errors": errors,
                "restart": restart,
                "state_errors": self.state_errors,
            }
            _last_watch = dict(report)
            return report
