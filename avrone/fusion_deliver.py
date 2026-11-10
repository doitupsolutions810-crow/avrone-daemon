"""Outbound delivery from this daemon to CycleKernal ``POST /v1/events``.

The Fusion Bridge repository is the relay and has another writer. This module
is the daemon-side ledger and client: it speaks the same HTTP contract, it is
off unless a caller invokes it, and it does not start a background poller.

Accounting, over unique ``(source, id)`` keys admitted to the ledger:

    A = D + P + F

A is admitted keys, D delivered (HTTP 2xx, including ``duplicate: true``),
P still pending, F explicit permanent failures (local body that cannot fit
4096 bytes, or a permanent 4xx). Retries increment ``attempts`` and do not
change A. A second offer of the same key keeps the first payload.

429 and 500 are retried with the same bounded exponential delay. A usable
``Retry-After`` delta-seconds value replaces that schedule for a 429. Neither
is a permanent failure, a zero-delay spin, or a circuit-breaker trip. A
timeout or a refused connection opens the breaker. 401 and 403 leave the key
pending and do not use the transport backoff.

``heat``, ``cone_angle``, ``cone``, and ``beta_t`` are payload fields. A
finite number is kept. A bool, a non-finite value, a string where a number is
required, or a value over 512 JSON bytes is an explicit failure and is not
stored. That is a syntax limit, not a physical measurement.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from .bridge import _ID_RE
from .fusion_loopback import origin_key

MAX_BODY = 4096
FIELD_BUDGET = 512
# Stay far under CPython's recursion limit. json.dumps on a deeper tree raises
# RecursionError, and on 3.11 catching that error leaves the recursion counter
# high enough that a later deep walk in the same process fails immediately.
MAX_NESTING = 32
SOURCES = ("avrone", "cyclekernal", "greatscale")
KINDS = ("finding", "question", "answer", "correction", "conclusion", "mood", "evidence", "health")
RESEND_ON_CK_BOOT = frozenset({"avrone", "greatscale"})
MIRROR_ID_PREFIXES = ("ck-health-", "ck-mood-", "ck-evidence-")
STATE_MAX_BYTES = 2 * 1024 * 1024
# A configured backoff of 0 must not tight-loop. 50 ms is the same floor the
# relay uses before it will start another call inside a cycle.
MIN_RETRY_DELAY = 0.05

Transport = Callable[[str, str, bytes | None, dict[str, str], float], "HttpResult"]


@dataclass
class HttpResult:
    status: int
    body: dict[str, Any] | None
    error: str | None  # "timeout" | "refused" | None
    retry_after: str | None = None


@dataclass
class DeliverConfig:
    base: str
    events_path: str
    health_path: str
    token: str
    timeout: float
    deadline: float
    breaker_threshold: int
    breaker_cooldown: float
    backoff_base: float
    backoff_max: float
    interval: float
    max_body: int = MAX_BODY


def _raw_env(name: str) -> str | None:
    """Env value with no trimming. Unset and empty are absent; spaces stay."""
    if name not in os.environ or os.environ[name] == "":
        return None
    return os.environ[name]


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def default_state_path(data_dir: Path | None = None) -> Path | None:
    """``AVRONE_FUSION_STATE_PATH`` when set, else ``<data_dir>/fusion/delivery-state.json``."""
    raw = os.environ.get("AVRONE_FUSION_STATE_PATH", "").strip()
    if raw:
        return Path(raw)
    if data_dir is None:
        return None
    return Path(data_dir) / "fusion" / "delivery-state.json"


def config_from_env() -> DeliverConfig:
    """Read at call time so a rotated token is picked up without a restart.

    ``CK_FUSION_TOKEN`` wins over ``FUSION_TOKEN``, which wins over
    ``AVRONE_ADMIN_TOKEN``.
    """
    token = _raw_env("CK_FUSION_TOKEN")
    if token is None:
        token = _raw_env("FUSION_TOKEN")
    if token is None:
        token = _raw_env("AVRONE_ADMIN_TOKEN") or ""
    base = os.environ.get("CK_BASE", "http://127.0.0.1:8090").strip() or "http://127.0.0.1:8090"
    return DeliverConfig(
        base=base.rstrip("/"),
        events_path=os.environ.get("CK_EVENTS_PATH", "/v1/events") or "/v1/events",
        health_path=os.environ.get("CK_HEALTH_PATH", "/health") or "/health",
        token=token,
        timeout=max(MIN_RETRY_DELAY, _float_env("FUSION_TIMEOUT", 5.0)),
        deadline=max(0.0, _float_env("FUSION_CYCLE_DEADLINE", 10.0)),
        breaker_threshold=max(0, _int_env("FUSION_BREAKER_THRESHOLD", 1)),
        breaker_cooldown=max(0.0, _float_env("FUSION_BREAKER_COOLDOWN", 30.0)),
        backoff_base=max(0.0, _float_env("FUSION_BACKOFF_BASE", 0.5)),
        backoff_max=max(0.0, _float_env("FUSION_BACKOFF_MAX", 8.0)),
        interval=max(0.0, _float_env("FUSION_INTERVAL", 1.0)),
    )


class Clock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ManualClock(Clock):
    """Test clock. ``sleep`` advances time and records the delay in seconds."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        seconds = 0.0 if seconds < 0 else float(seconds)
        self.sleeps.append(seconds)
        self.t += seconds


