"""Large live integer data must not stop durable event delivery or merge map keys."""
import json

import pytest

from avrone.codex import Codex, dumps_event, jsonable, wire_text


@pytest.fixture(autouse=True)
def decimal_guard(monkeypatch):
    import sys
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    monkeypatch.setenv("AVRONE_CODEX_FIELD_MAX_CHARS", "1000000")
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize("redact", ["on", "off"])
def test_large_live_integers_persist_and_deliver_with_distinct_keys(tmp_path, monkeypatch, redact):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", redact)
    giant = 10 ** 5000
    codex = Codex(tmp_path)
    queue = codex.subscribe()
    ev = codex.emit("giant", "tool.result", "worker", positive=giant, negative=-giant,
                    grid={giant: "first", giant + 1: "second", -giant: "negative", 7: "normal"})
    assert queue.get_nowait() is ev
    assert ev.data["positive"] == hex(giant)
    assert ev.data["negative"] == hex(-giant)
    assert ev.data["grid"] == {hex(giant): "first", hex(giant + 1): "second",
                               hex(-giant): "negative", "7": "normal"}
    assert codex.health()["write_errors"] == 0
    stored = codex.replay("giant")
    assert len(stored) == 1 and stored[0]["data"] == ev.data
    assert json.loads(wire_text(ev))["data"] == ev.data


def test_direct_event_serialization_handles_large_values_and_integer_keys():
    giant = 10 ** 5000
    encoded = dumps_event({"value": giant, "grid": {giant: "entry"}})
    assert json.loads(encoded) == {"value": hex(giant), "grid": {hex(giant): "entry"}}
    normal = {"value": 10 ** 100, "grid": {8: 9}, "enabled": True}
    assert dumps_event(normal) == json.dumps(normal)
    assert jsonable(normal) == {"value": 10 ** 100, "grid": {"8": 9}, "enabled": True}


def test_large_integer_projection_uses_existing_field_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_FIELD_MAX_CHARS", "1024")
    giant = 10 ** 5000
    codex = Codex(tmp_path)
    ev = codex.emit("capped-int", "tool.result", "worker", value=giant, grid={giant: "entry"})
    projected = ev.data["value"]
    assert projected.startswith(hex(giant)[:1024])
    assert "...[clipped " in projected and len(projected) < 1100
    assert list(ev.data["grid"]) == [projected]
    assert codex.replay("capped-int")[0]["data"] == ev.data
