"""Edge cases past the happy-path contract tests.

Each case is a behavior that has failed or would fail quietly: a second
Content-Length hiding a larger one, a JSON bool used as an id, a ledger race
on one (source, id), a Retry-After of 0, a 404 that must not use the 429
schedule, a breaker that must close after its cooldown, and a health timeout
that must not be treated as a dead peer.
"""
import asyncio
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from avrone import server
from avrone.fusion_deliver import (
    DeliverConfig,
    HttpResult,
    Ledger,
    ManualClock,
    config_from_env,
    parse_retry_after,
    prepare_event,
    retry_delay,
)
from avrone.fusion_blackboard import blackboard_posts

AUTH = {"X-Admin-Token": "test-token"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def _post(client, body, headers=None):
    h = dict(AUTH if headers is None else headers)
    return client.post("/api/blackboard", json=body, headers=h)


def _cfg(**kw) -> DeliverConfig:
    base = dict(
        base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
        token="tok", timeout=5, deadline=0, breaker_threshold=1, breaker_cooldown=30,
        backoff_base=0.5, backoff_max=8, interval=1,
    )
    base.update(kw)
    return DeliverConfig(**base)


def _event(eid="e1", source="avrone", **payload):
    body = {"text": "edge", "heat": 0.25, "cone_angle": 1.0, "beta_t": 2}
    body.update(payload)
    return {
        "id": eid, "source": source, "kind": "evidence", "run_id": "edge",
        "payload": body, "ts": 10,
    }


class Tape:
    """Scripted peer. modes is a list consumed by POST; the last item repeats."""

    def __init__(self, clock, modes):
        self.clock = clock
        self.modes = list(modes)
        self.posts = 0
        self.gets = 0
        self.headers = []
        self.bodies = []
        self.get_error = None
        self.boot = "b0"

    def __call__(self, method, url, body, headers, timeout):
        if method == "GET":
            self.gets += 1
            if self.get_error:
                return HttpResult(0, None, self.get_error)
            if self.boot is None:
                return HttpResult(200, {"status": "ghost-online"}, None)
            return HttpResult(200, {"status": "ghost-online", "boot": self.boot}, None)
        self.posts += 1
        self.headers.append(dict(headers))
        self.bodies.append(body)
        mode = self.modes[min(self.posts - 1, len(self.modes) - 1)]
        if mode == "429":
            return HttpResult(429, {"detail": "slow"}, None, None)
        if isinstance(mode, tuple) and mode[0] == "429":
            return HttpResult(429, {"detail": "slow"}, None, mode[1])
        if mode == "500":
            return HttpResult(500, {"detail": "later"}, None)
        if mode == "503":
            return HttpResult(503, {"detail": "unavailable"}, None)
        if mode == "404":
            return HttpResult(404, {"detail": "missing"}, None)
        if mode == "400":
            return HttpResult(400, {"detail": "bad event id"}, None)
        if mode == "401":
            return HttpResult(401, {"detail": "Bad admin token"}, None)
        if mode == "timeout":
            return HttpResult(0, None, "timeout")
        if mode == "302":
            return HttpResult(302, {"detail": "moved"}, None)
        if mode == "200":
            return HttpResult(200, {"accepted": True, "duplicate": self.posts > 1, "id": "x"}, None)
        raise AssertionError(mode)


# ---------------------------------------------------------------- HTTP cap

def test_any_content_length_over_the_cap_is_413_before_the_body_is_read():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = server.BodyLimit(app)

    async def drive(headers):
        used = []

        async def receive():
            used.append(1)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "path": "/api/blackboard", "headers": headers}
        await mw(scope, receive, send)
        return sent, used

    hidden = asyncio.run(drive([
        (b"content-length", b"2"),
        (b"content-length", b"999999"),
    ]))
    reverse = asyncio.run(drive([
        (b"content-length", b"999999"),
        (b"content-length", b"2"),
    ]))
    for sent, used in (hidden, reverse):
        assert sent[0]["status"] == 413
        assert used == []
        assert json.loads(sent[1]["body"])["detail"] == "request body too large (max 4096 bytes)"
    # A bad token is not a size. The stream is counted, and a small body is read.
    for label in (b"+12", b"-1", b"zz", b" 12 "):
        sent, used = asyncio.run(drive([(b"content-length", label)]))
        assert sent[0]["status"] == 200, label
        assert used == [1]
    assert calls == ["/api/blackboard", "/api/blackboard", "/api/blackboard", "/api/blackboard"]