def retry_delay(kind: str, streak: int, config: DeliverConfig) -> float:
    """Seconds to wait before another try.

    429 and 500 use the same exponential cap (``FUSION_BACKOFF_BASE`` doubling
    to ``FUSION_BACKOFF_MAX``). 404 and 405 wait ``interval`` only. Auth waits
    nothing here; the caller must not loop on it inside the cycle. A computed
    delay of 0 is raised to ``MIN_RETRY_DELAY`` so twenty fast 429s cannot spin.
    """
    if kind == "auth":
        return 0.0
    if kind in {"status404", "status405"}:
        delay = config.interval
    else:
        base = config.backoff_base
        maximum = config.backoff_max if config.backoff_max >= base else base
        if base > 0:
            delay = min(maximum, base * (2 ** max(0, streak - 1)))
        else:
            delay = maximum
    if delay <= 0:
        delay = MIN_RETRY_DELAY
    return delay


def parse_retry_after(header: str | None, *, cap_s: float = 30.0) -> float | None:
    """Delta-seconds in ``[0, cap_s]``. Words, signs, dates, and larger numbers are ignored.

    A value of 0 is returned as ``MIN_RETRY_DELAY`` so a peer that sends
    ``Retry-After: 0`` twenty times cannot busy-spin the client.
    """
    if header is None:
        return None
    text = str(header).strip()
    if not text or text[0] in "+-":
        return None
    if text.count(".") > 1:
        return None
    if any(char not in "0123456789." for char in text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0 or value > cap_s:
        return None
    if value == 0:
        return MIN_RETRY_DELAY
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _finite_number(value: Any, field: str) -> float | None:
    """A finite JSON number, or None when the value is the wrong type.

    Booleans are rejected. They are ints in Python and would become 1 or 0.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _check_known_fields(payload: dict[str, Any]) -> str | None:
    """Syntax only for heat, cone_angle (and cone), and beta_t. Not a physical law.

    Wrong types are an explicit failure. They are not rewritten into a number
    or into a descriptor object the peer would then reject.
    """
    import unicodedata
    numeric = ("heat", "cone_angle", "cone")
    for field in numeric:
        if field not in payload:
            continue
        if _finite_number(payload[field], field) is None:
            return f"{field}: not a finite JSON number"
    if "beta_t" not in payload:
        return None
    value = payload["beta_t"]
    if isinstance(value, bool):
        return "beta_t: boolean is not a number"
    if isinstance(value, (int, float)):
        if _finite_number(value, "beta_t") is None:
            return "beta_t: non-finite"
        return None
    if not isinstance(value, str):
        return "beta_t: not a number or string"
    for char in value:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            return "beta_t: control or format character"
    if value.strip()[:1] in "{[":
        try:
            embedded = json.loads(value)
        except json.JSONDecodeError:
            embedded = None
        if isinstance(embedded, (dict, list)):
            return "beta_t: double-encoded JSON container"
    return None


def _nesting_too_deep(value: Any, limit: int = MAX_NESTING) -> bool:
    """Iterative depth and cycle check. This function does not recurse."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    seen: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        if not isinstance(node, (dict, list, tuple)):
            continue
        mark = id(node)
        if mark in seen:
            return True
        seen.add(mark)
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list, tuple)):
                stack.append((child, depth + 1))
    return False


def _payload_too_big(payload: dict[str, Any]) -> bool:
    for value in payload.values():
        if _nesting_too_deep(value):
            return True
        try:
            if len(_canonical(value)) > FIELD_BUDGET:
                return True
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            return True
    return False


