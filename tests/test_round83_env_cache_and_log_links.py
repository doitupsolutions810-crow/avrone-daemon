"""Round 83.

1. redact() rebuilt the secret-value list from os.environ for every string of every CodeX event:
   751 us per call with 500 variables. It is now cached against a snapshot of the environment, so
   a variable set or removed at runtime still applies on the very next call.
2. Codex._append opened runs/<run>.jsonl with path.open("ab+"): a symlink planted there made every
   event of that run append to the link's target, and a FIFO raised io.UnsupportedOperation out of
   emit(). Such a log is now skipped (Codex.dropped) and subscribers still get the event.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from avrone import ctxspawn as c
from avrone.codex import Codex


@pytest.fixture
def many_env(monkeypatch):
    for i in range(150):
        monkeypatch.setenv(f"R83SVC{i}_TOKEN", f"r83tokvalue{i:04d}")
    for i in range(300):
        monkeypatch.setenv(f"R83PLAIN{i}", "v")
    yield


def test_env_secrets_follow_runtime_changes(monkeypatch, many_env):
    assert c.redact("a r83tokvalue0007 b") == f"a {c.REDACTED} b"
    monkeypatch.setenv("R83_NEW_SECRET", "freshsecret123")
    assert c.redact("x freshsecret123 y") == f"x {c.REDACTED} y"
    monkeypatch.delenv("R83_NEW_SECRET")
    assert c.redact("x freshsecret123 y") == "x freshsecret123 y"
    monkeypatch.setenv("R83SVC3_TOKEN", "changedvalue77")
    assert c.redact("changedvalue77 r83tokvalue0003") == f"{c.REDACTED} r83tokvalue0003"


def test_env_secret_list_is_cached(many_env):
    c._env_secret_values()
    calls = []
    import avrone.tools_extra as te
    orig = te.is_secret_name
    te.is_secret_name = lambda n: calls.append(n) or orig(n)
    try:
        for _ in range(50):
            c._env_secret_values()
    finally:
        te.is_secret_name = orig
    assert calls == []


def test_short_redact_is_cheap_with_many_env_vars(many_env):
    c.redact("warm")
    n = 3000
    t0 = time.perf_counter()
    for _ in range(n):
        c.redact("hello world, nothing secret here")
    per = (time.perf_counter() - t0) / n
    # the uncached rebuild alone, for comparison on this machine
    from avrone.tools_extra import is_secret_name
    t0 = time.perf_counter()
    for _ in range(200):
        sorted({v for k, v in os.environ.items() if is_secret_name(k) and v and len(v) >= 6}, key=len)
    rebuild = (time.perf_counter() - t0) / 200
    assert per < rebuild / 3


def _victim(tmp_path):
    v = tmp_path / "victim.txt"
    v.write_text("keep")
    return v


def test_symlinked_run_log_is_not_followed(tmp_path):
    cx = Codex(tmp_path)
    v = _victim(tmp_path)
    os.symlink(v, cx.log_dir / "r1.jsonl")
    ev = cx.emit("r1", "note", "A", x=1)
    assert v.read_text() == "keep"
    assert getattr(cx, "dropped", 0) == 1 and ev.kind == "note"
    cx.emit("r2", "note", "A", x=2)
    assert '"x": 2' in (cx.log_dir / "r2.jsonl").read_text() or '"x":2' in (cx.log_dir / "r2.jsonl").read_text()


def test_fifo_run_log_does_not_raise_or_hang(tmp_path):
    cx = Codex(tmp_path)
    os.mkfifo(cx.log_dir / "r9.jsonl")
    err = []

    def go():
        try:
            cx.emit("r9", "note", "A", x=1)
        except Exception as e:  # noqa: BLE001
            err.append(e)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and err == []
    assert getattr(cx, "dropped", 0) == 1


def test_torn_line_still_repaired(tmp_path):
    cx = Codex(tmp_path)
    p = cx.log_dir / "r5.jsonl"
    p.write_bytes(b'{"torn": tr')
    cx.emit("r5", "note", "A", x=1)
    lines = p.read_bytes().split(b"\n")
    assert lines[0] == b'{"torn": tr' and lines[1].startswith(b"{")