def test_chunked_body_is_cut_at_4096_even_when_the_declared_length_is_small():
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = server.BodyLimit(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"y" * 5000, "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "path": "/api/blackboard", "headers": [(b"content-length", b"10")]}
    asyncio.run(mw(scope, receive, send))
    assert sent[0]["status"] == 413


def test_non_string_ids_and_run_ids_are_400_and_store_nothing(client):
    for body in (
        {"id": 12, "text": "n"},
        {"id": True, "text": "n"},
        {"id": 1.5, "text": "n"},
        {"id": ["a"], "text": "n"},
        {"id": {"a": 1}, "text": "n"},
        {"text": "n", "run_id": 7},
        {"text": "n", "run_id": True},
    ):
        r = _post(client, body)
        assert r.status_code == 400, body
        assert r.json()["detail"] in {"bad post id", "bad run id"}
    assert client.get("/api/blackboard", headers=AUTH).json()["count"] == 0


def test_duplicate_json_keys_keep_the_last_value_and_nan_ts_is_finite(client):
    raw = b'{"id":"dup-key","text":"first","id":"dup-key","text":"second","ts":NaN}'
    r = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert r.status_code == 200 and r.json()["duplicate"] is False
    row = client.get("/api/blackboard", headers=AUTH).json()["posts"][0]
    assert row["id"] == "dup-key" and row["text"] == "second"
    assert row["ts"] > 0 and row["ts"] == row["ts"]
    again = client.post("/api/blackboard", content=b'{"id":"dup-key","text":"third","ts":1e309}', headers=AUTH)
    assert again.json()["duplicate"] is True
    assert client.get("/api/blackboard", headers=AUTH).json()["posts"][0]["text"] == "second"


def test_unknown_since_and_a_huge_since_do_not_drop_the_ring(client):
    assert _post(client, {"id": "s1", "text": "one", "ts": 1}).status_code == 200
    huge = "z" * 10000
    for since in ("../etc/passwd", huge, "\x00", "s1-nope"):
        body = client.get("/api/blackboard", headers=AUTH, params={"since": since}).json()
        assert [p["id"] for p in body["posts"]] == ["s1"]


def test_fifo_journal_and_oversized_order_file_do_not_hang_or_drop(client):
    assert _post(client, {"id": "keep", "text": "kept", "ts": 3}).status_code == 200
    core = server.state["core"]
    folder = core.settings.data_dir / "bridge"
    fifo = folder / "blocked.jsonl"
    os.mkfifo(fifo)
    order = folder / "_fanin_order.json"
    order.write_bytes(b" " * (1024 * 1024 + 10))
    del core._blackboard_order
    started = time.perf_counter()
    ids = [p["id"] for p in blackboard_posts(core)]
    assert time.perf_counter() - started < 2.0
    assert ids == ["keep"]
    assert order.stat().st_size < 4096


def test_emoji_text_at_the_byte_cap_is_stored_and_a_bool_ref_is_dropped(client):
    # 4096-byte document whose text contains a 4-byte character.
    prefix = '{"id":"emo-1","text":"'
    suffix = '"}'
    # one emoji is 4 bytes and 1 character. Fill the rest with ASCII.
    room = 4096 - len(prefix.encode()) - len(suffix.encode()) - 4
    raw = (prefix + "é"[:0] + "🔥" + ("q" * room) + suffix).encode()
    # "🔥" is 4 bytes. If the arithmetic is short or long, rebuild from the byte budget.
    text_bytes = 4096 - len(prefix.encode()) - len(suffix.encode())
    raw = prefix.encode() + "🔥".encode() + (b"q" * (text_bytes - 4)) + suffix.encode()
    assert len(raw) == 4096
    r = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert r.status_code == 200, r.text
    stored = client.get("/api/blackboard", headers=AUTH).json()["posts"][0]["text"]
    assert stored.startswith("🔥")
    refs = _post(client, {"id": "refs-1", "text": "r", "refs": [True, 1, "ok", "ok", "bad id", None], "ts": 2})
    assert refs.status_code == 200
    row = [p for p in client.get("/api/blackboard", headers=AUTH).json()["posts"] if p["id"] == "refs-1"][0]
    assert row["refs"] == ["ok"]


# ---------------------------------------------------------------- delivery edges

def test_retry_after_edges_and_zero_does_not_spin():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("30.0") == 30.0
    assert parse_retry_after(" 0.25 ") == 0.25
    assert parse_retry_after("00.05") == 0.05
    assert parse_retry_after("0") == 0.05
    assert parse_retry_after("0.00") == 0.05
    for bad in ("30.0001", "1.2.3", "1e1", ".", "1e+1", "+0.1", "-0", "  ", None, "Wed, 21 Oct 2015 07:28:00 GMT"):
        assert parse_retry_after(bad) is None
    clock = ManualClock()
    tape = Tape(clock, [("429", "0")] * 20 + ["200"])
    ledger = Ledger(None, clock)
    ledger.offer(_event())
    report = ledger.pump(tape, _cfg(deadline=0), max_cycles=1)
    assert report["A"] == 1 and report["D"] == 1 and report["P"] == 0 and report["F"] == 0
    assert clock.sleeps == [0.05] * 20
    assert sum(clock.sleeps) == pytest.approx(1.0)
    assert tape.posts == 21


def test_retry_after_30_is_used_and_over_cap_falls_back(tmp_path):
    clock = ManualClock()
    honored = Tape(clock, [("429", "30"), "200"])
    ledger = Ledger(tmp_path / "a.json", clock)
    ledger.offer(_event(eid="rate-30"))
    ledger.pump(honored, _cfg(deadline=0, backoff_base=0.5, backoff_max=8), max_cycles=1)
    assert clock.sleeps == [30.0]
    clock2 = ManualClock()
    fallback = Tape(clock2, [("429", "30.0001"), "200"])
    ledger2 = Ledger(tmp_path / "b.json", clock2)
    ledger2.offer(_event(eid="rate-bad"))
    ledger2.pump(fallback, _cfg(deadline=0, backoff_base=0.5, backoff_max=8), max_cycles=1)
    assert clock2.sleeps == [0.5]


def test_404_waits_the_interval_and_500_and_503_do_not_open_the_breaker(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["404", "404", "404", "200"])
    ledger = Ledger(tmp_path / "404.json", clock)
    ledger.offer(_event(eid="miss"))
    report = ledger.pump(tape, _cfg(deadline=0, interval=0.2, backoff_base=0.5, backoff_max=8), max_cycles=4)
    assert report["D"] == 1 and report["F"] == 0 and tape.posts == 4
    assert clock.sleeps == [0.2, 0.2, 0.2]
    assert ledger.breaker_for("http://127.0.0.1:8090").fails == 0

    clock = ManualClock()
    tape = Tape(clock, ["503", "500", "200"])
    ledger = Ledger(tmp_path / "503.json", clock)
    ledger.offer(_event(eid="busy"))
    report = ledger.pump(tape, _cfg(deadline=0, backoff_base=0.05, backoff_max=0.2), max_cycles=4)
    assert report["D"] == 1 and report["attempts"] == 3
    assert clock.sleeps == [0.05, 0.1]
    assert ledger.breaker_for("http://[::1]:8090").is_open(clock.t) is False


def test_400_fails_one_key_and_the_sibling_still_delivers(tmp_path):
    clock = ManualClock()
    seen = []

    def transport(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        seen.append(json.loads(body)["id"])
        if json.loads(body)["id"] == "bad":
            return HttpResult(400, {"detail": "bad event id"}, None)
        return HttpResult(200, {"accepted": True, "duplicate": False}, None)

    ledger = Ledger(tmp_path / "mix.json", clock)
    assert ledger.offer(_event(eid="bad")) == "accepted"
    assert ledger.offer(_event(eid="good")) == "accepted"
    report = ledger.pump(transport, _cfg(), max_cycles=2)
    assert report["A"] == 2 and report["D"] == 1 and report["F"] == 1 and report["P"] == 0
    assert seen.count("bad") == 1 and "good" in seen
    # A second cycle must not send the permanent failure again.
    again = ledger.pump(transport, _cfg(), max_cycles=1)
    assert again["calls"] == 0 or seen.count("bad") == 1
    assert seen.count("bad") == 1


def test_auth_failure_then_a_later_cycle_delivers(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["401"])
    ledger = Ledger(tmp_path / "auth.json", clock)
    ledger.offer(_event(eid="later"))
    first = ledger.pump(tape, _cfg(token=" wrong "), max_cycles=3)
    assert first["P"] == 1 and first["D"] == 0 and first["F"] == 0
    assert tape.posts == 1 and clock.sleeps == []
    assert tape.headers[0]["X-Admin-Token"] == " wrong "
    assert tape.headers[0]["Idempotency-Key"] == "later"
    tape.modes = ["200"]
    second = ledger.pump(tape, _cfg(token=" wrong "), max_cycles=1)
    assert second["D"] == 1 and second["A"] == 1 and tape.posts == 2


def test_timeout_opens_the_breaker_until_cooldown_then_one_probe_recovers(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["timeout"])
    ledger = Ledger(tmp_path / "cool.json", clock)
    ledger.offer(_event(eid="probe"))
    first = ledger.pump(tape, _cfg(deadline=0, breaker_cooldown=30), max_cycles=2)
    assert first["P"] == 1 and tape.posts == 1
    assert ledger.breaker_for("http://[::ffff:127.0.0.1]:8090").is_open(0.0) is True
    clock.t = 29
    held = ledger.pump(tape, _cfg(deadline=0, breaker_cooldown=30), max_cycles=1)
    assert held["P"] == 1 and tape.posts == 1
    clock.t = 30
    tape.modes = ["200"]
    recovered = ledger.pump(tape, _cfg(deadline=0, breaker_cooldown=30), max_cycles=1)
    assert recovered["D"] == 1 and tape.posts == 2
    assert ledger.breaker_for("http://127.0.0.1:8090").is_open(clock.t) is False


def test_health_timeout_does_not_open_the_breaker(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["200"])
    tape.get_error = "timeout"
    ledger = Ledger(tmp_path / "health.json", clock)
    ledger.offer(_event(eid="live"))
    report = ledger.pump(tape, _cfg(), max_cycles=1)
    assert report["D"] == 1 and tape.gets == 1 and tape.posts == 1
    assert ledger.breaker_for("http://127.0.0.1:8090").fails == 0


def test_boot_edges_do_not_resend_failures_or_the_same_integer(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["200"])
    ledger = Ledger(tmp_path / "boot.json", clock)
    ledger.offer(_event(eid="find"))
    ledger.offer(_event(eid="ck-mood-keep"))  # mirror, suppressed
    failed = _event(eid="nope", heat=True)
    assert ledger.offer(failed) == "failed"
    ledger.pump(tape, _cfg(), max_cycles=1)
    assert tape.posts == 1
    tape.boot = 1  # integer, first sight of a new value after "b0"
    ledger.pump(tape, _cfg(), max_cycles=1)
    assert tape.posts == 2  # boot changed, the finding is sent again
    tape.boot = "1"  # same boot as the integer 1
    ledger.pump(tape, _cfg(), max_cycles=1)
    assert tape.posts == 2
    tape.boot = True  # bool is not a boot id
    ledger.pump(tape, _cfg(), max_cycles=1)
    assert tape.posts == 2
    assert ledger.accounts()["F"] == 1 and ledger.accounts()["suppressed"] == 1
    assert ledger.accounts()["A"] == ledger.accounts()["D"] + ledger.accounts()["P"] + ledger.accounts()["F"]


def test_failed_first_write_is_kept_when_a_later_good_payload_arrives(tmp_path):
    ledger = Ledger(tmp_path / "first.json", ManualClock())
    assert ledger.offer(_event(eid="same", heat=float("nan"))) == "failed"
    assert ledger.offer(_event(eid="same", heat=0.5)) == "duplicate"
    assert ledger.offer(_event(eid="same", source="greatscale", heat=0.5)) == "accepted"
    blob = (tmp_path / "first.json").read_bytes()
    assert b"nan" not in blob.lower()
    accounts = ledger.accounts()
    assert accounts["A"] == 2 and accounts["F"] == 1 and accounts["P"] == 1


@pytest.mark.parametrize("heat", [float("inf"), float("-inf"), 10 ** 400, None, "0.2", [0.2], {"v": 0.2}, True, False])
def test_bad_heat_is_an_explicit_failure(tmp_path, heat):
    ledger = Ledger(tmp_path / "h.json", ManualClock())
    assert ledger.offer(_event(eid="h", heat=heat)) == "failed"
    assert ledger.accounts() == {
        "A": 1, "D": 0, "P": 0, "F": 1, "attempts": 0, "suppressed": 0,
    }


def test_beta_t_emoji_is_kept_and_format_characters_are_refused(tmp_path):
    ok, err = prepare_event(_event(beta_t="beta \U0001F525"))
    assert err is None and ok["payload"]["beta_t"] == "beta \U0001F525"
    ledger = Ledger(tmp_path / "b.json", ManualClock())
    assert ledger.offer(_event(eid="zw", beta_t="a\u200bb")) == "failed"
    assert ledger.offer(_event(eid="sur", beta_t="a\ud800b")) == "failed"
    assert ledger.offer(_event(eid="wrap", beta_t=' {"a": 1} ')) == "failed"
    assert ledger.offer(_event(eid="plain", beta_t="{not json")) == "accepted"
    assert ledger.accounts()["F"] == 3 and ledger.accounts()["P"] == 1
    stored = (tmp_path / "b.json").read_text(encoding="utf-8")
    assert "\u200b" not in stored and "\\u200b" not in stored


def test_oversized_list_is_not_stored(tmp_path):
    ledger = Ledger(tmp_path / "list.json", ManualClock())
    assert ledger.offer(_event(eid="typed", cone=[1, 2, 3])) == "failed"
    assert ledger.offer(_event(eid="big", samples=[1] * 400)) == "failed"
    assert ledger.offer(_event(eid="small", samples=[1, 2, 3])) == "accepted"
    blob = (tmp_path / "list.json").read_bytes()
    assert b"1,1,1,1" not in blob
    assert ledger.accounts()["A"] == 3 and ledger.accounts()["F"] == 2 and ledger.accounts()["P"] == 1
    finite, err = prepare_event(_event(eid="wide", heat=1e308))
    assert err is None and finite["payload"]["heat"] == 1e308


def test_concurrent_offers_admit_one_copy_of_each_key(tmp_path):
    ledger = Ledger(tmp_path / "race.json", ManualClock())
    barrier = threading.Barrier(24)
    results = []

    def go(i):
        barrier.wait(timeout=5)
        eid = "shared" if i < 12 else f"u-{i:02d}"
        results.append(ledger.offer(_event(eid=eid, text=f"t{i}")))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    accounts = ledger.accounts()
    assert accounts["A"] == 1 + 12  # one shared key plus 12 unique
    assert results.count("accepted") == accounts["A"]
    assert results.count("duplicate") == 11
    assert accounts["A"] == accounts["D"] + accounts["P"] + accounts["F"]
    reloaded = Ledger(tmp_path / "race.json", ManualClock())
    assert reloaded.accounts()["A"] == accounts["A"]
    shared = [row for row in reloaded.entries.values() if row["id"] == "shared"]
    assert len(shared) == 1 and shared[0]["event"]["payload"]["text"].startswith("t")


def test_state_file_edges(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 2,
        "cursor": "keep",
        "boots": {"cyclekernal": 4, "noise": True, "other": "x"},
        "entries": [
            {"source": "avrone", "id": "keep", "state": "delivered", "event": {"id": "keep"}, "attempts": True, "status": True},
            {"source": "nope", "id": "x", "state": "pending", "event": {}},
            {"source": "avrone", "id": "keep", "state": "pending", "event": {"id": "keep", "text": "second"}},
            "not-a-row",
            {"source": "greatscale", "id": "pend", "state": "pending", "event": {"id": "pend", "text": "p"}, "attempts": 3},
        ],
    }), encoding="utf-8")
    loaded = Ledger(path, ManualClock())
    assert loaded.boots == {"cyclekernal": "4", "other": "x"}
    assert loaded.accounts()["D"] == 1 and loaded.accounts()["P"] == 1
    kept = loaded.entries[("avrone", "keep")]
    assert kept["state"] == "delivered" and kept["attempts"] == 0 and kept["status"] is None
    assert loaded.offer(_event(eid="keep")) == "duplicate"

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"{" + b" " * (2 * 1024 * 1024 + 8))
    broken = Ledger(huge, ManualClock())
    assert broken.state_errors == 1 and broken.accounts()["A"] == 0

    link = tmp_path / "link.json"
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link.symlink_to(outside)
    skipped = Ledger(link, ManualClock())
    assert skipped.state_errors == 1
    assert outside.read_text(encoding="utf-8") == "secret"

    bad_version = tmp_path / "v3.json"
    bad_version.write_text('{"version":3,"entries":[]}', encoding="utf-8")
    assert Ledger(bad_version, ManualClock()).state_errors == 1