def _wire_bytes(event: dict[str, Any]) -> bytes | None:
    """UTF-8 JSON, or None when the document cannot be encoded.

    A lone surrogate is valid in a Python str and in JSON ``\\u`` escapes, and
    it is not valid UTF-8. The peer would never see those bytes.
    """
    try:
        return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        return None


def prepare_event(data: Any, *, max_body: int = MAX_BODY) -> tuple[dict[str, Any] | None, str | None]:
    """Return a ledger event whose JSON fits ``max_body``, or ``(None, detail)``."""
    if not isinstance(data, dict):
        return None, "body must be a JSON object"
    eid = data.get("id")
    source = data.get("source")
    kind = data.get("kind")
    run_id = data.get("run_id")
    payload = data.get("payload")
    if not isinstance(eid, str) or not _ID_RE.match(eid):
        return None, "bad event id"
    if source not in SOURCES:
        return None, "bad source"
    if not isinstance(kind, str) or kind not in KINDS:
        return None, "bad kind"
    if not isinstance(run_id, str) or not run_id.strip():
        return None, "run_id is required"
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"
    field_err = _check_known_fields(payload)
    if field_err:
        return None, field_err
    if _payload_too_big(payload):
        return None, "request body too large (max 4096 bytes)"
    ts = data.get("ts", 0)
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        ts = 0
    try:
        ts = float(ts)
    except (TypeError, ValueError, OverflowError):
        ts = 0
    if not (ts > 0) or ts != ts or ts in (float("inf"), float("-inf")):
        ts = 0
    event = {
        "id": eid,
        "source": source,
        "kind": kind,
        "run_id": run_id.strip()[:80],
        "payload": payload,
        "ts": ts,
    }
    raw = _wire_bytes(event)
    if raw is None:
        return None, "request body is not valid UTF-8"
    if len(raw) > max_body:
        return None, "request body too large (max 4096 bytes)"
    return event, None


def is_mirror(event: dict[str, Any]) -> bool:
    """Relay echoes this daemon must not forward back to CycleKernal."""
    eid = str(event.get("id") or "")
    if eid.startswith(MIRROR_ID_PREFIXES) or event.get("source") == "cyclekernal":
        return True
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    origin = str(payload.get("origin_node") or "")
    role = str(payload.get("role") or "")
    agent = str(payload.get("agent") or "")
    if origin == "fusion-bridge" or role == "cyclekernal-relay":
        return True
    if agent.lower().startswith("cyclekernal:"):
        return True
    return False


def event_from_post(post: dict[str, Any]) -> dict[str, Any]:
    """One Avrone blackboard post as a CycleKernal event. Source is ``avrone``."""
    payload = {
        "id": post.get("id"),
        "agent": post.get("agent"),
        "role": post.get("role"),
        "kind": post.get("kind"),
        "text": post.get("text"),
        "refs": post.get("refs") or [],
        "origin_node": post.get("origin_node"),
    }
    return {
        "id": post.get("id"),
        "source": "avrone",
        "kind": post.get("kind"),
        "run_id": post.get("run_id") or "run",
        "payload": payload,
        "ts": post.get("ts") or 0,
    }


class Breaker:
    def __init__(self) -> None:
        self.fails = 0
        self.open_until = 0.0

    def is_open(self, now: float) -> bool:
        return now < self.open_until

    def record_dial_failure(self, now: float, threshold: int, cooldown: float) -> None:
        self.fails += 1
        if threshold > 0 and self.fails >= threshold:
            self.open_until = now + cooldown

    def record_success(self) -> None:
        self.fails = 0
        self.open_until = 0.0


