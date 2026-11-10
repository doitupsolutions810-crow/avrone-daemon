"""Delivery ledger: dedup, backoff, 500 recovery, boot, and bounded payloads.

Times below are seconds on ManualClock unless a test says wall clock.
Identity: A = D + P + F over unique (source, id). Retries do not change A.
"""
import json
import time

import pytest

from avrone.fusion_deliver import (
    DeliverConfig,
    HttpResult,
    Ledger,
    ManualClock,
    parse_retry_after,
    prepare_event,
)

pytestmark = pytest.mark.filterwarnings("ignore")


def _cfg(**kw) -> DeliverConfig:
    base = dict(
        base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
        token="tok", timeout=5, deadline=0, breaker_threshold=1, breaker_cooldown=30,
        backoff_base=0.5, backoff_max=8, interval=1,
    )
    base.update(kw)
    return DeliverConfig(**base)


def _event(eid="e1", source="avrone", text="heat on the cone", **payload):
    body = {"text": text, "heat": 0.2, "cone": 0.38, "beta_t": 1.0}
    body.update(payload)
    return {
        "id": eid, "source": source, "kind": "finding", "run_id": "run-1",
        "payload": body, "ts": 1700000000.0,
    }


class Script:
    def __init__(self, clock: ManualClock):
        self.clock = clock
        self.posts = 0
        self.gets = 0
        self.bodies = []
        self.boot = "boot-1"
        self.mode = "ok"
        self.hold_s = 0.0
        self.retry_after = None
        self.seen_keys = []

    def __call__(self, method, url, body, headers, timeout):
        if method == "GET":
            self.gets += 1
            if self.boot is None:
                return HttpResult(200, {"status": "ghost-online"}, None)
            return HttpResult(200, {"status": "ghost-online", "boot": self.boot}, None)
        self.posts += 1
        self.seen_keys.append(headers.get("Idempotency-Key"))
        if body:
            self.bodies.append(body)
        if self.mode == "429" and self.posts <= 20:
            return HttpResult(429, {"detail": "slow down"}, None, self.retry_after)
        if self.mode == "500" and self.posts <= 10:
            if self.hold_s:
                self.clock.sleep(self.hold_s)
            return HttpResult(500, {"detail": "later"}, None)
        if self.mode == "401":
            return HttpResult(401, {"detail": "Bad admin token"}, None)
        if self.mode == "413":
            return HttpResult(413, {"detail": "request body too large (max 4096 bytes)"}, None)
        if self.mode == "timeout":
            return HttpResult(0, None, "timeout")
        if self.mode == "refused":
            return HttpResult(0, None, "refused")
        return HttpResult(200, {"accepted": True, "duplicate": False, "id": "e1"}, None)


