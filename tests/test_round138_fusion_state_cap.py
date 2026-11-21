"""Round 138: fusion_peer state and order files load back at the size they are written.

v0.130: FusionWatch._save and fusion_peer._save_order wrote any size, but both loaders used
_STATE_CAP = 1,000,000 bytes. After about 66k delivered posts (12-character ids), a restart hit
FileTooBig. The watchdog then started with an empty `seen` set and stopped saving state for good
(_writable False), so every post was offered to CycleKernal again on every cycle.
The order file fell back to ts order, which is the round 137 cursor bug.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as S

from avrone import fusion_peer as fp


def test_watch_seen_survives_restart_past_one_megabyte(tmp_path):
    state = tmp_path / "fusion_state.json"
    w = fp.FusionWatch(state)
    w.seen = {f"{i:012x}" for i in range(80_000)}
    assert w._save()
    assert state.stat().st_size > 1_000_000
    again = fp.FusionWatch(state)
    assert again.state_errors == 0
    assert again._writable
    assert len(again.seen) == 80_000


def test_peer_order_survives_restart_past_one_megabyte(tmp_path):
    (tmp_path / "bridge").mkdir()
    ids = [f"p{i:011d}" for i in range(80_000)]
    core = S(settings=S(data_dir=tmp_path))
    from collections import OrderedDict
    fp._save_order(core, OrderedDict((i, None) for i in ids))
    path = tmp_path / "bridge" / fp._ORDER_NAME
    assert path.stat().st_size > 1_000_000
    loaded = fp._load_order(S(settings=S(data_dir=tmp_path)))
    assert list(loaded) == ids


def test_caps_hold_many_ids():
    assert fp._STATE_CAP >= 64 * 1024 * 1024


def _event(i, text="x" * 500):
    return {"source": "avrone", "id": f"e{i}", "kind": "finding", "run_id": "r",
            "payload": {"text": text, "a": "y" * 500, "b": "z" * 500}, "ts": 1}


def test_ledger_pending_events_survive_restart_past_two_megabytes(tmp_path):
    from avrone import fusion_deliver as fd
    path = tmp_path / "ledger.json"
    led = fd.Ledger(path)
    led.entries.clear()
    for i in range(1400):
        event, err = fd.prepare_event(_event(i))
        assert err is None
        led.entries[("avrone", f"e{i}")] = led._row(("avrone", f"e{i}"), event, "pending", None)
    led.save()
    assert path.stat().st_size > 2 * 1024 * 1024
    again = fd.Ledger(path)
    assert again.accounts()["P"] == 1400
    assert again.state_errors == 0


def test_ledger_too_big_is_not_overwritten(tmp_path, monkeypatch):
    from avrone import fusion_deliver as fd
    path = tmp_path / "ledger.json"
    led = fd.Ledger(path)
    for i in range(3):
        assert led.offer(_event(i, "hi")) == "accepted"
    before = path.read_bytes()
    monkeypatch.setattr(fd, "STATE_MAX_BYTES", 100)
    again = fd.Ledger(path)
    assert again.state_errors == 1
    again.offer(_event(99, "new"))
    assert path.read_bytes() == before  # the events it could not read are still on disk
