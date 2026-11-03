"""Adversarial and chaos coverage for the run-scoped AVRONE bridge.

No external services are contacted. Tests exercise hostile relay payloads,
concurrent writers, torn persistence, bounded retention and failure isolation.
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from avrone.bridge import Bridge, BridgeError, safe_run_id


def _remote(post_id: str, **overrides):
    row = {
        "id": post_id, "kind": "finding", "text": f"remote-{post_id}",
        "agent": "remote-agent", "role": "worker", "refs": [],
        "ts": 1_700_000_000, "origin_node": "origin-A",
    }
    row.update(overrides)
    return row


def test_remote_merge_rejects_hostile_shapes_without_crashing(tmp_path):
    bridge = Bridge("relay", tmp_path)
    circular = {"id": "cycle", "kind": "finding", "text": "cycle"}
    circular["self"] = circular
    payload = [
        None, 7, "text", [], {}, _remote("bad kind", kind="exec"),
        _remote("bad kind value", kind=[]), _remote("bad text", text={"nested": True}),
        _remote("bad id", id="../escape"), circular, _remote("valid-1"),
    ]
    stats = bridge.merge_remote(payload, "fallback-node")
    assert stats == {"merged": 1, "skipped": 0, "rejected": 10}
    assert bridge.get("valid-1")["text"] == "remote-valid-1"


def test_remote_timestamp_bool_nan_inf_and_strings_are_normalized(tmp_path):
    bridge = Bridge("timestamps", tmp_path)
    rows = [
        _remote("bool-ts", ts=True), _remote("nan-ts", ts=float("nan")),
        _remote("inf-ts", ts=float("inf")), _remote("str-ts", ts="1700000000"),
        _remote("good-ts", ts=1_700_000_000),
    ]
    assert bridge.merge_remote(rows, "peer")["merged"] == 5
    for row in bridge.posts():
        assert isinstance(row["ts"], float)
        assert math.isfinite(row["ts"]) and row["ts"] > 0
    assert bridge.get("good-ts")["ts"] == 1_700_000_000.0


def test_remote_merge_duplicate_never_overwrites_local_post(tmp_path):
    bridge = Bridge("dedupe", tmp_path)
    local = bridge.post("local", "finding", "trusted local text")
    forged = dict(local, text="forged", agent="attacker", origin_node="attacker")
    assert bridge.merge_remote([forged], "remote") == {"merged": 0, "skipped": 1, "rejected": 0}
    assert bridge.get(local["id"])["text"] == "trusted local text"
    assert bridge.get(local["id"])["origin_node"] == "local"


def test_remote_refs_are_bounded_deduplicated_and_validated(tmp_path):
    bridge = Bridge("refs", tmp_path)
    refs = [f"ref-{i}" for i in range(30)] + ["ref-1", "../bad", ""]
    bridge.merge_remote([_remote("with-refs", refs=refs)], "peer")
    got = bridge.get("with-refs")["refs"]
    assert len(got) == 20
    assert len(got) == len(set(got))
    assert all("/" not in ref for ref in got)


def test_two_bridge_instances_share_file_lock_for_concurrent_posts(tmp_path):
    left = Bridge("shared", tmp_path, node_id="left")
    right = Bridge("shared", tmp_path, node_id="right")

    def post_many(pair):
        bridge, worker = pair
        for index in range(60):
            bridge.post(worker, "finding", f"{worker}:{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(post_many, [(left, f"L{i}") for i in range(4)] + [(right, f"R{i}") for i in range(4)]))

    replay = Bridge("shared", tmp_path)
    assert len(replay) == 480
    assert len({post["id"] for post in replay.posts()}) == 480
    assert {post["agent"] for post in replay.posts()} == {f"L{i}" for i in range(4)} | {f"R{i}" for i in range(4)}


def test_torn_final_jsonl_record_does_not_hide_next_write(tmp_path):
    bridge = Bridge("torn", tmp_path)
    first = bridge.post("a", "finding", "first")
    path = tmp_path / "bridge" / "torn.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"id":"partial","kind":"finding","text":"half')
    reloaded = Bridge("torn", tmp_path)
    assert [post["id"] for post in reloaded.posts()] == [first["id"]]
    second = reloaded.post("b", "conclusion", "second")
    final = Bridge("torn", tmp_path)
    assert [post["id"] for post in final.posts()] == [first["id"], second["id"]]


def test_invalid_utf8_and_extreme_json_lines_are_ignored(tmp_path):
    bridge = Bridge("hostile-lines", tmp_path)
    expected = bridge.post("a", "finding", "survives")
    path = tmp_path / "bridge" / "hostile-lines.jsonl"
    with path.open("ab") as stream:
        stream.write(b"\xff\xfe\n")
        stream.write(("[" * 1200 + "0" + "]" * 1200 + "\n").encode())
        stream.write(json.dumps(_remote("valid-after-hostile-line")).encode() + b"\n")
    replay = Bridge("hostile-lines", tmp_path)
    assert replay.get(expected["id"]) is not None
    assert replay.get("valid-after-hostile-line") is not None


def test_retention_limit_clamps_and_tombstones_prevent_resurrection(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "2")
    bridge = Bridge("bounded", tmp_path)
    first = bridge.post("a", "finding", "one")
    second = bridge.post("a", "finding", "two")
    third = bridge.post("a", "finding", "three")
    assert [post["id"] for post in bridge.posts()] == [second["id"], third["id"]]
    assert bridge.merge_remote([first], "old-peer")["skipped"] == 1
    assert Bridge("bounded", tmp_path).get(first["id"]) is None


def test_safe_run_id_cannot_escape_bridge_directory():
    for value in ("../../etc/passwd", "/tmp/evil", "..", "a/../../b", "\\\\server\\share"):
        safe = safe_run_id(value)
        assert safe and "/" not in safe and "\\\\" not in safe and not safe.startswith(".")


def test_codex_failure_does_not_sink_post(tmp_path):
    class BrokenCodex:
        def emit(self, *args, **kwargs):
            raise RuntimeError("audit sink unavailable")

    bridge = Bridge("codex-failure", tmp_path, codex=BrokenCodex())
    post = bridge.post("agent", "finding", "still persisted")
    assert bridge.get(post["id"])["text"] == "still persisted"
    assert Bridge("codex-failure", tmp_path).get(post["id"])["text"] == "still persisted"


def test_write_failure_degrades_to_in_memory_operation(tmp_path, monkeypatch):
    bridge = Bridge("disk-failure", tmp_path)
    original_open = Path.open

    def fail_bridge_append(path, mode="r", *args, **kwargs):
        if path.name == "disk-failure.jsonl" and "a" in mode:
            raise OSError("simulated disk full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_bridge_append)
    post = bridge.post("agent", "finding", "memory remains available")
    assert bridge.get(post["id"])["text"] == "memory remains available"


def test_post_input_types_and_reference_requirements_fail_closed():
    bridge = Bridge("strict")
    with pytest.raises(BridgeError):
        bridge.post("agent", "finding", None)
    with pytest.raises(BridgeError):
        bridge.post("agent", "unknown", "text")
    with pytest.raises(BridgeError):
        bridge.reply("agent", [], "reply without parent")


def _multiprocess_writer(data_dir: str, run_id: str, worker: str, count: int, limit: int) -> None:
    # Top-level target so multiprocessing spawn can import it on macOS as well.
    bridge = Bridge(run_id, data_dir, node_id=worker, limit=limit)
    for index in range(count):
        bridge.post(worker, "finding", f"{worker}:{index}")


def test_multiprocess_append_preserves_every_record(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_multiprocess_writer,
                           args=(str(tmp_path), "process-shared", f"P{i}", 45, 500))
               for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join()
            pytest.fail("multiprocess writer did not finish within 20 seconds")
        assert worker.exitcode == 0

    replay = Bridge("process-shared", tmp_path, limit=500)
    assert len(replay) == 180
    assert len({post["id"] for post in replay.posts()}) == 180
    assert {post["agent"] for post in replay.posts()} == {f"P{i}" for i in range(4)}


def test_multiprocess_compaction_is_serialized_and_keeps_bounded_tail(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_multiprocess_writer,
                           args=(str(tmp_path), "process-compact", f"C{i}", 55, 13))
               for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(25)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join()
            pytest.fail("multiprocess compaction writer did not finish within 25 seconds")
        assert worker.exitcode == 0

    replay = Bridge("process-compact", tmp_path, limit=13)
    assert len(replay) == 13
    assert len({post["id"] for post in replay.posts()}) == 13
    path = tmp_path / "bridge" / "process-compact.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert not list(path.parent.glob(".process-compact.jsonl.*.tmp"))


def _hold_bridge_sidecar_lock(data_dir: str, run_id: str, ready) -> None:
    import fcntl
    from pathlib import Path
    lock = Path(data_dir) / "bridge" / f"{run_id}.jsonl.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        ready.set()
        while True:
            import time
            time.sleep(1)


def test_sigkill_while_holding_bridge_lock_allows_restart(tmp_path):
    import multiprocessing
    import os
    import signal

    if os.name != "posix":
        pytest.skip("flock is a POSIX lock")
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    holder = ctx.Process(target=_hold_bridge_sidecar_lock,
                         args=(str(tmp_path), "killed-lock", ready))
    holder.start()
    assert ready.wait(10)
    os.kill(holder.pid, signal.SIGKILL)
    holder.join(10)
    assert holder.exitcode == -signal.SIGKILL

    bridge = Bridge("killed-lock", tmp_path)
    post = bridge.post("recovery", "conclusion", "lock recovered")
    assert Bridge("killed-lock", tmp_path).get(post["id"]) is not None


def test_six_processes_append_without_corrupting_jsonl(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_multiprocess_writer,
                           args=(str(tmp_path), "six-workers", f"W{i}", 35, 500))
               for i in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(25)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join()
            pytest.fail("six-process writer did not finish within 25 seconds")
        assert worker.exitcode == 0

    path = tmp_path / "bridge" / "six-workers.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 210
    assert len({row["id"] for row in rows}) == 210


def test_process_lock_sidecar_survives_atomic_journal_replacement(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_multiprocess_writer,
                           args=(str(tmp_path), "sidecar-stable", f"S{i}", 35, 7))
               for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(25)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join()
            pytest.fail("sidecar stability writer timed out")
        assert worker.exitcode == 0

    path = tmp_path / "bridge" / "sidecar-stable.jsonl"
    assert path.exists()
    assert path.with_suffix(path.suffix + ".lock").exists()
    assert len(Bridge("sidecar-stable", tmp_path, limit=7)) == 7