def test_config_reads_tokens_verbatim_and_rejects_junk_numbers(monkeypatch):
    monkeypatch.setenv("CK_FUSION_TOKEN", " spaced ")
    monkeypatch.setenv("FUSION_TOKEN", "other")
    monkeypatch.setenv("FUSION_TIMEOUT", "nan")
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "-4")
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "nope")
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "inf")
    cfg = config_from_env()
    assert cfg.token == " spaced "
    assert cfg.timeout == 5.0
    assert cfg.deadline == 0.0
    assert cfg.breaker_threshold == 1
    assert cfg.backoff_base == 0.5
    monkeypatch.setenv("CK_FUSION_TOKEN", "")
    assert config_from_env().token == "other"
    # A max below the base does not invert the schedule into a spin.
    narrow = _cfg(backoff_base=2.0, backoff_max=0.1)
    assert retry_delay("status429", 1, narrow) == 2.0
    assert retry_delay("status404", 5, _cfg(interval=0)) == 0.05


def test_deadline_under_50ms_does_not_send(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["200"])
    ledger = Ledger(tmp_path / "short.json", clock)
    ledger.offer(_event(eid="skip"))
    report = ledger.pump(tape, _cfg(deadline=0.04), max_cycles=1)
    assert tape.posts == 0 and report["P"] == 1 and report["D"] == 0 and report["F"] == 0