class Ledger:
    """Persistent first-write-wins ledger. Version 2. A version-1 ``seen`` list loads."""

    def __init__(self, path: Path | None, clock: Clock | None = None) -> None:
        self.path = None if path is None else Path(path)
        self.clock = clock or Clock()
        self.entries: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
        self.boots: dict[str, str] = {}
        self.cursor = ""
        self.state_errors = 0
        self.suppressed = 0
        self._breakers: dict[str, Breaker] = {}
        self._lock = threading.RLock()
        self.load()

    def accounts(self) -> dict[str, int]:
        with self._lock:
            return self._accounts()

    def _accounts(self) -> dict[str, int]:
        states = [row["state"] for row in self.entries.values()]
        admitted = len(states)
        delivered = states.count("delivered")
        pending = states.count("pending")
        failed = states.count("failed")
        if admitted != delivered + pending + failed:
            raise RuntimeError(f"ledger identity broken: {admitted} != {delivered}+{pending}+{failed}")
        return {
            "A": admitted,
            "D": delivered,
            "P": pending,
            "F": failed,
            "attempts": sum(int(row["attempts"]) for row in self.entries.values()),
            "suppressed": self.suppressed,
        }

    def offer(self, data: Any) -> str:
        """Admit one event. ``accepted``, ``duplicate``, ``suppressed``, or ``failed``.

        Duplicate keeps the first payload. Failed is an explicit permanent
        failure (the body cannot fit) and is part of A and F, not a drop.
        """
        with self._lock:
            return self._offer(data)

    def _offer(self, data: Any) -> str:
        if isinstance(data, dict) and is_mirror(data):
            self.suppressed += 1
            return "suppressed"
        event, err = prepare_event(data)
        if event is None:
            if isinstance(data, dict) and isinstance(data.get("id"), str) and data.get("source") in SOURCES \
                    and _ID_RE.match(str(data.get("id"))) and err:
                key = (str(data["source"]), str(data["id"]))
                if key in self.entries:
                    return "duplicate"
                self.entries[key] = self._row(key, None, "failed", err)
                self.save()
                return "failed"
            raise ValueError(err or "rejected")
        if is_mirror(event):
            self.suppressed += 1
            return "suppressed"
        key = (event["source"], event["id"])
        if key in self.entries:
            return "duplicate"
        self.entries[key] = self._row(key, event, "pending", None)
        self.save()
        return "accepted"

    @staticmethod
    def _row(key: tuple[str, str], event: dict[str, Any] | None, state: str, detail: str | None) -> dict[str, Any]:
        return {
            "source": key[0],
            "id": key[1],
            "state": state,
            "event": event,
            "detail": detail,
            "attempts": 0,
            "status": None,
        }

    def note_boot(self, peer: str, boot: Any) -> bool:
        """Record a peer boot id. A change requeues deliveries to that peer.

        A missing boot leaves the ledger alone. Only Avrone and greatscale
        deliveries are requeued when CycleKernal restarts; explicit failures
        stay failed because the payload itself was refused.
        """
        with self._lock:
            return self._note_boot(peer, boot)

    def _note_boot(self, peer: str, boot: Any) -> bool:
        if boot is None or boot == "":
            return False
        if isinstance(boot, bool) or isinstance(boot, float) or not isinstance(boot, (str, int)):
            # A float is not an int. str(1.0) is "1.0" and str(1) is "1", so
            # accepting both would resend on every poll that changes type.
            return False
        text = str(boot)
        prev = self.boots.get(peer)
        if prev is None:
            self.boots[peer] = text
            self.save()
            return False
        if prev == text:
            return False
        self.boots[peer] = text
        changed = False
        if peer == "cyclekernal":
            for row in self.entries.values():
                if row["state"] == "delivered" and row["source"] in RESEND_ON_CK_BOOT \
                        and isinstance(row.get("event"), dict):
                    row["state"] = "pending"
                    changed = True
        self.save()
        return changed

    def breaker_for(self, base: str) -> Breaker:
        key = origin_key(base)
        found = self._breakers.get(key)
        if found is None:
            found = Breaker()
            self._breakers[key] = found
        return found

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        from .safeio import FileTooBig, read_capped
        try:
            raw = read_capped(self.path, STATE_MAX_BYTES)
        except FileTooBig:
            self._discard()
            return
        if raw is None:
            if self.path.exists():
                self._discard()
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError, RecursionError, TypeError, ValueError):
            self._discard()
            return
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            self._discard()
            return
        self.cursor = data.get("cursor") if isinstance(data.get("cursor"), str) else ""
        boots = data.get("boots") if isinstance(data.get("boots"), dict) else {}
        self.boots = {str(k): str(v) for k, v in boots.items() if isinstance(v, (str, int)) and not isinstance(v, bool)}
        if data.get("version") == 1:
            self._load_v1_seen(data.get("seen"))
            return
        rows = data.get("entries")
        if not isinstance(rows, list):
            self._discard()
            return
        for row in rows:
            parsed = self._parse_row(row)
            if parsed is None:
                continue
            key = (parsed["source"], parsed["id"])
            if key not in self.entries:
                self.entries[key] = parsed

    def _load_v1_seen(self, seen: Any) -> None:
        if not isinstance(seen, list):
            return
        for item in seen:
            if isinstance(item, str) and _ID_RE.match(item):
                key = ("avrone", item)
                if key not in self.entries:
                    row = self._row(key, None, "delivered", "version-1 seen")
                    self.entries[key] = row

    def _parse_row(self, row: Any) -> dict[str, Any] | None:
        if not isinstance(row, dict):
            return None
        source = row.get("source")
        eid = row.get("id")
        state = row.get("state")
        if source not in SOURCES or not isinstance(eid, str) or not _ID_RE.match(eid):
            return None
        if state not in {"pending", "delivered", "failed"}:
            return None
        event = row.get("event")
        if event is not None and not isinstance(event, dict):
            return None
        if state == "pending" and not isinstance(event, dict):
            return None
        attempts_raw = row.get("attempts") or 0
        if isinstance(attempts_raw, bool):
            attempts = 0
        else:
            try:
                attempts = int(attempts_raw)
            except (TypeError, ValueError):
                attempts = 0
        status = row.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            status = None
        return {
            "source": source,
            "id": eid,
            "state": state,
            "event": event,
            "detail": row.get("detail") if isinstance(row.get("detail"), str) else None,
            "attempts": max(0, attempts),
            "status": status,
        }

    def _discard(self) -> None:
        self.entries.clear()
        self.boots.clear()
        self.cursor = ""
        self.state_errors += 1

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": 2,
            "cursor": self.cursor,
            "boots": self.boots,
            "entries": list(self.entries.values()),
            "state_errors": self.state_errors,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            from .safeio import atomic_write
            atomic_write(self.path, json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
                         encoding="utf-8")
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            return

    def pump(self, transport: Transport, config: DeliverConfig | None = None, *, max_cycles: int = 8) -> dict[str, Any]:
        """Deliver pending keys. Each cycle is deadline-bounded. Stops when idle.

        ``max_cycles`` is the hard stop so a peer that never recovers cannot
        loop forever. Pending keys stay pending; they are not failed.
        """
        config = config or config_from_env()
        cycles = 0
        calls = 0
        for _ in range(max(1, max_cycles)):
            cycles += 1
            made, used = self._cycle(transport, config)
            calls += used
            with self._lock:
                idle = self._accounts()["P"] == 0
            if idle or not made:
                break
        with self._lock:
            report = self._accounts()
        report["cycles"] = cycles
        report["calls"] = calls
        report["clock_s"] = self.clock.now() if isinstance(self.clock, ManualClock) else None
        return report

    def _cycle(self, transport: Transport, config: DeliverConfig) -> tuple[bool, int]:
        start = self.clock.now()
        calls = 0
        progressed = False
        self._probe_health(transport, config, start)
        breaker = self.breaker_for(config.base)
        with self._lock:
            pending = [row for row in self.entries.values()
                       if row["state"] == "pending" and isinstance(row.get("event"), dict)]
        for row in pending:
            if row["state"] != "pending":
                continue
            if not self._time_left(config, start):
                break
            if breaker.is_open(self.clock.now()):
                break
            outcome, n = self._attempt_row(row, transport, config, breaker, start)
            calls += n
            if outcome in {"delivered", "failed", "retry"}:
                progressed = True
            if outcome == "auth":
                break
        return progressed, calls

    def _time_left(self, config: DeliverConfig, start: float) -> bool:
        if config.deadline <= 0:
            return True
        return (self.clock.now() - start) <= (config.deadline - MIN_RETRY_DELAY)

    def _call_timeout(self, config: DeliverConfig, start: float) -> float:
        if config.deadline <= 0:
            return config.timeout
        left = config.deadline - (self.clock.now() - start)
        return max(MIN_RETRY_DELAY, min(config.timeout, left))

    def _probe_health(self, transport: Transport, config: DeliverConfig, start: float) -> None:
        if not self._time_left(config, start):
            return
        url = _join(config.base, config.health_path)
        timeout = self._call_timeout(config, start)
        result = transport("GET", url, None, {}, timeout)
        if result.error or result.status != 200 or not isinstance(result.body, dict):
            return
        if "boot" not in result.body:
            return
        self.note_boot("cyclekernal", result.body.get("boot"))

    def _attempt_row(self, row: dict[str, Any], transport: Transport, config: DeliverConfig,
                     breaker: Breaker, start: float) -> tuple[str, int]:
        """Try one key. 429 retries inside the cycle until success, deadline, or cap."""
        calls = 0
        while row["state"] == "pending":
            if not self._time_left(config, start):
                return "stopped", calls
            if breaker.is_open(self.clock.now()):
                return "stopped", calls
            if calls >= 64:
                return "stopped", calls
            event = row["event"]
            body = _wire_bytes(event)
            if body is None:
                with self._lock:
                    self._fail(row, 400, "request body is not valid UTF-8")
                return "failed", calls
            if len(body) > config.max_body:
                with self._lock:
                    self._fail(row, 413, "request body too large (max 4096 bytes)")
                return "failed", calls
            headers = {
                "Content-Type": "application/json",
                "Idempotency-Key": event["id"],
            }
            if config.token:
                if _header_unsafe(config.token):
                    with self._lock:
                        self.save()
                    return "auth", calls
                headers["X-Admin-Token"] = config.token
            timeout = self._call_timeout(config, start)
            result = transport("POST", _join(config.base, config.events_path), body, headers, timeout)
            calls += 1
            with self._lock:
                row["attempts"] = int(row["attempts"]) + 1
                kind = _classify(result)
                if kind == "delivered":
                    row["state"] = "delivered"
                    row["status"] = result.status
                    row["detail"] = None
                    self.cursor = event["id"]
                    breaker.record_success()
                    self.save()
                    return "delivered", calls
                if kind == "permanent":
                    detail = ""
                    if isinstance(result.body, dict) and isinstance(result.body.get("detail"), str):
                        detail = result.body["detail"]
                    self._fail(row, result.status, detail or "permanent")
                    return "failed", calls
                if kind == "dial":
                    breaker.record_dial_failure(self.clock.now(), config.breaker_threshold, config.breaker_cooldown)
                    self.save()
                    return "retry", calls
                if kind == "auth":
                    self.save()
                    return "auth", calls
                if kind == "status429":
                    honored = parse_retry_after(result.retry_after)
                    delay = honored if honored is not None else retry_delay("status429", row["attempts"], config)
                elif kind == "status5xx":
                    delay = retry_delay("status429", row["attempts"], config)
                else:
                    delay = retry_delay(kind, row["attempts"], config)
            self.clock.sleep(delay)
            with self._lock:
                self.save()
            if kind != "status429":
                return "retry", calls
        return "stopped", calls

    def _fail(self, row: dict[str, Any], status: int, detail: str) -> None:
        row["state"] = "failed"
        row["status"] = status
        row["detail"] = detail
        self.save()


