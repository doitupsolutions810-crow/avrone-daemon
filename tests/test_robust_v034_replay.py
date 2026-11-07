"""v0.34: run-log reads are streamed and bounded. Before: Codex.replay read the whole log and parsed
every line, on the event loop; live, GET /api/runs/<id> on a 200 MB log took 7.6 s, stalled every
other request for 7.1 s, returned 212 MB and peaked at 1.37 GB RSS. A newline-less log was also
read whole by runs() just to list it."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from avrone import codex as codex_mod
from avrone import server
from avrone.codex import Codex, iter_lines


def ev(i):
    return json.dumps({"run_id": "r", "kind": "k", "agent": "a", "data": {"i": i}, "ts": i}) + "\n"


def test_iter_lines_skips_overlong_without_loading():
    f = io.StringIO("a\n" + "x" * 50 + "\nb\nlast")
    assert list(iter_lines(f, max_line=8)) == ["a\n", None, "b\n", "last"]
    assert list(iter_lines(io.StringIO("z" * 20), max_line=8)) == [None]


def test_replay_keeps_newest_and_skips_giant_line(tmp_path, monkeypatch):
    cx = Codex(tmp_path)
    body = "".join(ev(i) for i in range(10)) + '{"huge": "' + "q" * 1000 + '"}\n' + ev(10)
    (cx.log_dir / "r.jsonl").write_text(body)
    monkeypatch.setattr(codex_mod.iter_lines, "__defaults__", (200,))
    out = cx.replay("r", limit=4)
    assert [e["data"]["i"] for e in out] == [7, 8, 9, 10]
    monkeypatch.setenv("AVRONE_REPLAY_MAX_EVENTS", "3")
    assert [e["data"]["i"] for e in cx.replay("r")] == [8, 9, 10]


def test_replay_limit_env(monkeypatch):
    assert codex_mod.replay_limit() == 2000
    for v, want in (("5", 5), ("0", 1), ("10000000", 50_000), ("x", 2000)):
        monkeypatch.setenv("AVRONE_REPLAY_MAX_EVENTS", v)
        assert codex_mod.replay_limit() == want


def test_replay_unreadable_and_missing(tmp_path, monkeypatch):
    cx = Codex(tmp_path)
    assert cx.replay("nope") == []
    (cx.log_dir / "r.jsonl").write_text(ev(1))

    def boom(*a, **k):
        raise OSError("gone")
    monkeypatch.setattr(type(cx.log_dir / "r.jsonl"), "open", boom)
    monkeypatch.setattr(codex_mod, "open_log", boom, raising=False)  # round 88: readers use open_log
    assert cx.replay("r") == []


def test_runs_lists_without_reading_newline_less_log(tmp_path, monkeypatch):
    cx = Codex(tmp_path)
    monkeypatch.setattr(codex_mod.iter_lines, "__defaults__", (64,))
    (cx.log_dir / "bad.jsonl").write_text('{"a": "' + "y" * 500)
    (cx.log_dir / "ok.jsonl").write_text(json.dumps({"ts": 1, "data": {"prompt": "hi"}}) + "\n")
    (cx.log_dir / "empty.jsonl").write_text("")
    assert [r["run_id"] for r in cx.runs()] == ["ok"]


def test_api_truncates_with_header(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("AVRONE_REPLAY_MAX_EVENTS", "3")
    with TestClient(server.app) as c:
        logs = server.core().codex.log_dir
        (logs / "big.jsonl").write_text("".join(ev(i) for i in range(10)))
        (logs / "small.jsonl").write_text("".join(ev(i) for i in range(3)))
        r = c.get("/api/runs/big")
        assert r.status_code == 200 and [e["data"]["i"] for e in r.json()] == [7, 8, 9]
        assert "AVRONE_REPLAY_MAX_EVENTS" in r.headers["x-avrone-truncated"]
        r = c.get("/api/runs/small")
        assert len(r.json()) == 3 and "x-avrone-truncated" not in r.headers
        assert c.get("/api/runs/none").status_code == 404