def test_mirror_spellings_are_suppressed(tmp_path):
    ledger = Ledger(tmp_path / "echo.json", ManualClock())
    cases = [
        _event(eid="ck-health-1"),
        _event(eid="ck-evidence-2"),
        _event(eid="plain", source="cyclekernal"),
        _event(eid="agent", agent="CycleKernal:Mood"),
    ]
    for case in cases:
        assert ledger.offer(case) == "suppressed"
    assert ledger.accounts()["A"] == 0 and ledger.accounts()["suppressed"] == 4


def test_duplicate_true_counts_as_delivered_once(tmp_path):
    clock = ManualClock()
    bodies = []

    def transport(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {"boot": "same"}, None)
        bodies.append(json.loads(body))
        return HttpResult(200, {"accepted": True, "duplicate": True, "id": bodies[-1]["id"]}, None)

    ledger = Ledger(tmp_path / "dup.json", clock)
    assert ledger.offer(_event(eid="once", text="original")) == "accepted"
    assert ledger.offer(_event(eid="once", text="rewrite")) == "duplicate"
    report = ledger.pump(transport, _cfg(), max_cycles=2)
    assert report["A"] == 1 and report["D"] == 1 and report["P"] == 0 and report["F"] == 0
    assert len(bodies) == 1 and bodies[0]["payload"]["text"] == "original"
    assert ledger.pump(transport, _cfg(), max_cycles=1)["calls"] == 0