def _header_unsafe(value: str) -> bool:
    """CR, LF, or NUL in a header value is not a token. It is not a dead peer."""
    return any(ch in value for ch in "\r\n\x00")


def _classify(result: HttpResult) -> str:
    if result.error == "bad-header":
        return "auth"
    if result.error in {"timeout", "refused"}:
        return "dial"
    if result.error:
        return "dial"
    status = result.status
    if 200 <= status < 300:
        return "delivered"
    if status in {401, 403}:
        return "auth"
    if status == 429:
        return "status429"
    if status in {404, 405}:
        return "status404" if status == 404 else "status405"
    if 500 <= status < 600:
        return "status5xx"
    if 400 <= status < 500:
        return "permanent"
    if 300 <= status < 400:
        # Not a 2xx ack and not a dead socket. Do not open the breaker, and do
        # not follow the Location: the admin token must stay on this origin.
        return "status3xx"
    return "dial"


def _join(base: str, path: str) -> str:
    path = path if path.startswith("/") else "/" + path
    return base.rstrip("/") + path


def httpx_transport(method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float) -> HttpResult:
    """One real HTTP call. ``trust_env`` is off so an HTTP proxy cannot swallow loopback."""
    import httpx
    if any(_header_unsafe(value) for value in headers.values()):
        return HttpResult(0, None, "bad-header")
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = client.request(method, url, content=body, headers=headers)
    except httpx.TimeoutException:
        return HttpResult(0, None, "timeout")
    except httpx.ConnectError:
        return HttpResult(0, None, "refused")
    except httpx.HTTPError:
        return HttpResult(0, None, "refused")
    parsed: dict[str, Any] | None = None
    try:
        data = response.json()
        if isinstance(data, dict):
            parsed = data
    except (json.JSONDecodeError, UnicodeError, ValueError):
        parsed = None
    return HttpResult(response.status_code, parsed, None, response.headers.get("retry-after"))

