"""Fan-in blackboard for the fusion relay.

``GET/POST /api/blackboard`` is the HTTP seam GreatScale's relay polls. Posts
stay on the run bridge (``merge_remote``: caller id, unknown refs allowed,
evicted ids not resurrected). The fan-in order is append-only after the first
snapshot so a ``since`` cursor cannot skip a post that arrived later with an
older timestamp.

Dedup on this route is the post id, first write wins. CycleKernal's event
store dedups ``(source, id)`` instead; that key lives on the outbound ledger
in ``fusion_deliver``, not in this ring. Two runs cannot store the same id.
"""
from __future__ import annotations

import json
import math
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .bridge import KINDS, Bridge, BridgeError, _ID_RE, _clip, _refs, _remote_ts, for_core

_ORDER_NAME = "_fanin_order.json"
_ORDER_MAX_BYTES = 1_048_576
_GUARD = threading.Lock()


def normalize_blackboard_post(data: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a ``POST /api/blackboard`` object. ``(post, None)`` or ``(None, detail)``.

    Same shape ``Bridge.post`` stores. ``kind`` defaults to finding. Text is clipped,
    not rejected, at ``MAX_TEXT``. An omitted id is assigned here.
    """
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
        # A JSON number or bool is not an id. str(True) would be "True" and
        # str(1.0) would be "1.0", both of which match the character pattern.
        return None, "bad post id"
    else:
        pid = raw_id
    raw_run = data.get("run_id", None)
    if raw_run is None:
        run_id = "run"
    elif not isinstance(raw_run, str):
        return None, "bad run id"
    else:
        clipped = raw_run[:80]
        run_id = clipped if clipped.strip() else "run"
    return {
        "id": pid,
        "run_id": run_id or "run",
        "agent": str(data.get("agent") or "agent")[:80],
        "role": str(data.get("role") or "")[:60],
        "kind": kind,
        "text": text,
        "refs": _string_refs(data.get("refs")),
        "ts": _remote_ts(data.get("ts")),
        "origin_node": str(data.get("origin_node") or "local")[:80],
    }, None


def _string_refs(refs: Any) -> list[str]:
    """Refs the peer actually sent as strings. ``true`` and ``1`` are not ids.

    ``_refs`` stringifies every entry, so a JSON bool would be stored as ``True``.
    """
    if isinstance(refs, str):
        return _refs(refs)
    if isinstance(refs, (list, tuple)):
        return _refs([item for item in refs if isinstance(item, str)])
    return []


def _data_dir(core: Any) -> Path | None:
    settings = getattr(core, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    return None if data_dir is None else Path(data_dir)


def _blackboard_lock(core: Any) -> threading.RLock:
    guard = getattr(core, "_blackboard_guard", None)
    if guard is not None:
        return guard
    with _GUARD:
        guard = getattr(core, "_blackboard_guard", None)
        if guard is None:
            guard = threading.RLock()
            try:
                core._blackboard_guard = guard
            except AttributeError:  # pragma: no cover - slotted core
                return _GUARD
        return guard


def _iter_bridges(core: Any) -> list[Bridge]:
    """Open run bridges, then persisted ones the cache has dropped.

    Symlinks and special files are skipped. ``read_regular`` would already
    refuse them, and opening a FIFO here would stall the request.
    """
    guard = getattr(core, "_bridge_guard", None)
    if guard is None:
        cached = list((getattr(core, "_bridges", None) or {}).values())
    else:
        with guard:
            cached = list((getattr(core, "_bridges", None) or {}).values())
    seen: set[str] = set()
    out: list[Bridge] = []
    for bridge in cached:
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
    for path in sorted(folder.glob("*.jsonl")):
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat_regular(st.st_mode):
            continue
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        out.append(Bridge(path.stem, data_dir))
        seen.add(resolved)
    return out


def stat_regular(mode: int) -> bool:
    import stat
    return stat.S_ISREG(mode)


def _posts_by_id(core: Any) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for bridge in _iter_bridges(core):
        for post in bridge.posts():
            by_id.setdefault(post["id"], post)  # first write wins across runs
    return by_id


def _id_state(core: Any, pid: str) -> str | None:
    """``stored`` if any ring still has the id, ``evicted`` if only a tombstone does."""
    evicted = False
    for bridge in _iter_bridges(core):
        with bridge._lock:
            if pid in bridge._posts:
                return "stored"
            if pid in bridge._evicted:
                evicted = True
    return "evicted" if evicted else None


def _ts_key(post: dict[str, Any]) -> tuple[float, str]:
    ts = post.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        number = 0.0
    else:
        try:
            number = float(ts)
        except (TypeError, ValueError, OverflowError):
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
    cached = getattr(core, "_blackboard_order", None)
    if isinstance(cached, OrderedDict):
        return cached
    order: "OrderedDict[str, None]" = OrderedDict()
    path = _order_path(core)
    if path is not None:
        from .safeio import FileTooBig, read_capped
        try:
            raw = read_capped(path, _ORDER_MAX_BYTES)
        except FileTooBig:
            raw = None
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, RecursionError, UnicodeError, TypeError, ValueError):
                data = None
            ids = data.get("ids") if isinstance(data, dict) else None
            if isinstance(ids, list):
                for item in ids:
                    if isinstance(item, str) and _ID_RE.match(item) and item not in order:
                        order[item] = None
    try:
        core._blackboard_order = order
    except AttributeError:  # pragma: no cover - slotted core
        pass
    return order


def _save_order(core: Any, order: "OrderedDict[str, None]") -> None:
    path = _order_path(core)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from .safeio import atomic_write
        atomic_write(path, json.dumps({"ids": list(order)}), encoding="utf-8")
    except OSError:
        return


def _sync_order(core: Any, by_id: dict[str, dict[str, Any]]) -> "OrderedDict[str, None]":
    """Drop gone ids. First snapshot is oldest-ts-first; later ids append.

    Appending keeps ``since`` safe: a post accepted after the cursor is returned
    even when its ``ts`` is older than that cursor. A pure timestamp sort would
    place it before the cursor and the relay would never see it.
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
    with _blackboard_lock(core):
        by_id = _posts_by_id(core)
        order = _sync_order(core, by_id)
        posts = [by_id[pid] for pid in order if pid in by_id]
        if since:
            ids = [post["id"] for post in posts]
            if since in ids:
                posts = posts[ids.index(since) + 1:]
        return posts


def blackboard_accept(core: Any, post: dict[str, Any]) -> tuple[bool, str]:
    """Store ``post`` on its run bridge. Return ``(duplicate, id)``.

    A duplicate (already stored, or already evicted) is not rewritten.
    """
    pid = str(post.get("id") or "")
    if not _ID_RE.match(pid):
        raise BridgeError("bad post id")
    with _blackboard_lock(core):
        if _id_state(core, pid) is not None:
            return True, pid
        dest = for_core(core, str(post.get("run_id") or "run"))
        stats = dest.merge_remote([post], str(post.get("origin_node") or "local"))
        if stats.get("merged") == 1:
            _sync_order(core, _posts_by_id(core))
            return False, pid
        if stats.get("skipped"):
            return True, pid
        raise BridgeError("post rejected")