def test_bidi_override_is_stored_on_the_board_and_rejected_in_beta_t(client, tmp_path):
    """Round 136 strips U+202E from one-line displays. The blackboard keeps the
    character in the stored post. beta_t rejects it because it is category Cf.
    one_line, which the bridge uses when printing a post, collapses it."""
    from avrone.tools import one_line
    raw = '{"id":"bidi-1","text":"ok\\u202eevil","ts":4}'.encode()
    r = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert r.status_code == 200, r.text
    text = client.get("/api/blackboard", headers=AUTH).json()["posts"][0]["text"]
    assert "\u202e" in text
    assert "\u202e" not in one_line(text)
    ledger = Ledger(tmp_path / "bidi.json", ManualClock())
    assert ledger.offer(_event(eid="bidi", beta_t="ok\u202eevil")) == "failed"
    assert "\u202e" not in (tmp_path / "bidi.json").read_text(encoding="utf-8")
    assert ledger.accounts()["F"] == 1 and ledger.accounts()["P"] == 0


def test_float_boot_does_not_look_like_a_restart(tmp_path):
    """A JSON number that is not an int is not a boot id. The same 1.5 twice
    must not resend, and must not flap against the string \"1.5\"."""
    clock = ManualClock()
    posts = []

    def transport(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {"status": "ghost-online", "boot": 1.5}, None)
        posts.append(json.loads(body)["id"])
        return HttpResult(200, {"accepted": True, "duplicate": False}, None)

    ledger = Ledger(tmp_path / "float-boot.json", clock)
    ledger.offer(_event(eid="stay"))
    assert ledger.pump(transport, _cfg(), max_cycles=1)["D"] == 1
    assert ledger.pump(transport, _cfg(), max_cycles=1)["calls"] == 0
    assert posts == ["stay"]
    assert "cyclekernal" not in ledger.boots