def test_twenty_429s_use_bounded_backoff_then_recover(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    script.mode = "429"
    ledger = Ledger(tmp_path / "state.json", clock)
    assert ledger.offer(_event()) == "accepted"
    for _ in range(20):
        assert ledger.offer(_event(text="replacement")) == "duplicate"
    wall0 = time.perf_counter()
    report = ledger.pump(script, _cfg(deadline=0), max_cycles=4)
    wall = time.perf_counter() - wall0
    delays = clock.sleeps
    assert delays[0] == 0.5
    assert delays[4] == 8.0
    assert delays[-1] == 8.0
    assert all(d > 0 for d in delays)
    # 0.5*(1+2+4+8+16) + 8*15 = 15.5 + 120 = 135.5 seconds of virtual wait.
    assert delays == [min(8.0, 0.5 * (2 ** n)) for n in range(20)]
    assert sum(delays) == pytest.approx(135.5)
    assert script.posts == 21
    assert report == {
        "A": 1, "D": 1, "P": 0, "F": 0, "attempts": 21, "suppressed": 0,
        "cycles": 1, "calls": 21, "clock_s": 135.5,
    }
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert stored["entries"][0]["event"]["payload"]["text"] == "heat on the cone"
    assert wall < 2.0  # the 135.5 s is virtual; the process did not sleep it


def test_deadline_stops_a_429_storm_without_dropping(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    script.mode = "429"
    ledger = Ledger(tmp_path / "state.json", clock)
    ledger.offer(_event())
    report = ledger.pump(script, _cfg(deadline=1.0, backoff_base=0.5, backoff_max=8), max_cycles=1)
    assert script.posts < 20
    assert report["A"] == 1 and report["P"] == 1 and report["D"] == 0 and report["F"] == 0
    assert report["attempts"] == script.posts
    assert clock.t <= 1.0 + 1e-9 or clock.sleeps[-1] > 0


def test_ten_delayed_500s_then_recovery_does_not_open_the_breaker(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    script.mode = "500"
    script.hold_s = 2.0
    ledger = Ledger(tmp_path / "state.json", clock)
    ledger.offer(_event())
    # 500 uses the same exponential cap as 429. The 2 s is the peer holding the socket.
    report = ledger.pump(
        script, _cfg(deadline=0, backoff_base=0.05, backoff_max=0.2), max_cycles=12,
    )
    assert script.posts == 11
    assert report["A"] == 1 and report["D"] == 1 and report["P"] == 0 and report["F"] == 0
    assert report["attempts"] == 11
    holds = [d for d in clock.sleeps if d == 2.0]
    backs = [d for d in clock.sleeps if d != 2.0]
    assert holds == [2.0] * 10
    assert backs == [min(0.2, 0.05 * (2 ** n)) for n in range(10)]
    assert ledger.breaker_for("http://127.0.0.1:8090").fails == 0
    assert ledger.breaker_for("http://[::1]:8090").is_open(clock.t) is False
    assert sum(holds) == 20.0


def test_timeout_opens_one_loopback_breaker_for_v4_and_v6(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    script.mode = "timeout"
    ledger = Ledger(tmp_path / "v4.json", clock)
    ledger.offer(_event())
    report = ledger.pump(script, _cfg(deadline=0), max_cycles=2)
    assert report["P"] == 1 and report["D"] == 0 and report["F"] == 0
    assert script.posts == 1  # the second cycle is skipped while the breaker is open
    assert ledger.breaker_for("http://[::1]:8090").is_open(clock.t) is True
    assert ledger.breaker_for("http://[::ffff:127.0.0.1]:8090").is_open(clock.t) is True
    assert ledger.breaker_for("http://10.0.0.5:8090").is_open(clock.t) is False


def test_auth_failure_is_not_delivered_and_does_not_backoff(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    script.mode = "401"
    ledger = Ledger(tmp_path / "auth.json", clock)
    ledger.offer(_event())
    report = ledger.pump(script, _cfg(deadline=0), max_cycles=3)
    assert report["P"] == 1 and report["D"] == 0 and report["F"] == 0
    assert script.posts == 1
    assert clock.sleeps == []


def test_peer_413_is_an_explicit_failure(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    script.mode = "413"
    ledger = Ledger(tmp_path / "big.json", clock)
    ledger.offer(_event())
    report = ledger.pump(script, _cfg(), max_cycles=2)
    assert report["A"] == 1 and report["F"] == 1 and report["P"] == 0 and report["D"] == 0
    assert script.posts == 1


def test_source_and_id_first_write_wins(tmp_path):
    ledger = Ledger(tmp_path / "dedup.json", ManualClock())
    assert ledger.offer(_event(eid="same", source="avrone", text="first")) == "accepted"
    assert ledger.offer(_event(eid="same", source="avrone", text="second")) == "duplicate"
    assert ledger.offer(_event(eid="same", source="greatscale", text="other")) == "accepted"
    assert ledger.accounts()["A"] == 2
    texts = [row["event"]["payload"]["text"] for row in ledger.entries.values()]
    assert texts == ["first", "other"]


def test_boot_change_requeues_delivery_and_a_missing_boot_does_not(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    ledger = Ledger(tmp_path / "boot.json", clock)
    ledger.offer(_event())
    first = ledger.pump(script, _cfg(), max_cycles=2)
    assert first["D"] == 1 and script.posts == 1
    script.boot = "boot-2"
    second = ledger.pump(script, _cfg(), max_cycles=2)
    assert second["D"] == 1 and second["P"] == 0
    assert script.posts == 2
    script.boot = None
    third = ledger.pump(script, _cfg(), max_cycles=1)
    assert third["D"] == 1 and script.posts == 2


def test_mirror_is_suppressed_not_dropped_into_the_failure_count(tmp_path):
    ledger = Ledger(tmp_path / "echo.json", ManualClock())
    echo = _event(eid="ck-mood-abc", text='{"agent":"cyclekernal:mood"}')
    echo["payload"]["origin_node"] = "fusion-bridge"
    echo["payload"]["role"] = "cyclekernal-relay"
    assert ledger.offer(echo) == "suppressed"
    accounts = ledger.accounts()
    assert accounts["A"] == 0 and accounts["suppressed"] == 1
    assert accounts["A"] == accounts["D"] + accounts["P"] + accounts["F"]


def test_heat_cone_and_beta_keep_finite_numbers_and_refuse_the_rest(tmp_path):
    ledger = Ledger(tmp_path / "heat.json", ManualClock())
    small = prepare_event(_event(cone_angle=1.25, beta_t="beta \U0001F525"))[0]
    assert small["payload"]["heat"] == 0.2
    assert small["payload"]["cone"] == 0.38
    assert small["payload"]["cone_angle"] == 1.25
    assert small["payload"]["beta_t"] == "beta \U0001F525"
    assert ledger.offer(_event(eid="bad-heat", heat=True)) == "failed"
    assert ledger.offer(_event(eid="bad-cone", cone_angle="  1.25  ")) == "failed"
    assert ledger.offer(_event(eid="bad-beta", beta_t="\u200bhidden")) == "failed"
    assert ledger.offer(_event(eid="huge", heat="H" * 100_000, cone=[1] * 20_000, beta_t="B" * 100_000)) == "failed"
    blob = (tmp_path / "heat.json").read_bytes()
    assert b"HHHH" not in blob and b"BBBB" not in blob
    assert ledger.accounts()["F"] == 4
    assert ledger.accounts()["A"] == ledger.accounts()["D"] + ledger.accounts()["P"] + ledger.accounts()["F"]


def test_unshrinkable_body_is_failed_and_not_sent(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    ledger = Ledger(tmp_path / "huge.json", clock)
    payload = {f"k{i:02d}": "x" * 2000 for i in range(50)}
    assert ledger.offer(_event(**payload)) == "failed"
    report = ledger.pump(script, _cfg(), max_cycles=2)
    assert report["A"] == 1 and report["F"] == 1 and report["P"] == 0
    assert script.posts == 0
    blob = (tmp_path / "huge.json").read_bytes()
    assert b"xxxx" not in blob


def test_version1_seen_and_corrupt_state(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "seen": ["legacy1"], "cursor": "legacy1"}), encoding="utf-8")
    loaded = Ledger(path, ManualClock())
    assert loaded.accounts()["D"] == 1
    assert loaded.offer(_event(eid="legacy1")) == "duplicate"
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")
    broken = Ledger(bad, ManualClock())
    assert broken.state_errors == 1
    assert broken.accounts()["A"] == 0


def test_retry_after_is_honored_and_garbage_falls_back(tmp_path):
    assert parse_retry_after("0.05") == 0.05
    assert parse_retry_after("0") == 0.05
    for bad in ("", "-1", "+1", "soon", "999999", "Wed, 21 Oct 2015 07:28:00 GMT"):
        assert parse_retry_after(bad) is None
    clock = ManualClock()
    script = Script(clock)
    script.mode = "429"
    script.retry_after = "0.05"
    ledger = Ledger(tmp_path / "rate.json", clock)
    ledger.offer(_event())
    report = ledger.pump(script, _cfg(deadline=0, backoff_base=0.5, backoff_max=8), max_cycles=2)
    assert report["D"] == 1 and script.posts == 21
    assert clock.sleeps == [0.05] * 20


def test_zero_backoff_cannot_spin(tmp_path):
    clock = ManualClock()
    script = Script(clock)
    script.mode = "429"
    ledger = Ledger(tmp_path / "floor.json", clock)
    ledger.offer(_event())
    ledger.pump(script, _cfg(deadline=0.2, backoff_base=0, backoff_max=0), max_cycles=1)
    assert clock.sleeps
    assert min(clock.sleeps) >= 0.05
    assert ledger.accounts()["P"] == 1
