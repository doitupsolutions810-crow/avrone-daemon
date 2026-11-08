"""Round 108: governor cgroup/meminfo/thermal reads never block on a FIFO.

v0.100: _read_text used plain open() and read_temp_c used open(p).read() (unbounded). A FIFO at a
configured cgroup root (AVRONE_CGROUP_ROOT style), a meminfo path or a thermal glob match blocked
the governor sample forever. Now O_NONBLOCK + fstat-regular; sysfs/procfs files still read.
"""
from __future__ import annotations

import os
import threading

from avrone import governor


def _bounded(fn, limit=4.0):
    box = {}

    def go():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(limit)
    return not t.is_alive(), box


def _unblock(p):
    try:
        os.close(os.open(p, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


def test_cgroup_cpu_max_fifo(tmp_path):
    p = tmp_path / "cpu.max"
    os.mkfifo(p)
    try:
        done, box = _bounded(lambda: governor.cgroup_cpus(str(tmp_path)))
    finally:
        _unblock(p)
    assert done, "cgroup_cpus blocked forever on a FIFO cpu.max"
    assert box.get("v") is None


def test_cgroup_mem_fifo(tmp_path):
    (tmp_path / "memory.max").write_text("1000000\n")
    p = tmp_path / "memory.current"
    os.mkfifo(p)
    try:
        done, box = _bounded(lambda: governor.cgroup_mem_pct(str(tmp_path)))
    finally:
        _unblock(p)
    assert done, "cgroup_mem_pct blocked forever on a FIFO memory.current"


def test_thermal_fifo_skipped(tmp_path):
    (tmp_path / "zone0").mkdir()
    (tmp_path / "zone1").mkdir()
    os.mkfifo(tmp_path / "zone0" / "temp")
    (tmp_path / "zone1" / "temp").write_text("45000\n")
    try:
        done, box = _bounded(lambda: governor.read_temp_c(str(tmp_path / "zone*" / "temp")))
    finally:
        _unblock(tmp_path / "zone0" / "temp")
    assert done, "read_temp_c blocked forever on a FIFO thermal file"
    # psutil sensors may answer first on some hosts; when sysfs is used the regular zone counts
    assert box.get("v") is None or -40 < box["v"] < 150


def test_thermal_huge_file_bounded(tmp_path):
    (tmp_path / "z").mkdir()
    with open(tmp_path / "z" / "temp", "wb") as f:
        f.write(b"5" * (8 * 1024 * 1024))
    done, box = _bounded(lambda: governor.read_temp_c(str(tmp_path / "*" / "temp")))
    assert done and "e" not in box


def test_read_text_regular_and_symlink(tmp_path):
    real = tmp_path / "cpu.max.real"
    real.write_text("200000 100000\n")
    (tmp_path / "cpu.max").symlink_to(real)  # sysfs paths go through symlinked dirs/files
    assert governor.cgroup_cpus(str(tmp_path)) == 2.0


def test_read_text_proc_still_works():
    if not os.path.exists("/proc/meminfo"):
        return
    text = governor._read_text("/proc/meminfo")
    assert text and "MemTotal" in text