def test_padded_and_leading_zero_content_lengths(client):
    """4096 is in range. 0004097 is 4097 and is refused before the body is read."""

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = server.BodyLimit(app)

    async def drive(length: bytes):
        used = []

        async def receive():
            used.append(1)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "path": "/api/blackboard", "headers": [(b"content-length", length)]}
        await mw(scope, receive, send)
        return sent[0]["status"], used

    assert asyncio.run(drive(b"0000000004096")) == (200, [1])
    assert asyncio.run(drive(b"0004097")) == (413, [])
    # The chat route uses the process cap. A second header over that cap still refuses.
    chat_cap = server.max_body_bytes("/api/chat")

    async def drive_chat():
        used = []

        async def receive():
            used.append(1)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(msg):
            sent.append(msg)

        scope = {
            "type": "http", "path": "/api/chat",
            "headers": [(b"content-length", b"16"), (b"Content-Length", str(chat_cap + 1).encode())],
        }
        await mw(scope, receive, send)
        return sent[0]["status"], used

    assert asyncio.run(drive_chat()) == (413, [])


def test_numeric_edges_and_a_bad_source_is_not_admitted(tmp_path):
    raw = _event(eid="a.b:c-1", heat=0, cone_angle=-1, beta_t="")
    raw["run_id"] = "R" * 100
    kept, err = prepare_event(raw)
    assert err is None
    assert kept["payload"]["heat"] == 0
    assert kept["payload"]["cone_angle"] == -1
    assert kept["payload"]["beta_t"] == ""
    assert len(kept["run_id"]) == 80
    ledger = Ledger(tmp_path / "num.json", ManualClock())
    assert ledger.offer(kept) == "accepted"
    assert ledger.offer(_event(eid="bool-cone", cone_angle=True)) == "failed"
    long_beta = "b" * 600
    assert ledger.offer(_event(eid="long-beta", beta_t=long_beta)) == "failed"
    blob = (tmp_path / "num.json").read_text(encoding="utf-8")
    assert "b" * 100 not in blob
    with pytest.raises(ValueError, match="bad source"):
        ledger.offer(_event(eid="nope", source="AVRONE"))
    # A bad kind on a valid id is an explicit failure, not a dropped offer.
    assert ledger.offer({**_event(eid="kind"), "kind": "Finding"}) == "failed"
    assert ledger.accounts()["A"] == 4
    assert ledger.accounts()["F"] == 3 and ledger.accounts()["P"] == 1


def test_409_is_permanent_and_204_is_delivered(tmp_path):
    clock = ManualClock()

    def transport(method, url, body, headers, timeout, status):
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        transport.posts += 1
        transport.keys.append(headers.get("Idempotency-Key"))
        return HttpResult(status, None, None)

    transport.posts = 0
    transport.keys = []
    ledger = Ledger(tmp_path / "perm.json", clock)
    ledger.offer(_event(eid="conflict"))
    report = ledger.pump(lambda *a: transport(*a, 409), _cfg(), max_cycles=3)
    assert report["F"] == 1 and report["P"] == 0 and transport.posts == 1
    ledger.offer(_event(eid="empty-ok"))
    report = ledger.pump(lambda *a: transport(*a, 204), _cfg(), max_cycles=1)
    assert report["D"] == 1 and report["F"] == 1 and report["A"] == 2
    assert transport.keys == ["conflict", "empty-ok"]


