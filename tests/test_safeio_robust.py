"""Round 58: safeio edge cases (umask window, temp-name leaks, fd leaks, cap allocation, odd paths)."""
from __future__ import annotations

import os
import threading
import tracemalloc
from pathlib import Path

import pytest

from avrone import safeio
from avrone.safeio import FileTooBig


# ---- umask: never a umask(0) window -------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs /proc")
def test_current_umask_reads_proc_without_touching_umask(monkeypatch):
    real = os.umask(0o027)
    try:
        def boom(_m):
            raise AssertionError("os.umask called")
        monkeypatch.setattr(safeio.os, "umask", boom)
        assert safeio.current_umask() == 0o027
        assert safeio._default_mode() == 0o640
    finally:
        monkeypatch.undo()
        os.umask(real)


def test_current_umask_fallback_window_is_restrictive(monkeypatch):
    monkeypatch.setattr(safeio, "_proc_umask", lambda: None)
    monkeypatch.setattr(safeio, "_UMASK", None)
    seen = []
    real = os.umask

    def spy(m):
        seen.append(m)
        return real(m)
    monkeypatch.setattr(safeio.os, "umask", spy)
    old = real(0o022)
    real(old)
    assert safeio.current_umask() == old
    assert seen and seen[0] == 0o077 and 0 not in seen
    seen.clear()
    assert safeio.current_umask() == old and seen == []  # cached, swapped once


