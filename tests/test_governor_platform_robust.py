"""Round 39: governor on macOS (no /proc, no temperatures), inside containers (cgroup limits), and
against junk inputs."""
import math
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from avrone import governor as gv

VM_STAT_M4 = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               81920.
Pages active:                           2500000.
Pages inactive:                          409600.
Pages speculative:                        40960.
Pages throttled:                              0.
Pages wired down:                        900000.
Pages purgeable:                          16384.
"Translation faults":                 123456789.
Pages copy-on-write:                    1234567.
Pages zero filled:                    987654321.
Pages reactivated:                        12345.
Pages purged:                              6789.
File-backed pages:                       300000.
Anonymous pages:                        2600000.
Pages stored in compressor:              500000.
Pages occupied by compressor:            200000.
Decompressions:                          100000.
Compressions:                            200000.
Pageins:                                  30000.
Pageouts:                                    10.
Swapins:                                      0.
Swapouts:                                     0.
"""
GB = 1024 ** 3
M4_TOTAL = 128 * GB


def fake_run(table):
    calls = []

    def run(args):
        calls.append(tuple(args))
        v = table.get(tuple(args))
        if isinstance(v, Exception):
            raise v
        return v
    run.calls = calls
    return run


@pytest.fixture(autouse=True)
def no_psutil(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)


# ================================================================ macOS memory
def test_parse_vm_stat_apple_silicon_page_size():
    pct = gv.parse_vm_stat(VM_STAT_M4, M4_TOTAL)
    avail = (81920 + 409600 + 40960 + 16384) * 16384
    assert pct == pytest.approx(100 * (1 - avail / M4_TOTAL))


def test_parse_vm_stat_intel_page_size():
    text = VM_STAT_M4.replace("16384 bytes", "4096 bytes")
    avail = (81920 + 409600 + 40960 + 16384) * 4096
    assert gv.parse_vm_stat(text, 16 * GB) == pytest.approx(100 * (1 - avail / (16 * GB)))


def test_parse_vm_stat_no_header_defaults_4k():
    text = "\n".join(VM_STAT_M4.splitlines()[1:])
    assert gv.parse_vm_stat(text, 16 * GB) == pytest.approx(
        100 * (1 - (81920 + 409600 + 40960 + 16384) * 4096 / (16 * GB)))


@pytest.mark.parametrize("text,total", [("", M4_TOTAL), ("garbage\n", M4_TOTAL), (VM_STAT_M4, 0),
                                        (VM_STAT_M4, -5), ("Pages active: 5.\n", M4_TOTAL)])
def test_parse_vm_stat_unusable(text, total):
    assert gv.parse_vm_stat(text, total) is None


def test_parse_vm_stat_clamped():
    assert gv.parse_vm_stat(VM_STAT_M4, 1024) == 0.0  # more free than total -> 0, never negative
    huge = VM_STAT_M4.replace("81920.", "0.").replace("409600.", "0.").replace("40960.", "0.")
    huge = huge.replace("16384.\n", "0.\n")
    assert gv.parse_vm_stat(huge, M4_TOTAL) == 100.0


def test_darwin_mem_without_psutil():
    run = fake_run({("sysctl", "-n", "hw.memsize"): f"{M4_TOTAL}\n", ("vm_stat",): VM_STAT_M4,
                    ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "1\n"})
    v = gv.read_mem_pct(platform="darwin", run=run)
    assert v == pytest.approx(gv.parse_vm_stat(VM_STAT_M4, M4_TOTAL))


@pytest.mark.parametrize("level,floor", [("2", 90.0), ("4", 96.0)])
def test_darwin_pressure_level_raises_reading(level, floor):
    run = fake_run({("sysctl", "-n", "hw.memsize"): f"{M4_TOTAL}", ("vm_stat",): VM_STAT_M4,
                    ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): level})
    base = gv.parse_vm_stat(VM_STAT_M4, M4_TOTAL)
    assert gv.read_mem_pct(platform="darwin", run=run) == max(base, floor)
    light = fake_run({("sysctl", "-n", "hw.memsize"): f"{16 * GB}", ("vm_stat",): VM_STAT_M4,
                      ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): level})
    assert gv.read_mem_pct(platform="darwin", run=light) == floor  # cache-heavy but the kernel says pressure


def test_darwin_pressure_only():
    run = fake_run({("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "4"})
    assert gv.read_mem_pct(platform="darwin", run=run) == 96.0


@pytest.mark.parametrize("level", ["", "junk", "0", "3", "-1", None])
def test_darwin_odd_pressure_ignored(level):
    run = fake_run({("sysctl", "-n", "hw.memsize"): f"{M4_TOTAL}", ("vm_stat",): VM_STAT_M4,
                    ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): level})
    assert gv.read_mem_pct(platform="darwin", run=run) == pytest.approx(gv.parse_vm_stat(VM_STAT_M4, M4_TOTAL))


def test_darwin_all_probes_fail_is_none():
    assert gv.read_mem_pct(platform="darwin", run=fake_run({})) is None


def test_darwin_never_reads_proc(tmp_path):
    called = []
    run = fake_run({})
    gv.read_mem_pct(platform="darwin", run=run, meminfo_path=str(tmp_path / "nope"))
    assert ("vm_stat",) in run.calls


def test_mac_m4_squad_throttles_under_pressure():
    run = fake_run({("sysctl", "-n", "hw.memsize"): f"{M4_TOTAL}", ("vm_stat",): VM_STAT_M4,
                    ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "4"})
    g = gv.Governor(8, readers={"load": lambda: 0.1, "disk_free_pct": lambda: 50, "temp_c": lambda: None,
                                "thermal_limit_pct": lambda: None,
                                "mem_pct": lambda: gv.read_mem_pct(platform="darwin", run=run)})
    assert g.target() == 1 and "mem_pct" in g.last_reason


# ================================================================ macOS thermal
PMSET_INTEL = """Note: No thermal warning level has been recorded
Note: No performance warning level has been recorded
2026-11-03 12:00:00 -0500 CPU Power notify
\tCPU_Scheduler_Limit \t= 100
\tCPU_Available_CPUs \t= 8
\tCPU_Speed_Limit \t= 63
"""


def test_pmset_speed_limit():
    assert gv.parse_pmset_therm(PMSET_INTEL) == 63.0


def test_pmset_scheduler_limit_smaller_wins():
    assert gv.parse_pmset_therm(PMSET_INTEL.replace("Scheduler_Limit \t= 100", "Scheduler_Limit \t= 40")) == 40.0


@pytest.mark.parametrize("text", ["Note: No thermal warning level has been recorded\n", "", None, "CPU_Speed_Limit = x"])
def test_pmset_no_limit_is_none(text):
    assert gv.parse_pmset_therm(text) is None


def test_pmset_clamped():
    assert gv.parse_pmset_therm("CPU_Speed_Limit = 250") == 100.0


def test_read_thermal_limit_only_on_darwin():
    run = fake_run({("pmset", "-g", "therm"): PMSET_INTEL})
    assert gv.read_thermal_limit(platform="linux", run=run) is None and run.calls == []
    assert gv.read_thermal_limit(platform="darwin", run=run) == 63.0


def test_read_thermal_limit_probe_failure():
    assert gv.read_thermal_limit(platform="darwin", run=fake_run({})) is None


@pytest.mark.parametrize("lim,factor", [(100, 1.0), (75, 0.5), (50, 0.0), (10, 0.0)])
def test_thermal_limit_factor(lim, factor):
    g = gv.Governor(4, readers={})
    assert g.factors(gv.NodeHealth(thermal_limit_pct=lim))["thermal_limit_pct"] == pytest.approx(factor)


def test_thermal_limit_throttles_target():
    g = gv.Governor(9, readers={"load": lambda: None, "mem_pct": lambda: None, "disk_free_pct": lambda: None,
                                "temp_c": lambda: None, "thermal_limit_pct": lambda: 75})
    assert g.target() == 5 and "thermal_limit_pct" in g.last_reason


@pytest.mark.parametrize("lim,hot", [(100, False), (81, False), (80, True), (50, True), (None, False)])
def test_is_hot_on_mac_uses_speed_limit(lim, hot):
    g = gv.Governor(4, readers={"temp_c": lambda: None, "thermal_limit_pct": lambda: lim})
    assert g.is_hot() is hot


def test_is_hot_threshold_env(monkeypatch):
    monkeypatch.setenv("AVRONE_THERMAL_LIMIT_HOT_PCT", "95")
    g = gv.Governor(4, readers={"temp_c": lambda: None, "thermal_limit_pct": lambda: 90})
    assert g.is_hot() and g.state()["thermal_limit_hot_pct"] == 95.0


@pytest.mark.parametrize("v,want", [("nan", 80.0), ("0", 1.0), ("500", 99.0), ("x", 80.0)])  # round 89: 99 max (100 = always hot)
def test_thermal_limit_hot_env_clamped(monkeypatch, v, want):
    monkeypatch.setenv("AVRONE_THERMAL_LIMIT_HOT_PCT", v)
    assert gv.Governor(2, readers={}).thermal_limit_hot == want


def test_state_includes_thermal_limit():
    g = gv.Governor(4, readers={"thermal_limit_pct": lambda: 63.456})
    st = g.state()
    assert st["health"]["thermal_limit_pct"] == 63.46 and "thermal_limit_pct" in st["factors"]


# ================================================================ /proc/meminfo
def test_meminfo_odd_lines_skipped():
    text = ("MemTotal:       16000000 kB\nWeirdLine\nEmpty:\nBad: abc kB\nMemFree: 1000000 kB\n"
            "MemAvailable:    4000000 kB\nNeg: -5 kB\n")
    assert gv.parse_meminfo(text) == pytest.approx(75.0)


def test_meminfo_old_kernel_memfree_only():
    assert gv.parse_meminfo("MemTotal: 1000 kB\nMemFree: 250 kB\n") == pytest.approx(75.0)


@pytest.mark.parametrize("text", ["", "MemTotal: 0 kB\n", "MemFree: 5 kB\n", "MemTotal: nan kB\nMemFree: 1 kB\n"])
def test_meminfo_unusable(text):
    assert gv.parse_meminfo(text) is None


def test_meminfo_clamped():
    assert gv.parse_meminfo("MemTotal: 100 kB\nMemAvailable: 500 kB\n") == 0.0


def test_meminfo_file_used(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1000 kB\nMemAvailable: 100 kB\nOops\n")
    assert gv.read_mem_pct(platform="linux", meminfo_path=str(p), cgroup_root=str(tmp_path / "cg")) == pytest.approx(90.0)


def test_linux_missing_meminfo_raises_for_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        gv.read_mem_pct(platform="linux", meminfo_path=str(tmp_path / "x"), cgroup_root=str(tmp_path / "cg"))


def test_linux_missing_meminfo_but_cgroup(tmp_path):
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.max").write_text("1000\n")
    (cg / "memory.current").write_text("500\n")
    assert gv.read_mem_pct(platform="linux", meminfo_path=str(tmp_path / "x"), cgroup_root=str(cg)) == 50.0


def test_psutil_raising_falls_back(tmp_path, monkeypatch):
    def boom():
        raise PermissionError("sandboxed")
    monkeypatch.setattr(gv, "_psutil", lambda: SimpleNamespace(virtual_memory=boom))
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    assert gv.read_mem_pct(platform="linux", meminfo_path=str(p), cgroup_root=str(tmp_path)) == pytest.approx(60.0)


def test_psutil_nan_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: SimpleNamespace(virtual_memory=lambda: SimpleNamespace(percent=float("nan"))))
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    assert gv.read_mem_pct(platform="linux", meminfo_path=str(p), cgroup_root=str(tmp_path)) == pytest.approx(60.0)


def test_psutil_on_mac_skips_probes(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: SimpleNamespace(virtual_memory=lambda: SimpleNamespace(percent=33.0)))
    run = fake_run({})
    assert gv.read_mem_pct(platform="darwin", run=run) == 33.0 and run.calls == []


# ================================================================ cgroups
def cg2(tmp_path, maxv, cur, stat=""):
    d = tmp_path / "cg2"
    d.mkdir(exist_ok=True)
    (d / "memory.max").write_text(maxv)
    (d / "memory.current").write_text(cur)
    if stat:
        (d / "memory.stat").write_text(stat)
    return str(d)


def test_cgroup_v2_percent(tmp_path):
    assert gv.cgroup_mem_pct(cg2(tmp_path, "2000\n", "1500\n")) == 75.0


def test_cgroup_v2_subtracts_inactive_file(tmp_path):
    root = cg2(tmp_path, "2000", "1500", "anon 900\ninactive_file 500\nactive_file 100\n")
    assert gv.cgroup_mem_pct(root) == 50.0


def test_cgroup_v2_inactive_file_prefix_not_confused(tmp_path):
    root = cg2(tmp_path, "2000", "1500", "total_inactive_file 500\n")
    assert gv.cgroup_mem_pct(root) == 75.0


@pytest.mark.parametrize("maxv", ["max\n", "", "junk", "0", "-1"])
def test_cgroup_v2_unlimited_or_junk(tmp_path, maxv):
    assert gv.cgroup_mem_pct(cg2(tmp_path, maxv, "1500")) is None


def test_cgroup_v1(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    (d / "memory.limit_in_bytes").write_text("4000\n")
    (d / "memory.usage_in_bytes").write_text("3000\n")
    (d / "memory.stat").write_text("cache 10\ntotal_inactive_file 1000\n")
    assert gv.cgroup_mem_pct(str(tmp_path)) == 50.0


def test_cgroup_v1_unlimited(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    (d / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    (d / "memory.usage_in_bytes").write_text("3000\n")
    assert gv.cgroup_mem_pct(str(tmp_path)) is None


def test_cgroup_none_when_absent(tmp_path):
    assert gv.cgroup_mem_pct(str(tmp_path / "nothing")) is None


def test_container_limit_beats_healthy_host(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 64000000 kB\nMemAvailable: 60000000 kB\n")  # host ~6% used
    root = cg2(tmp_path, "1000", "960")                                     # container 96%
    assert gv.read_mem_pct(platform="linux", meminfo_path=str(p), cgroup_root=root) == 96.0


def test_cgroup_usage_over_limit_clamped(tmp_path):
    assert gv.cgroup_mem_pct(cg2(tmp_path, "1000", "5000")) == 100.0


@pytest.mark.parametrize("cpumax,want", [("200000 100000\n", 2.0), ("50000 100000", 0.5), ("max 100000", None),
                                         ("", None), ("abc 100000", None), ("150000", 1.5), ("0 100000", None)])
def test_cgroup_cpus_v2(tmp_path, cpumax, want):
    (tmp_path / "cpu.max").write_text(cpumax)
    assert gv.cgroup_cpus(str(tmp_path)) == want


@pytest.mark.parametrize("quota,period,want", [("-1", "100000", None), ("300000", "100000", 3.0),
                                               ("x", "100000", None), ("100000", "0", None)])
def test_cgroup_cpus_v1(tmp_path, quota, period, want):
    d = tmp_path / "cpu"
    d.mkdir()
    (d / "cpu.cfs_quota_us").write_text(quota)
    (d / "cpu.cfs_period_us").write_text(period)
    assert gv.cgroup_cpus(str(tmp_path)) == want


def test_effective_cpus_uses_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(gv.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(gv.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    (tmp_path / "cpu.max").write_text("200000 100000")
    assert gv.effective_cpus(str(tmp_path)) == 2.0


def test_effective_cpus_uses_affinity(tmp_path, monkeypatch):
    monkeypatch.setattr(gv.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(gv.os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)
    assert gv.effective_cpus(str(tmp_path / "none")) == 3.0


def test_effective_cpus_floor_one(tmp_path, monkeypatch):
    monkeypatch.setattr(gv.os, "cpu_count", lambda: None)
    (tmp_path / "cpu.max").write_text("10000 100000")
    assert gv.effective_cpus(str(tmp_path)) == 1.0


def test_effective_cpus_no_affinity_on_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(gv.os, "cpu_count", lambda: 14)
    monkeypatch.delattr(gv.os, "sched_getaffinity", raising=False)
    assert gv.effective_cpus(str(tmp_path / "none")) == 14.0


def test_effective_cpus_affinity_error(tmp_path, monkeypatch):
    def boom(pid):
        raise OSError("nope")
    monkeypatch.setattr(gv.os, "cpu_count", lambda: 6)
    monkeypatch.setattr(gv.os, "sched_getaffinity", boom, raising=False)
    assert gv.effective_cpus(str(tmp_path / "none")) == 6.0


def test_read_load_divides_by_container_cpus(tmp_path, monkeypatch):
    monkeypatch.setattr(gv, "CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(gv.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(gv.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    monkeypatch.setattr(gv.os, "getloadavg", lambda: (4.0, 0, 0))
    (tmp_path / "cpu.max").write_text("200000 100000")
    assert gv.read_load() == 2.0  # was 0.25 (4 / 16 host CPUs): a saturated container read as idle


# ================================================================ probes are bounded
def test_run_timeout_returns_none(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    monkeypatch.setattr(gv.subprocess, "run", slow)
    assert gv._run(["vm_stat"]) is None


def test_run_missing_binary():
    assert gv._run(["definitely-not-a-binary-avrone"]) is None


def test_run_nonzero_exit():
    assert gv._run([sys.executable, "-c", "import sys; print('x'); sys.exit(3)"]) is None


def test_run_ok():
    assert gv._run([sys.executable, "-c", "print(42)"]).strip() == "42"


def test_run_real_timeout(monkeypatch):
    import time
    monkeypatch.setattr(gv, "CMD_TIMEOUT_S", 0.3)
    t0 = time.monotonic()
    assert gv._run([sys.executable, "-c", "import time; time.sleep(5)"]) is None
    assert time.monotonic() - t0 < 3


# ================================================================ sysfs temps
def test_negative_millidegree_dropped(tmp_path):
    z = tmp_path / "thermal_zone0"
    z.mkdir()
    (z / "temp").write_text("-274000\n")  # a powered-off wifi sensor
    z1 = tmp_path / "thermal_zone1"
    z1.mkdir()
    (z1 / "temp").write_text("52000\n")
    assert gv.read_temp_c(str(tmp_path / "thermal_zone*" / "temp")) == 52.0


# ================================================================ junk inputs never raise
@pytest.mark.parametrize("want", [float("nan"), float("inf"), "x", None, True, [], "3", 2.7, 0, -4])
def test_target_junk_want_never_raises(want):
    g = gv.Governor(4, readers={k: (lambda: None) for k in ("load", "mem_pct", "disk_free_pct", "temp_c",
                                                              "thermal_limit_pct")})
    t = g.target(want)
    assert 1 <= t <= 4


def test_target_string_want_honoured():
    g = gv.Governor(8, readers={k: (lambda: None) for k in ("load", "mem_pct", "disk_free_pct", "temp_c",
                                                              "thermal_limit_pct")})
    assert g.target("3") == 3 and g.target(2.7) == 2


@pytest.mark.parametrize("mp,want", [(float("inf"), 1), ("6", 6), ("x", 1), (None, 1), (0, 1), (10 ** 9, 4096),
                                     (True, 1), ("2.0", 2)])
def test_max_parallel_coercion(mp, want):
    assert gv.Governor(mp, readers={}).max_parallel == want


@pytest.mark.parametrize("mn,want", [("x", 1), (float("nan"), 1), ("3", 3), (99, 8), (-2, 1)])
def test_min_parallel_coercion(mn, want):
    assert gv.Governor(8, min_parallel=mn, readers={}).floor == want


@pytest.mark.parametrize("v,want", [("2.0", 2), (" 3 ", 3), ("x", 1), ("inf", 1)])
def test_min_parallel_env(monkeypatch, v, want):
    monkeypatch.setenv("AVRONE_MIN_PARALLEL", v)
    assert gv.Governor(8, readers={}).floor == want


def test_ttl_nan_uses_default():
    n = [0]

    def r():
        n[0] += 1
        return 0.1
    g = gv.Governor(4, readers={"load": r}, ttl_s=float("nan"), clock=lambda: 100.0)
    g.read()
    g.read()
    assert g.ttl_s == 2.0 and n[0] == 1


def test_from_settings_with_mac_reader(monkeypatch):
    s = SimpleNamespace(max_parallel="4", min_parallel=None, thermal_max_c=None, governor=True, data_dir=".")
    g = gv.Governor.from_settings(s, readers={"thermal_limit_pct": lambda: 60, "load": lambda: None,
                                              "mem_pct": lambda: None, "disk_free_pct": lambda: None,
                                              "temp_c": lambda: None})
    assert g.max_parallel == 4 and g.target() < 4 and g.is_hot()


def test_default_readers_include_thermal_limit():
    assert gv.Governor(2).readers["thermal_limit_pct"] is gv.read_thermal_limit