def test_threshold_zero_does_not_open_and_a_short_deadline_leaves_the_second_pending(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["timeout", "timeout", "200"])
    ledger = Ledger(tmp_path / "thr.json", clock)
    ledger.offer(_event(eid="open"))
    report = ledger.pump(tape, _cfg(deadline=0, breaker_threshold=0, breaker_cooldown=30), max_cycles=3)
    assert report["D"] == 1 and tape.posts == 3
    assert ledger.breaker_for("http://127.0.0.1:8090").is_open(clock.t) is False

    clock = ManualClock()
    order = []

    def transport(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        order.append(json.loads(body)["id"])
        clock.sleep(0.2)
        return HttpResult(200, {"accepted": True, "duplicate": False}, None)

    ledger = Ledger(tmp_path / "two.json", clock)
    ledger.offer(_event(eid="first"))
    ledger.offer(_event(eid="second"))
    report = ledger.pump(transport, _cfg(deadline=0.24, timeout=1), max_cycles=1)
    assert order == ["first"]
    assert report["D"] == 1 and report["P"] == 1 and report["F"] == 0
    assert report["A"] == report["D"] + report["P"] + report["F"]


def test_endless_429_stops_at_64_and_stays_pending(tmp_path):
    clock = ManualClock()
    tape = Tape(clock, ["429"])
    ledger = Ledger(tmp_path / "cap.json", clock)
    ledger.offer(_event(eid="forever"))
    report = ledger.pump(tape, _cfg(deadline=0, backoff_base=0.05, backoff_max=0.05), max_cycles=1)
    assert tape.posts == 64
    assert report["A"] == 1 and report["P"] == 1 and report["D"] == 0 and report["F"] == 0
    assert report["attempts"] == 64
    assert clock.sleeps == [0.05] * 64
    assert sum(clock.sleeps) == pytest.approx(3.2)


def test_auth_on_the_first_key_does_not_send_the_second(tmp_path):
    clock = ManualClock()
    sent = []

    def transport(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        sent.append(json.loads(body)["id"])
        return HttpResult(401, {"detail": "Bad admin token"}, None)

    ledger = Ledger(tmp_path / "stop.json", clock)
    ledger.offer(_event(eid="one"))
    ledger.offer(_event(eid="two"))
    report = ledger.pump(transport, _cfg(token=""), max_cycles=1)
    assert sent == ["one"]
    report = ledger.pump(transport, _cfg(token=""), max_cycles=1)
    # The first call of the second pump is still one, because auth stopped
    # before two. A fresh cycle starts at the first pending key again.
    assert sent == ["one", "one"]
    assert report["A"] == 2 and report["P"] == 2 and report["F"] == 0 and report["D"] == 0
    assert [row["attempts"] for row in ledger.entries.values()] == [2, 0]


def test_empty_token_is_omitted_and_near_miss_mirrors_are_kept(tmp_path):
    clock = ManualClock()
    seen = []

    def transport(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {}, None)
        seen.append(headers)
        return HttpResult(200, {"accepted": True, "duplicate": False}, None)

    ledger = Ledger(tmp_path / "tok.json", clock)
    assert ledger.offer(_event(eid="keep-space", origin_node=" fusion-bridge")) == "accepted"
    assert ledger.offer(_event(eid="keep-role", role="cyclekernal-relay ")) == "accepted"
    report = ledger.pump(transport, _cfg(token=""), max_cycles=1)
    assert report["D"] == 2
    assert seen and all("X-Admin-Token" not in headers for headers in seen)


def test_surrogate_circular_and_deep_payloads_are_failures_not_crashes(tmp_path):
    ledger = Ledger(tmp_path / "bad.json", ManualClock())
    assert ledger.offer(_event(eid="surr", text="a\ud800b")) == "failed"
    circle: dict = {}
    circle["self"] = circle
    raw = _event(eid="circle")
    raw["payload"] = {"text": "x", "loop": circle}
    assert ledger.offer(raw) == "failed"
    # Deeper than MAX_NESTING (32) and far below CPython's recursion limit.
    # A 2000-deep tree makes json.dumps raise RecursionError; on 3.11 that
    # leaves the recursion counter high and the next deep walk in the process
    # fails (test_round114_deep_paths).
    node: dict = {"n": 0}
    cursor = node
    for _ in range(40):
        cursor["down"] = {}
        cursor = cursor["down"]
    deep = _event(eid="deep")
    deep["payload"] = {"text": "x", "tree": node}
    assert ledger.offer(deep) == "failed"
    text = (tmp_path / "bad.json").read_text(encoding="utf-8")
    assert "\ud800" not in text
    assert ledger.accounts()["F"] == 3 and ledger.accounts()["P"] == 0
    assert ledger.accounts()["A"] == ledger.accounts()["D"] + ledger.accounts()["P"] + ledger.accounts()["F"]


def test_board_post_round_trip_and_its_mirror_is_not_forwarded(client, tmp_path):
    from avrone.fusion_deliver import event_from_post
    posted = _post(client, {
        "id": "board-1", "text": "from the board", "kind": "question", "ts": 9,
        "agent": "scout", "origin_node": "local",
    })
    assert posted.status_code == 200
    row = client.get("/api/blackboard", headers=AUTH).json()["posts"][0]
    event = event_from_post(row)
    ledger = Ledger(tmp_path / "trip.json", ManualClock())
    assert ledger.offer(event) == "accepted"
    mirror = dict(row)
    mirror["id"] = "board-echo"
    mirror["origin_node"] = "fusion-bridge"
    assert ledger.offer(event_from_post(mirror)) == "suppressed"
    clock = ManualClock()
    ledger.clock = clock
    tape = Tape(clock, ["200"])
    report = ledger.pump(tape, _cfg(), max_cycles=1)
    assert report["A"] == 1 and report["D"] == 1 and report["suppressed"] == 1
    body = json.loads(tape.bodies[0])
    assert body["source"] == "avrone" and body["id"] == "board-1"
    assert body["payload"]["text"] == "from the board"
    assert body["kind"] == "question"


def test_loose_blackboard_shapes_do_not_500(client):
    assert _post(client, {"text": 1, "kind": " Finding "}).status_code == 200
    row = client.get("/api/blackboard", headers=AUTH).json()["posts"][0]
    assert row["text"] == "1" and row["kind"] == "finding"
    assert _post(client, {"id": "refs-dict", "text": "r", "refs": {"a": 1}}).status_code == 200
    stored = [p for p in client.get("/api/blackboard", headers=AUTH).json()["posts"] if p["id"] == "refs-dict"][0]
    assert stored["refs"] == []
    raw = b'{"id":"surr-1","text":"a\\ud800b","ts":3}'
    r = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert r.status_code == 200, r.text
    got = client.get("/api/blackboard", headers=AUTH)
    assert got.status_code == 200
    text = [p for p in got.json()["posts"] if p["id"] == "surr-1"][0]["text"]
    assert "\ud800" in text


def test_order_directory_does_not_hang_and_junk_pending_rows_are_dropped(client, tmp_path):
    assert _post(client, {"id": "dir-1", "text": "kept", "ts": 2}).status_code == 200
    core = server.state["core"]
    order = core.settings.data_dir / "bridge" / "_fanin_order.json"
    order.unlink()
    order.mkdir()
    del core._blackboard_order
    started = time.perf_counter()
    ids = [p["id"] for p in blackboard_posts(core)]
    assert time.perf_counter() - started < 2.0
    assert ids == ["dir-1"]

    path = tmp_path / "rows.json"
    path.write_text(json.dumps({
        "version": 2,
        "entries": [
            {"source": "avrone", "id": "ghost", "state": "pending", "event": None},
            {"source": "avrone", "id": "real", "state": "pending", "event": {"id": "real", "text": "yes"}, "attempts": 2},
        ],
    }), encoding="utf-8")
    loaded = Ledger(path, ManualClock())
    assert loaded.accounts()["A"] == 1 and loaded.accounts()["P"] == 1
    assert "ghost" not in {row["id"] for row in loaded.entries.values()}


def test_redirect_does_not_open_the_breaker_or_leave_the_origin(tmp_path):
    """A 302 is not a dead peer. The token stays on the first origin."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hit = {"first": 0, "second": 0, "tokens": []}

    class First(BaseHTTPRequestHandler):
        def do_POST(self):
            hit["first"] += 1
            hit["tokens"].append(self.headers.get("X-Admin-Token"))
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{second.server_address[1]}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            body = b'{"boot":"r"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    class Second(BaseHTTPRequestHandler):
        def do_POST(self):
            hit["second"] += 1
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, fmt, *args):
            return

    from avrone.fusion_deliver import httpx_transport
    second = ThreadingHTTPServer(("127.0.0.1", 0), Second)
    first = ThreadingHTTPServer(("127.0.0.1", 0), First)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (first, second)]
    for thread in threads:
        thread.start()
    try:
        ledger = Ledger(tmp_path / "redir.json")
        ledger.offer(_event(eid="redir"))
        config = _cfg(
            base=f"http://127.0.0.1:{first.server_address[1]}",
            token="stay-here", timeout=2, deadline=5, backoff_base=0.05, backoff_max=0.05,
        )
        # Scripted 302 is the accounting check. The live pair checks the client
        # does not follow Location.
        clock = ManualClock()
        tape = Tape(clock, ["302", "200"])
        scripted = Ledger(tmp_path / "script.json", clock)
        scripted.offer(_event(eid="scripted"))
        scripted_report = scripted.pump(tape, _cfg(deadline=0, backoff_base=0.05, backoff_max=0.05), max_cycles=2)
        assert scripted_report["D"] == 1 and scripted_report["F"] == 0
        assert scripted.breaker_for(config.base).fails == 0
        live = ledger.pump(httpx_transport, config, max_cycles=1)
    finally:
        first.shutdown(); second.shutdown()
        first.server_close(); second.server_close()
    assert hit["first"] == 1 and hit["second"] == 0
    assert hit["tokens"] == ["stay-here"]
    assert live["P"] == 1 and live["D"] == 0 and live["F"] == 0
    assert ledger.breaker_for(config.base).is_open(0) is False


def test_symlink_journal_is_not_a_second_copy_of_the_board(client, tmp_path):
    assert _post(client, {"id": "real-1", "text": "on the board", "ts": 2}).status_code == 200
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        '{"id":"secret-post","kind":"finding","text":"from outside","refs":[],"ts":1}\n',
        encoding="utf-8",
    )
    folder = server.state["core"].settings.data_dir / "bridge"
    link = folder / "outside.jsonl"
    link.symlink_to(outside)
    ids = [p["id"] for p in client.get("/api/blackboard", headers=AUTH).json()["posts"]]
    assert ids == ["real-1"]
    assert "secret-post" not in ids


def test_null_kind_and_blank_run_id_have_defaults(client):
    r = _post(client, {"text": "defaults", "kind": None, "run_id": "   ", "ts": 6})
    assert r.status_code == 200, r.text
    row = client.get("/api/blackboard", headers=AUTH).json()["posts"][0]
    assert row["kind"] == "finding"
    assert row["run_id"] == "run"
    assert _post(client, {"id": " spaced ", "text": "no"}).status_code == 400


def test_negative_attempts_and_a_non_list_entry_table(tmp_path):
    path = tmp_path / "neg.json"
    path.write_text(json.dumps({
        "version": 2,
        "entries": [
            {"source": "avrone", "id": "neg", "state": "delivered", "event": {"id": "neg"}, "attempts": -4},
        ],
    }), encoding="utf-8")
    loaded = Ledger(path, ManualClock())
    assert loaded.entries[("avrone", "neg")]["attempts"] == 0
    bad = tmp_path / "dict.json"
    bad.write_text('{"version":2,"entries":{"source":"avrone"}}', encoding="utf-8")
    assert Ledger(bad, ManualClock()).state_errors == 1


def test_blank_base_falls_back_and_max_cycles_zero_still_tries_once(monkeypatch, tmp_path):
    monkeypatch.setenv("CK_BASE", "   ")
    monkeypatch.delenv("CK_FUSION_TOKEN", raising=False)
    monkeypatch.delenv("FUSION_TOKEN", raising=False)
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", "from-admin")
    assert config_from_env().base == "http://127.0.0.1:8090"
    assert config_from_env().token == "from-admin"
    clock = ManualClock()
    tape = Tape(clock, ["200"])
    ledger = Ledger(tmp_path / "zero.json", clock)
    ledger.offer(_event(eid="zero"))
    report = ledger.pump(tape, _cfg(), max_cycles=0)
    assert report["D"] == 1 and tape.posts == 1