def test_current_umask_fallback_threads_agree(monkeypatch):
    monkeypatch.setattr(safeio, "_proc_umask", lambda: None)
    monkeypatch.setattr(safeio, "_UMASK", None)
    want = os.umask(0o022)
    os.umask(want)
    out = []
    ts = [threading.Thread(target=lambda: out.append(safeio.current_umask())) for _ in range(32)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(out) == {want}
    assert os.umask(want) == want


def test_proc_umask_parses_or_none(monkeypatch, tmp_path):
    v = safeio._proc_umask()
    assert v is None or 0 <= v <= 0o777


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs /proc")
def test_atomic_write_new_file_mode_follows_live_umask(tmp_path):
    old = os.umask(0o077)
    try:
        safeio.atomic_write(tmp_path / "f", "x")
        assert (os.stat(tmp_path / "f").st_mode & 0o777) == 0o600
    finally:
        os.umask(old)


def test_tools_atomic_write_bytes_does_not_zero_umask(monkeypatch, tmp_path):
    from avrone import tools
    monkeypatch.setattr(safeio, "_proc_umask", lambda: 0o022)

    def boom(_m):
        raise AssertionError("os.umask called")
    monkeypatch.setattr(os, "umask", boom)
    tools.atomic_write_bytes(tmp_path / "n.txt", b"hi")
    monkeypatch.undo()
    assert (tmp_path / "n.txt").read_bytes() == b"hi"
    assert (os.stat(tmp_path / "n.txt").st_mode & 0o777) == 0o644


# ---- errors never name the random temp file -----------------------------------------------------

def test_atomic_write_onto_directory_names_target(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(IsADirectoryError) as ei:
        safeio.atomic_write(d, "x")
    assert ".tmp" not in str(ei.value) and ei.value.filename == str(d)
    assert sorted(os.listdir(tmp_path)) == ["d"]


def test_atomic_write_chmod_error_names_target(tmp_path, monkeypatch):
    def bad(path, mode):
        raise PermissionError(1, "Operation not permitted", path)
    monkeypatch.setattr(safeio.os, "chmod", bad)
    with pytest.raises(PermissionError) as ei:
        safeio.atomic_write(tmp_path / "f", "x")
    assert ei.value.filename == str(tmp_path / "f")
    assert os.listdir(tmp_path) == []


def test_tools_atomic_write_bytes_onto_directory_names_target(tmp_path):
    from avrone import tools
    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(IsADirectoryError) as ei:
        tools.atomic_write_bytes(d, b"x")
    assert ".tmp" not in str(ei.value)
    assert sorted(os.listdir(tmp_path)) == ["d"]


def test_with_target_tmp_in_filename2():
    e = OSError(21, "Is a directory", "/x/.f.abc.tmp", None, "/x/f")
    t = safeio.with_target(e, "/x/f", "/x/.f.abc.tmp")
    assert t is not e and t.filename == "/x/f" and t.errno == 21
    e2 = OSError(21, "Is a directory", "/elsewhere")
    assert safeio.with_target(e2, "/x/f", "/x/.f.abc.tmp") is e2
    assert safeio.with_target(ValueError("v"), "/x/f", "/t") .__class__ is ValueError


def test_with_target_odd_subclass_returned_as_is():
    class Weird(OSError):
        def __init__(self):
            super().__init__(5, "io", "/t")
    e = Weird()
    assert safeio.with_target(e, "/p", "/t") is e


# ---- fsync=True also syncs the directory --------------------------------------------------------

def test_atomic_write_fsync_syncs_dir(tmp_path, monkeypatch):
    calls = []
    real = os.fsync

    def spy(fd):
        calls.append(os.path.isdir(f"/proc/self/fd/{fd}") if os.path.exists("/proc/self/fd") else True)
        return real(fd)
    monkeypatch.setattr(safeio.os, "fsync", spy)
    safeio.atomic_write(tmp_path / "f", "x", fsync=True)
    assert len(calls) == 2 and calls[-1] is True
    calls.clear()
    safeio.atomic_write(tmp_path / "g", "x")
    assert calls == []


def test_fsync_dir_tolerates_missing_and_failing(tmp_path, monkeypatch):
    safeio.fsync_dir(tmp_path / "nope")

    def bad(_fd):
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(safeio.os, "fsync", bad)
    safeio.fsync_dir(tmp_path)
    safeio.atomic_write(tmp_path / "f", "x", fsync=False)


def test_fsync_dir_closes_fd(tmp_path):
    if not os.path.exists("/proc/self/fd"):
        pytest.skip("needs /proc")
    n = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        safeio.fsync_dir(tmp_path)
    assert len(os.listdir("/proc/self/fd")) == n


# ---- open_append: bad modes never leak an fd or create the file ---------------------------------

@pytest.mark.parametrize("mode", ["r", "w", "wb", "rb", "zz", "", "ab+x", "x", "a\0"])
def test_open_append_bad_mode(tmp_path, mode):
    fdn = len(os.listdir("/proc/self/fd")) if os.path.exists("/proc/self/fd") else None
    with pytest.raises(ValueError):
        safeio.open_append(tmp_path / "f", mode)
    assert not (tmp_path / "f").exists()
    if fdn is not None:
        assert len(os.listdir("/proc/self/fd")) == fdn


@pytest.mark.parametrize("mode", ["ab", "a", "a+b", "a+"])
def test_open_append_good_modes(tmp_path, mode):
    with safeio.open_append(tmp_path / "f", mode) as f:
        f.write(b"x" if "b" in mode else "x")
    assert (tmp_path / "f").read_bytes() == b"x"


def test_open_append_text_is_utf8(tmp_path):
    with safeio.open_append(tmp_path / "f", "a") as f:
        f.write("é✓")
    assert (tmp_path / "f").read_bytes() == "é✓".encode()


# ---- read_capped: no up-front allocation of the cap --------------------------------------------

def test_read_capped_huge_cap_small_file(tmp_path):
    p = tmp_path / "s"
    p.write_bytes(b"hi")
    tracemalloc.start()
    try:
        assert safeio.read_capped(p, 1 << 30) == b"hi"
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 << 20


@pytest.mark.parametrize("cap", [10 ** 19, 2 ** 63, 2 ** 64 + 7])
def test_read_capped_cap_past_ssize_t(tmp_path, cap):
    p = tmp_path / "s"
    p.write_bytes(b"hi")
    assert safeio.read_capped(p, cap) == b"hi"


def test_read_capped_negative_and_zero(tmp_path):
    e = tmp_path / "e"
    e.write_bytes(b"")
    s = tmp_path / "s"
    s.write_bytes(b"x")
    assert safeio.read_capped(e, -1) == b""
    assert safeio.read_capped(e, 0) == b""
    with pytest.raises(FileTooBig):
        safeio.read_capped(s, 0)
    with pytest.raises(FileTooBig):
        safeio.read_capped(s, -5)


@pytest.mark.parametrize("n", [0, 1, 65535, 65536, 65537, 300_001])
def test_read_capped_exact_boundaries(tmp_path, n):
    p = tmp_path / "f"
    data = os.urandom(n)
    p.write_bytes(data)
    assert safeio.read_capped(p, n) == data
    assert safeio.read_capped(p, n + 1) == data
    if n:
        with pytest.raises(FileTooBig):
            safeio.read_capped(p, n - 1)


def test_read_capped_file_growing_past_cap_while_read(tmp_path, monkeypatch):
    p = tmp_path / "g"
    p.write_bytes(b"a" * 10)
    real = os.fstat

    class St:
        def __init__(self, st):
            self.st_size = 3
            self.st_mode = st.st_mode
    monkeypatch.setattr(safeio.os, "fstat", lambda fd: St(real(fd)))
    with pytest.raises(FileTooBig):
        safeio.read_capped(p, 5)
    assert safeio.read_capped(p, 10) == b"a" * 10  # grew but still under the cap: read whole


def test_read_capped_closes_fd_on_toobig(tmp_path):
    if not os.path.exists("/proc/self/fd"):
        pytest.skip("needs /proc")
    p = tmp_path / "f"
    p.write_bytes(b"xxxx")
    n = len(os.listdir("/proc/self/fd"))
    for _ in range(30):
        with pytest.raises(FileTooBig):
            safeio.read_capped(p, 1)
    assert len(os.listdir("/proc/self/fd")) == n


# ---- read_regular -------------------------------------------------------------------------------

def test_read_regular_negative_cap(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"a\nb\n")
    assert safeio.read_regular(p, -3) == b""
    assert safeio.read_regular(p, 0) == b""
    assert safeio.read_regular(p, 2) == b""
    assert safeio.read_regular(p, 3) == b"b\n"
    assert safeio.read_regular(p) == b"a\nb\n"


# ---- impossible paths read as "missing" --------------------------------------------------------

@pytest.mark.parametrize("bad", ["a\0b", b"a\0b", "\0"])
def test_nul_path_is_missing(tmp_path, bad):
    assert safeio.read_regular(bad) is None
    assert safeio.read_capped(bad, 10) is None


def test_path_through_a_file_is_missing(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    assert safeio.read_regular(f / "x") is None
    assert safeio.read_capped(f / "x", 10) is None
    assert safeio.read_regular(str(f) + "/") is None


def test_other_value_errors_still_raise():
    with pytest.raises(TypeError):
        safeio.read_regular(12.5)


def test_symlink_and_fifo_still_none(tmp_path):
    t = tmp_path / "t"
    t.write_text("secret")
    (tmp_path / "l").symlink_to(t)
    assert safeio.read_capped(tmp_path / "l", 100) is None
    os.mkfifo(tmp_path / "q")
    assert safeio.read_capped(tmp_path / "q", 100) is None
    assert safeio.read_regular(tmp_path / "q") is None


# ---- flock_wait timeouts ------------------------------------------------------------------------

@pytest.mark.parametrize("t", [float("inf"), 1e308, float("nan"), -1.0, -float("inf")])
def test_flock_wait_odd_timeouts_free_lock(tmp_path, t):
    fd = os.open(tmp_path / "l", os.O_RDWR | os.O_CREAT)
    try:
        assert safeio.flock_wait(fd, t) is True
    finally:
        os.close(fd)


def test_flock_wait_nan_on_held_lock_returns_fast(tmp_path):
    import fcntl
    import time
    a = os.open(tmp_path / "l", os.O_RDWR | os.O_CREAT)
    b = os.open(tmp_path / "l", os.O_RDWR)
    try:
        fcntl.flock(a, fcntl.LOCK_EX)
        t0 = time.monotonic()
        assert safeio.flock_wait(b, float("nan")) is False
        assert safeio.flock_wait(b, -5) is False
        assert time.monotonic() - t0 < 1
    finally:
        os.close(a)
        os.close(b)


def test_flock_wait_inf_is_capped(monkeypatch, tmp_path):
    import fcntl
    seen = {}
    real_mono = safeio.__dict__.get("time")
    import time as _t
    start = _t.monotonic()
    clock = [start]
    monkeypatch.setattr(_t, "monotonic", lambda: clock[0])

    def fake_sleep(d):
        clock[0] += 1000.0
    monkeypatch.setattr(_t, "sleep", fake_sleep)
    a = os.open(tmp_path / "l", os.O_RDWR | os.O_CREAT)
    b = os.open(tmp_path / "l", os.O_RDWR)
    try:
        fcntl.flock(a, fcntl.LOCK_EX)
        assert safeio.flock_wait(b, float("inf")) is False
        assert clock[0] - start <= 3600 + 1000
    finally:
        os.close(a)
        os.close(b)
    del seen, real_mono


# ---- regressions of earlier rounds still hold ---------------------------------------------------

def test_atomic_write_long_name_and_symlink_target(tmp_path):
    name = "é" * 120  # 240 bytes
    safeio.atomic_write(tmp_path / name, "x")
    assert (tmp_path / name).read_text() == "x"
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "link").symlink_to(victim)
    safeio.atomic_write(tmp_path / "link", "new")
    assert victim.read_text() == "keep" and not (tmp_path / "link").is_symlink()


def test_atomic_write_bad_data_leaves_nothing(tmp_path):
    with pytest.raises(TypeError):
        safeio.atomic_write(tmp_path / "f", 123)  # type: ignore[arg-type]
    with pytest.raises(UnicodeEncodeError):
        safeio.atomic_write(tmp_path / "f", "\ud800")
    assert os.listdir(tmp_path) == []


def test_json_loads_deep_nesting_is_decode_error():
    import json
    with pytest.raises(json.JSONDecodeError):
        safeio.json_loads("[" * 200_000)


def test_treewalk_and_newest_entries_basic(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.log").write_text("1")
    (tmp_path / "b.log").write_text("2")
    rels = [r for r, _ in safeio.TreeWalk(tmp_path, 100)]
    assert rels == ["a", "a/x.log", "b.log"]
    best, total = safeio.newest_entries(tmp_path, ".log", -1)
    assert best == [] and total == 1
    assert Path(safeio.newest_files(tmp_path, ".log", 5)[0][0]).name == "b.log"
