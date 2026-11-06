"""Round 37: checkpoint/resume against crashes, power loss, odd files and odd values."""
import json
import os
import threading
import time

import pytest

from avrone import checkpoint as cp
from avrone.checkpoint import Checkpoint


def body(path):
    return json.loads(path.read_text())


# ================================================================ durability (fsync)
def test_save_fsyncs_file_and_directory(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    Checkpoint(tmp_path / "c.json").put("a", 1)
    assert len(calls) >= 2  # the temp file before the rename, then the directory entry


def test_fsync_error_counts_as_write_error_and_keeps_old_file(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    ck = Checkpoint(p)
    ck.put("a", 1)
    before = p.read_text()

    def boom(fd):
        raise OSError(5, "EIO")
    monkeypatch.setattr(os, "fsync", boom)
    assert ck.put("b", 2) is False
    assert p.read_text() == before and ck._store.write_errors == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_dir_fsync_failure_is_not_fatal(tmp_path, monkeypatch):
    real_open = os.open

    def no_dir_open(path, flags, *a):
        if os.path.isdir(path):
            raise PermissionError("no")
        return real_open(path, flags, *a)
    monkeypatch.setattr(os, "open", no_dir_open)
    ck = Checkpoint(tmp_path / "c.json")
    assert ck.put("a", 1) is True and body(tmp_path / "c.json")["steps"]["a"] == 1


# ================================================================ unserialisable values
class Weird:
    def __str__(self):
        return "weird!"


def test_cycle_refused_and_later_puts_still_save(tmp_path):
    p = tmp_path / "c.json"
    ck = Checkpoint(p)
    loop = []
    loop.append(loop)
    assert ck.put("bad", loop) is False
    assert ck.put("good", "ok") is True
    data = body(p)["steps"]
    assert data == {"good": "ok"} and ck._store.write_errors == 1


def test_tuple_keys_refused_without_poisoning(tmp_path):
    ck = Checkpoint(tmp_path / "c.json")
    assert ck.put("bad", {(1, 2): "x"}) is False
    assert ck.get("bad") is None
    assert ck.put("w.0", "done") is True
    assert body(tmp_path / "c.json")["steps"]["w.0"] == "done"


def test_values_stored_as_json_round_trip(tmp_path):
    ck = Checkpoint(tmp_path / "c.json")
    ck.put("v", {"t": (1, 2), 3: "int key", "o": Weird()})
    assert ck.get("v") == {"t": [1, 2], "3": "int key", "o": "weird!"}
    assert Checkpoint(tmp_path / "c.json").get("v") == ck.get("v")  # fresh process sees the same


def test_deep_nesting_refused(tmp_path):
    v = []
    for _ in range(100000):
        v = [v]
    ck = Checkpoint(tmp_path / "c.json")
    assert ck.put("deep", v) is False
    assert ck.put("ok", 1) is True


def test_put_returns_true_on_success(tmp_path):
    assert Checkpoint(tmp_path / "c.json").put("a", {"x": [1, "two", None, True, 1.5]}) is True


def test_put_mutation_after_store_does_not_change_checkpoint(tmp_path):
    ck = Checkpoint(tmp_path / "c.json")
    v = {"list": [1]}
    ck.put("v", v)
    v["list"].append(2)
    assert ck.get("v") == {"list": [1]}


# ================================================================ unusable files set aside
@pytest.mark.parametrize("content,tag", [
    (b"{not json", "corrupt"), (b"[1, 2, 3]", "corrupt"), (b'{"version": 1, "steps": []}', "corrupt"),
    (b'{"steps": {}}', "corrupt"), (b'"just a string"', "corrupt"), (b"\xff\xfe\x00garbage", "corrupt"),
    (b'{"version": 2, "steps": {"a": 1}}', "v2"), (b'{"version": 99, "steps": {}}', "v99"),
])
def test_unusable_file_renamed_aside_not_overwritten(tmp_path, content, tag):
    p = tmp_path / "c.json"
    p.write_bytes(content)
    ck = Checkpoint(p)
    assert ck.keys() == []
    aside = list(tmp_path.glob(f"c.json.{tag}-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == content
    ck.put("a", 1)
    assert body(p)["steps"] == {"a": 1}
    assert aside[0].read_bytes() == content


def test_empty_file_from_crash_is_just_fresh(tmp_path):
    p = tmp_path / "c.json"
    p.write_bytes(b"")
    ck = Checkpoint(p)
    assert ck.keys() == [] and not list(tmp_path.glob("c.json.*-*"))
    ck.put("a", 1)
    assert body(p)["steps"] == {"a": 1}


def test_truncated_file_set_aside(tmp_path):
    p = tmp_path / "c.json"
    Checkpoint(p).put("w.0", "x" * 1000)
    raw = p.read_bytes()
    p.write_bytes(raw[: len(raw) // 2])
    Checkpoint(p)
    assert len(list(tmp_path.glob("c.json.corrupt-*"))) == 1


def test_oversize_file_not_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CHECKPOINT_MAX_BYTES", str(64 * 1024))
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": 1, "steps": {"big": "x" * 100000}}))
    ck = Checkpoint(p)
    assert ck.get("big") is None and len(list(tmp_path.glob("c.json.toobig-*"))) == 1


def test_two_set_asides_same_second_do_not_clobber(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    p = tmp_path / "c.json"
    p.write_bytes(b"{one")
    Checkpoint(p)
    p.write_bytes(b"{two")
    Checkpoint(p)
    asides = sorted(x.read_bytes() for x in tmp_path.glob("c.json.corrupt-*"))
    assert asides == [b"{one", b"{two"]


def test_directory_at_path_left_alone(tmp_path):
    p = tmp_path / "c.json"
    p.mkdir()
    ck = Checkpoint(p)
    assert ck.keys() == [] and p.is_dir()
    assert ck.put("a", 1) is False  # cannot replace a directory; counted, not raised


def test_unreadable_file_not_renamed(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text('{"version": 1, "steps": {"a": 1}}')
    real = type(p).read_bytes

    def denied(self):
        if self == p:
            raise PermissionError("denied")
        return real(self)
    monkeypatch.setattr(type(p), "read_bytes", denied)
    Checkpoint(p)
    assert p.exists() and not list(tmp_path.glob("c.json.*-*"))


@pytest.mark.parametrize("v", ["max", "", "-5", "99999999999999"])
def test_max_bytes_env_clamped(monkeypatch, v):
    monkeypatch.setenv("AVRONE_CHECKPOINT_MAX_BYTES", v)
    assert 64 * 1024 <= cp.max_bytes() <= 1024 ** 3


def test_max_bytes_default(monkeypatch):
    monkeypatch.delenv("AVRONE_CHECKPOINT_MAX_BYTES", raising=False)
    assert cp.max_bytes() == 64 * 1024 * 1024


# ================================================================ stale temp files
def test_stale_tmp_files_removed_fresh_kept(tmp_path):
    p = tmp_path / "c.json"
    old = tmp_path / "c.json.abc123.tmp"
    new = tmp_path / "c.json.def456.tmp"
    other = tmp_path / "other.json.zzz.tmp"
    for f in (old, new, other):
        f.write_text("partial")
    t = time.time() - 2 * cp.STALE_TMP_SECONDS
    os.utime(old, (t, t))
    os.utime(other, (t, t))
    Checkpoint(p)
    assert not old.exists() and new.exists() and other.exists()


def test_tmp_glob_metacharacters_in_name(tmp_path):
    p = tmp_path / "c[1].json"
    stale = tmp_path / "c[1].json.x.tmp"
    stale.write_text("p")
    t = time.time() - 2 * cp.STALE_TMP_SECONDS
    os.utime(stale, (t, t))
    lookalike = tmp_path / "c1.json.x.tmp"
    lookalike.write_text("p")
    os.utime(lookalike, (t, t))
    Checkpoint(p).put("a", 1)
    assert not stale.exists() and lookalike.exists()


# ================================================================ resume matching
def test_resume_with_tuple_extra_matches(tmp_path):
    p = tmp_path / "c.json"
    ck = Checkpoint(p)
    ck.resume_point("t", "squad", roles=("a", "b"))
    ck.begin("t", "squad", "r1", roles=("a", "b"))
    ck.put("worker.0", "done")
    ck2 = Checkpoint(p)
    assert ck2.resume_point("t", "squad", roles=("a", "b")) is not None
    assert ck2.get("worker.0") == "done"


def test_resume_with_dict_int_keys_extra_matches(tmp_path):
    p = tmp_path / "c.json"
    ck = Checkpoint(p)
    ck.begin("t", "deep", "r1", weights={1: 0.5})
    assert Checkpoint(p).resume_point("t", "deep", weights={1: 0.5}) is not None


def test_resume_different_extra_still_clears(tmp_path):
    p = tmp_path / "c.json"
    ck = Checkpoint(p)
    ck.begin("t", "squad", "r1", n=4)
    ck.put("worker.0", "x")
    assert Checkpoint(p).resume_point("t", "squad", n=5) is None
    assert Checkpoint(p).keys() == []


def test_resume_unicode_and_surrogate_task(tmp_path):
    p = tmp_path / "c.json"
    task = "résumé ✓ \udcff end\r\n"
    ck = Checkpoint(p)
    ck.begin(task, "squad", "r1", n=2)
    assert Checkpoint(p).resume_point(task, "squad", n=2) is not None


def test_resume_float_vs_int_extra(tmp_path):
    p = tmp_path / "c.json"
    Checkpoint(p).begin("t", "squad", "r1", n=4)
    assert Checkpoint(p).resume_point("t", "squad", n=4.0) is not None


# ================================================================ case-insensitive keys (APFS)
def test_claim_case_variants_conflict(tmp_path):
    rel = cp.claim(tmp_path / "Job.json")
    try:
        with pytest.raises(cp.CheckpointConflict):
            cp.claim(tmp_path / "job.json")
        assert cp.is_busy(tmp_path / "JOB.json")
    finally:
        rel()
    assert not cp.is_busy(tmp_path / "job.json")


def test_file_lock_shared_across_case(tmp_path):
    assert cp._file_lock(tmp_path / "A.json") is cp._file_lock(tmp_path / "a.json")


def test_delete_managed_busy_case_variant(tmp_path):
    p = cp.managed_path(tmp_path, "MyRun")
    p.parent.mkdir(parents=True)
    p.write_text('{"version": 1, "steps": {}}')
    with cp.claimed(p):
        with pytest.raises(cp.CheckpointConflict):
            cp.delete_managed(tmp_path, "myrun")


def test_check_compatible_busy_case_variant(tmp_path):
    with cp.claimed(tmp_path / "Run.json"):
        with pytest.raises(cp.CheckpointConflict):
            cp.check_compatible(tmp_path / "run.json", "t", "squad")


# ================================================================ describe / list with odd files
def write_managed(d, key, data):
    p = cp.managed_path(d, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


@pytest.mark.parametrize("updated", ["yesterday", None, [1], {"a": 1}, True, "NaN"])
def test_list_survives_odd_updated(tmp_path, updated):
    write_managed(tmp_path, "a", {"version": 1, "updated": updated, "steps": {}})
    write_managed(tmp_path, "b", {"version": 1, "updated": 5.0, "steps": {}})
    rows = cp.list_managed(tmp_path)
    assert [r["id"] for r in rows] == ["b", "a"]
    assert rows[1]["updated"] is None


def test_list_nan_infinity_updated(tmp_path):
    p = cp.managed_path(tmp_path, "a")
    p.parent.mkdir(parents=True)
    p.write_text('{"version": 1, "updated": Infinity, "steps": {}}')
    write_managed(tmp_path, "b", {"version": 1, "updated": NaN_SAFE, "steps": {}})
    rows = cp.list_managed(tmp_path)
    assert {r["id"]: r["updated"] for r in rows} == {"a": None, "b": 3.0}


NaN_SAFE = 3.0


@pytest.mark.parametrize("history", ["abc", 7, {"x": 1}, None])
def test_describe_full_odd_history(tmp_path, history):
    p = write_managed(tmp_path, "a", {"version": 1, "steps": {"rounds": {"history": history},
                                                                 "meta": {"n": "four", "mode": 5}}})
    d = cp.describe(p, full=True)
    assert d["rounds_done"] == 0 and d["squad_size"] is None and d["mode"] is None


def test_describe_full_good_history(tmp_path):
    p = write_managed(tmp_path, "a", {"version": 1, "steps": {"rounds": {"history": [1, 2]},
                                                                 "meta": {"n": 4, "mode": "deep", "task": "t"}}})
    d = cp.describe(p, full=True)
    assert d["rounds_done"] == 2 and d["squad_size"] == 4 and d["mode"] == "deep"


def test_describe_bool_n_not_a_size(tmp_path):
    p = write_managed(tmp_path, "a", {"version": 1, "steps": {"meta": {"n": True}}})
    assert cp.describe(p, full=True)["squad_size"] is None


def test_list_skips_symlink_to_outside_json(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"steps": {"meta": {"task": "TOP SECRET"}}}))
    d = cp.managed_dir(tmp_path)
    d.mkdir()
    (d / "leak.json").symlink_to(secret)
    assert cp.list_managed(tmp_path) == []
    assert cp.describe(d / "leak.json") is None


def test_list_skips_directory_named_json(tmp_path):
    d = cp.managed_dir(tmp_path)
    (d / "x.json").mkdir(parents=True)
    write_managed(tmp_path, "ok", {"version": 1, "steps": {}})
    assert [r["id"] for r in cp.list_managed(tmp_path)] == ["ok"]


def test_list_skips_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CHECKPOINT_MAX_BYTES", str(64 * 1024))
    write_managed(tmp_path, "big", {"version": 1, "steps": {"x": "y" * 100000}})
    write_managed(tmp_path, "ok", {"version": 1, "steps": {}})
    assert [r["id"] for r in cp.list_managed(tmp_path)] == ["ok"]


def test_list_ties_sorted_stably_by_id(tmp_path):
    for k in ("b", "a", "c"):
        write_managed(tmp_path, k, {"version": 1, "updated": 1.0, "steps": {}})
    assert [r["id"] for r in cp.list_managed(tmp_path)] == ["c", "b", "a"]


def test_read_deeply_nested_file_is_none(tmp_path):
    p = cp.managed_path(tmp_path, "deep")
    p.parent.mkdir(parents=True)
    p.write_text('{"version": 1, "steps": {"x": ' + "[" * 200000 + "]" * 200000 + "}}")
    assert cp.describe(p) is None


def test_check_compatible_symlink_treated_unreadable(tmp_path):
    other = tmp_path / "o.json"
    other.write_text(json.dumps({"owner": "x", "steps": {"meta": {"task": "a", "mode": "squad"}}}))
    link = tmp_path / "l.json"
    link.symlink_to(other)
    assert cp.check_compatible(link, "b", "deep") is None


# ================================================================ concurrency still holds
def test_parallel_puts_all_survive_with_fsync(tmp_path):
    p = tmp_path / "c.json"
    ck = Checkpoint(p)

    def worker(i):
        for j in range(10):
            ck.scope(f"w{i}.").put(str(j), j)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(body(p)["steps"]) == 60


def test_failed_put_does_not_leave_tmp(tmp_path):
    ck = Checkpoint(tmp_path / "c.json")
    loop = {}
    loop["x"] = loop
    ck.put("bad", loop)
    assert not list(tmp_path.glob("*.tmp"))


def test_summary_counts_refused_value(tmp_path):
    ck = Checkpoint(tmp_path / "c.json")
    loop = []
    loop.append(loop)
    ck.put("bad", loop)
    assert ck.summary()["write_errors"] == 1


# ================================================================ real crash: SIGKILL mid-write
@pytest.mark.parametrize("delay", [0.15, 0.4])
def test_sigkill_mid_writes_leaves_loadable_file(tmp_path, delay):
    import signal
    import subprocess
    import sys
    p = tmp_path / "c.json"
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from avrone.checkpoint import Checkpoint\n"
            "ck = Checkpoint(%r)\n"
            "i = 0\n"
            "print('go', flush=True)\n"
            "while True:\n"
            "    ck.put('w.%%d' %% i, 'x' * 2000); i += 1\n") % (os.getcwd(), str(p))
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == b"go"
        time.sleep(delay)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
    ck = Checkpoint(p)
    n = len(ck.keys())
    assert n > 0 and not list(tmp_path.glob("c.json.corrupt-*"))
    # every stored step is complete and the run can carry on from it
    assert all(ck.get(k) == "x" * 2000 for k in ck.keys())
    assert ck.put("after", 1) is True
