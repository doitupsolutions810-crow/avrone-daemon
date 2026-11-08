"""Health / thermal governor: adapt squad parallelism to the node's condition.

An edge node abroad runs hot, shares its CPU, fills its disk and swaps. Before a
squad (or deep wave) spawns its workers, Trinity asks the governor how many may
run at once. The governor reads four sensors, each through an injectable reader
so tests (and odd platforms) can supply their own:

  load      1-minute load average divided by CPU count   (os.getloadavg)
  mem_pct   memory in use, percent                        (psutil or /proc/meminfo)
  disk_free disk free on the data dir, percent            (shutil.disk_usage)
  temp_c    hottest CPU/thermal zone, Celsius             (psutil or /sys/class/thermal)

Each reading maps to a pressure factor in [0, 1] (1 = healthy, 0 = critical):

  temp:  1 at <= THERMAL_MAX-15C, falling linearly to 0 at >= THERMAL_MAX
  load:  1 at <= 1.0 per CPU,     falling linearly to 0 at >= 2.0 per CPU
  mem:   1 at <= 80% used,        falling linearly to 0 at >= 95% used
  disk:  1 at >= 10% free,        falling linearly to 0 at <= 2% free

  target = floor + round((max_parallel - floor) * min(factors))

clamped to [floor, max_parallel], where floor = AVRONE_MIN_PARALLEL (default 1,
itself clamped to 1..max_parallel). A sensor that cannot be read (or a reader that
raises) is ignored; with nothing readable the target is max_parallel. The governor
is advisory: it never raises and never returns less than 1.

Env knobs: AVRONE_THERMAL_MAX_C (default 85), AVRONE_MIN_PARALLEL (default 1),
AVRONE_GOVERNOR (on/off, default on). ``is_hot()`` (temp over AVRONE_THERMAL_MAX_C) drives
edgepack thermal migration of idle agent homes (FleetManager.thermal_migrate, AVRONE_AGENT_MIGRATE).
"""
from __future__ import annotations

import glob
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

Reader = Callable[[], "float | None"]

DEFAULT_THERMAL_MAX_C = 85.0
THERMAL_SOFT_BAND_C = 15.0
LOAD_SOFT, LOAD_HARD = 1.0, 2.0
MEM_SOFT, MEM_HARD = 80.0, 95.0
DISK_SOFT, DISK_HARD = 10.0, 2.0


# ------------------------------------------------------------------ default readers
def _psutil() -> Any:
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:
        return None


CGROUP_ROOT = "/sys/fs/cgroup"
CMD_TIMEOUT_S = 2.0
_UNLIMITED = 1 << 60  # cgroup v1 reports "no limit" as a huge number (9223372036854771712)


def _run(args: list[str]) -> str | None:
    """Output of a short probe command (sysctl, vm_stat, pmset), or None. Bounded by CMD_TIMEOUT_S:
    the governor runs on the event loop's thread, so a hung probe must not stall a squad."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=CMD_TIMEOUT_S,
                           stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return p.stdout if p.returncode == 0 else None


def _read_text(path: str) -> str | None:
    # round 108: plain open() blocked the governor sample forever on a FIFO at a cgroup (root= param),
    # meminfo or thermal path; O_NONBLOCK + fstat-regular (sysfs and procfs files are regular)
    from .safeio import open_read
    try:
        with open_read(path, follow=True) as f:
            return f.read(65536).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _pos_int(text: Any) -> int | None:
    try:
        n = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def cgroup_cpus(root: str | None = None) -> float | None:
    """CPUs a container may use from its cgroup quota (v2 cpu.max "quota period", v1 cfs_quota/period).
    None when unlimited or unreadable. In a 2-CPU Docker container on a 16-core host the load average
    is the container's, but os.cpu_count() is the host's, so load per CPU read 8x too low."""
    root = CGROUP_ROOT if root is None else root
    raw = _read_text(os.path.join(root, "cpu.max"))
    if raw is not None:
        parts = raw.split()
        if len(parts) >= 1 and parts[0] != "max":
            q = _pos_int(parts[0])
            per = _pos_int(parts[1]) if len(parts) > 1 else 100000
            if q and per:
                return q / per
        return None
    q = _read_text(os.path.join(root, "cpu", "cpu.cfs_quota_us"))
    per = _read_text(os.path.join(root, "cpu", "cpu.cfs_period_us"))
    qn, pn = _pos_int(q), _pos_int(per)  # quota -1 means unlimited
    return qn / pn if qn and pn else None


def effective_cpus(root: str | None = None) -> float:
    """The CPUs this process can actually run on: the smallest of os.cpu_count(), the affinity mask
    (taskset, cpuset) and the cgroup quota. Never below 1."""
    n = float(os.cpu_count() or 1)
    aff = getattr(os, "sched_getaffinity", None)
    if aff is not None:
        try:
            n = min(n, float(len(aff(0)) or n))
        except OSError:
            pass
    q = cgroup_cpus(root)
    if q:
        n = min(n, q)
    return max(1.0, n)


def read_load() -> float | None:
    """1-minute load average per usable CPU (affinity and container quota respected)."""
    one = os.getloadavg()[0]
    return one / effective_cpus()


def parse_meminfo(text: str) -> float | None:
    """Percent used from /proc/meminfo text. Lines that don't parse are skipped: one odd line used to
    raise IndexError and lose the whole reading."""
    info: dict[str, float] = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if not sep:
            continue
        bits = v.split()
        if not bits:
            continue
        try:
            f = float(bits[0])
        except ValueError:
            continue
        if math.isfinite(f) and f >= 0:
            info[k.strip()] = f
    total = info.get("MemTotal") or 0
    avail = info.get("MemAvailable", info.get("MemFree"))
    if not total or avail is None:
        return None
    return max(0.0, min(100.0, 100.0 * (1 - avail / total)))


def cgroup_mem_pct(root: str | None = None) -> float | None:
    """Percent of the container's memory limit in use (v2 memory.max/current, v1 limit/usage), with
    reclaimable page cache (inactive_file) subtracted the way `docker stats` does. None when there is
    no limit. /proc/meminfo inside a container shows the host's RAM, so a container at 95% of its
    own limit used to read as a healthy host."""
    root = CGROUP_ROOT if root is None else root
    lim = _read_text(os.path.join(root, "memory.max"))
    if lim is not None:
        cur = _read_text(os.path.join(root, "memory.current"))
        stat = _read_text(os.path.join(root, "memory.stat")) or ""
        key = "inactive_file"
    else:
        lim = _read_text(os.path.join(root, "memory", "memory.limit_in_bytes"))
        cur = _read_text(os.path.join(root, "memory", "memory.usage_in_bytes"))
        stat = _read_text(os.path.join(root, "memory", "memory.stat")) or ""
        key = "total_inactive_file"
    limit, used = _pos_int(lim), _pos_int(cur)
    if not limit or limit >= _UNLIMITED or used is None:
        return None
    m = re.search(rf"^{key}\s+(\d+)\s*$", stat, re.M)
    if m:
        used = max(0, used - int(m.group(1)))
    return max(0.0, min(100.0, 100.0 * used / limit))


def parse_vm_stat(text: str, total_bytes: int) -> float | None:
    """macOS `vm_stat` output plus `sysctl hw.memsize` -> percent used. Available = free + inactive +
    speculative + purgeable pages (what the OS can hand out without swapping). The page size comes
    from the header: 16384 on Apple Silicon, 4096 on Intel."""
    if not total_bytes or total_bytes <= 0:
        return None
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    pages: dict[str, int] = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        mm = re.match(r"\s*(\d+)\.?\s*$", v) if sep else None
        if mm:
            pages[k.strip().strip('"').lower()] = int(mm.group(1))
    if "pages free" not in pages:
        return None
    avail = sum(pages.get(k, 0) for k in ("pages free", "pages inactive", "pages speculative",
                                          "pages purgeable")) * page
    return max(0.0, min(100.0, 100.0 * (1 - avail / total_bytes)))


# kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical. macOS keeps lots of RAM busy as
# cache, so the kernel's own pressure verdict is the better signal; warn/critical raise the reading
# to at least these values (inside the 80..95 ramp, and past it)
DARWIN_PRESSURE_FLOOR = {2: 90.0, 4: 96.0}


def darwin_mem_pct(run: Callable[[list[str]], "str | None"] | None = None) -> float | None:
    run = run or _run
    total = _pos_int(run(["sysctl", "-n", "hw.memsize"]))
    vm = run(["vm_stat"])
    pct = parse_vm_stat(vm, total) if (vm and total) else None
    level = _pos_int(run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]))
    floor = DARWIN_PRESSURE_FLOOR.get(level or 0)
    if floor is not None:
        pct = max(pct or 0.0, floor)
    return pct


def read_mem_pct(platform: str | None = None, run: Callable[[list[str]], "str | None"] | None = None,
                 meminfo_path: str = "/proc/meminfo", cgroup_root: str | None = None) -> float | None:
    """Memory in use, percent: the worse of the host reading and the container's own limit.
    psutil first; when it is missing or raises, /proc/meminfo on Linux and sysctl + vm_stat on macOS
    (before, a Mac without psutil had no memory reading at all, while gpt-oss:120b holds ~65 GB)."""
    platform = sys.platform if platform is None else platform
    host: float | None = None
    ps = _psutil()
    if ps is not None:
        try:
            v = float(ps.virtual_memory().percent)
            host = v if math.isfinite(v) else None
        except Exception:
            host = None
    if host is None:
        if platform == "darwin":
            host = darwin_mem_pct(run)
        else:
            text = _read_text(meminfo_path)
            host = parse_meminfo(text) if text is not None else None
    cg = cgroup_mem_pct(cgroup_root) if platform != "darwin" else None
    vals = [v for v in (host, cg) if v is not None]
    if not vals and platform.startswith("linux") and _read_text(meminfo_path) is None:
        raise FileNotFoundError(meminfo_path)  # recorded in health.errors so the gap is visible
    return max(vals) if vals else None


def parse_pmset_therm(text: str) -> float | None:
    """CPU speed limit percent from `pmset -g therm` (100 = not throttled). macOS exposes no
    temperature without root, so this is the Mac's thermal signal. CPU_Scheduler_Limit is used when
    CPU_Speed_Limit is absent; the smaller of the two wins. None when neither is reported (Apple
    Silicon often prints only 'No thermal warning level has been recorded')."""
    vals = []
    for key in ("CPU_Speed_Limit", "CPU_Scheduler_Limit"):
        m = re.search(rf"{key}\s*=\s*(\d+)", text or "")
        if m:
            vals.append(max(0.0, min(100.0, float(m.group(1)))))
    return min(vals) if vals else None


def read_thermal_limit(platform: str | None = None,
                       run: Callable[[list[str]], "str | None"] | None = None) -> float | None:
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return None
    out = (run or _run)(["pmset", "-g", "therm"])
    return parse_pmset_therm(out) if out else None


def make_disk_reader(path: str = ".") -> Reader:
    def read_disk_free_pct() -> float | None:
        p = path
        while p and not os.path.exists(p):  # the data dir may not exist yet
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        try:
            u = shutil.disk_usage(p or ".")
        except OSError:  # vanished mount / unreadable path: no reading rather than an exception
            return None
        return 100.0 * u.free / u.total if u.total else None
    return read_disk_free_pct


def read_temp_c(thermal_glob: str = "/sys/class/thermal/thermal_zone*/temp") -> float | None:
    """Hottest temperature sensor, Celsius; None when the platform exposes none."""
    temps: list[float] = []
    ps = _psutil()
    if ps is not None and hasattr(ps, "sensors_temperatures"):
        try:
            for entries in (ps.sensors_temperatures() or {}).values():
                temps += [float(e.current) for e in entries if getattr(e, "current", None) is not None]
        except Exception:
            pass
    if not temps:
        for p in glob.glob(thermal_glob):
            try:
                text = _read_text(p)  # round 108: was open(p).read(): unbounded, blocks on a FIFO
                if text is None:
                    continue
                raw = float(text.strip()[:64])
            except (OSError, ValueError):
                continue
            temps.append(raw / 1000.0 if abs(raw) > 1000 else raw)  # sysfs reports millidegrees
    temps = [t for t in temps if -40 < t < 150]  # drop disconnected-sensor junk
    return max(temps) if temps else None


# ------------------------------------------------------------------ model
@dataclass
class NodeHealth:
    load: float | None = None
    mem_pct: float | None = None
    disk_free_pct: float | None = None
    temp_c: float | None = None
    thermal_limit_pct: float | None = None  # macOS CPU speed limit (pmset -g therm), 100 = full speed
    errors: dict[str, str] = field(default_factory=dict)
    ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("load", "mem_pct", "disk_free_pct", "temp_c", "thermal_limit_pct"):
            if d[k] is not None:
                # +-inf drives the throttle as a real extreme, but strict JSON has no Infinity:
                # /api/telemetry answered 500 while a sensor read inf
                d[k] = round(d[k], 2) if math.isfinite(d[k]) else None
        return d


def _ramp_down(x: float, soft: float, hard: float) -> float:
    """1 at/below soft, 0 at/above hard, linear between (works for hard < soft too)."""
    if hard == soft:
        return 1.0 if x < soft else 0.0
    f = (hard - x) / (hard - soft)
    return max(0.0, min(1.0, f))


def _finite(v: Any, default: float) -> float:
    """float(v) when it is a finite number, else default ("nan"/"inf" thresholds disabled the
    thermal guard: every comparison against NaN is False, so is_hot() could never fire)."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):  # 10**400 used to raise out of Governor()
        return default
    return f if math.isfinite(f) else default


def _env_float(name: str, default: float) -> float:
    return _finite(os.environ.get(name, "") or default, default)


def _env_int(name: str, default: int) -> int:
    return _as_int(os.environ.get(name, "") or default, default)


def _as_int(v: Any, default: int) -> int:
    """int(v) for ints, integral floats and numeric strings ("2", " 3 ", "2.0"); default for anything
    else (bool, NaN, inf, junk). int(float("inf")) used to raise OverflowError out of Governor()."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        f = float(str(v).strip()) if not isinstance(v, float) else v
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return int(f)


THERMAL_LIMIT_SOFT, THERMAL_LIMIT_HARD = 100.0, 50.0
DEFAULT_THERMAL_LIMIT_HOT = 80.0


class Governor:
    """Computes a target concurrency in [floor, max_parallel] from node health."""

    def __init__(self, max_parallel: int, min_parallel: int | None = None,
                 thermal_max_c: float | None = None, enabled: bool = True,
                 readers: dict[str, Reader] | None = None, data_dir: str = ".",
                 ttl_s: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.max_parallel = max(1, min(4096, _as_int(max_parallel or 1, 1)))
        mp = _env_int("AVRONE_MIN_PARALLEL", 1) if min_parallel is None else _as_int(min_parallel, 1)
        self.floor = max(1, min(mp, self.max_parallel))
        self.thermal_max_c = (_env_float("AVRONE_THERMAL_MAX_C", DEFAULT_THERMAL_MAX_C)
                              if thermal_max_c is None else _finite(thermal_max_c, DEFAULT_THERMAL_MAX_C))
        self.enabled = enabled
        self.readers: dict[str, Reader] = {
            "load": read_load, "mem_pct": read_mem_pct,
            "disk_free_pct": make_disk_reader(str(data_dir)), "temp_c": read_temp_c,
            "thermal_limit_pct": read_thermal_limit,
        }
        # CPU speed limit at or under this percent counts as hot (AVRONE_THERMAL_LIMIT_HOT_PCT, 1..100)
        self.thermal_limit_hot = max(1.0, min(100.0, _env_float("AVRONE_THERMAL_LIMIT_HOT_PCT",
                                                                DEFAULT_THERMAL_LIMIT_HOT)))
        self.readers.update(readers or {})
        self.ttl_s = _finite(ttl_s, 2.0)
        self.clock = clock
        self._cache: tuple[float, NodeHealth] | None = None
        self.last_target = self.max_parallel
        self.last_reason = "not consulted yet"
        self.throttle_count = 0

    @classmethod
    def from_settings(cls, settings: Any, **kw: Any) -> "Governor":
        """Build from a Settings-like object; missing attributes fall back to env/defaults."""
        enabled = getattr(settings, "governor", None)
        if enabled is None:
            enabled = os.environ.get("AVRONE_GOVERNOR", "on").lower() not in {"0", "off", "false", "no"}
        return cls(max_parallel=getattr(settings, "max_parallel", 1) or 1,
                   min_parallel=getattr(settings, "min_parallel", None),
                   thermal_max_c=getattr(settings, "thermal_max_c", None),
                   enabled=bool(enabled), data_dir=str(getattr(settings, "data_dir", ".") or "."), **kw)

    # -------------------------------------------------------------- sensing
    def read(self, fresh: bool = False) -> NodeHealth:
        """Current readings (cached for ttl_s). Never raises."""
        now = self.clock()
        # 0 <= age: a clock that steps backwards must not pin a stale reading forever
        if not fresh and self._cache and 0 <= now - self._cache[0] < self.ttl_s:
            return self._cache[1]
        h = NodeHealth(ts=time.time())
        for key, reader in self.readers.items():
            try:
                v = reader()
                if v is not None:
                    v = float(v)
                    if v != v:  # NaN (+-inf is kept: it ramps to fully throttled / healthy as a real extreme)
                        raise ValueError("NaN reading")
                setattr(h, key, v)
            except Exception as e:  # unreadable sensor: ignore it, remember why
                h.errors[key] = f"{type(e).__name__}: {e}"[:200]
        self._cache = (now, h)
        return h

    def factors(self, h: NodeHealth) -> dict[str, float]:
        f: dict[str, float] = {}
        if h.temp_c is not None:
            f["temp_c"] = _ramp_down(h.temp_c, self.thermal_max_c - THERMAL_SOFT_BAND_C, self.thermal_max_c)
        if h.load is not None:
            f["load"] = _ramp_down(h.load, LOAD_SOFT, LOAD_HARD)
        if h.mem_pct is not None:
            f["mem_pct"] = _ramp_down(h.mem_pct, MEM_SOFT, MEM_HARD)
        if h.disk_free_pct is not None:
            f["disk_free_pct"] = _ramp_down(h.disk_free_pct, DISK_SOFT, DISK_HARD)
        if h.thermal_limit_pct is not None:
            f["thermal_limit_pct"] = _ramp_down(h.thermal_limit_pct, THERMAL_LIMIT_SOFT, THERMAL_LIMIT_HARD)
        return f

    def target(self, want: int | None = None) -> int:
        """How many workers may run at once (1 <= result <= max_parallel; <= want if given)."""
        cap = self.max_parallel
        try:
            if not self.enabled:
                t, reason = cap, "governor off"
            else:
                fs = self.factors(self.read())
                if not fs:
                    t, reason = cap, "no readable sensors"
                else:
                    worst = min(fs, key=lambda k: fs[k])
                    t = self.floor + round((cap - self.floor) * fs[worst])
                    t = max(self.floor, min(cap, t))
                    reason = "healthy" if t >= cap else f"throttled by {worst} (factor {fs[worst]:.2f})"
        except Exception as e:  # advisory: never break a run over a sensor bug
            t, reason = cap, f"governor error: {type(e).__name__}"
        w = None if want is None else max(1, _as_int(want, cap))  # junk want (NaN, "x") used to raise
        if w is not None:
            t = min(t, w)
        t = max(1, t)
        if t < min(cap, w or cap):
            self.throttle_count += 1
        self.last_target, self.last_reason = t, reason
        return t

    def is_hot(self) -> bool:
        """True when the hottest sensor is over AVRONE_THERMAL_MAX_C (edgepack thermal-migration
        hook). Reads sensors even when throttling is off (AVRONE_GOVERNOR only gates parallelism;
        migration has its own switch, AVRONE_AGENT_MIGRATE). Never raises; unreadable = not hot."""
        try:
            h = self.read()
            t, lim = h.temp_c, h.thermal_limit_pct
            # macOS has no readable temperature: a CPU speed limit at/under the hot mark counts instead
            return (t is not None and t > self.thermal_max_c) or \
                (lim is not None and lim <= self.thermal_limit_hot)
        except Exception:  # pragma: no cover - read() already swallows reader errors
            return False

    def state(self) -> dict[str, Any]:
        """Governor + node health snapshot for telemetry. Never raises."""
        try:
            h = self.read()
            fs = self.factors(h)
            health = h.as_dict()
        except Exception as e:  # pragma: no cover - read() already swallows reader errors
            fs, health = {}, {"errors": {"governor": str(e)}}
        return {"enabled": self.enabled, "max_parallel": self.max_parallel, "min_parallel": self.floor,
                "thermal_max_c": self.thermal_max_c, "thermal_limit_hot_pct": self.thermal_limit_hot,
                "factors": {k: round(v, 3) for k, v in fs.items()},
                "last_target": self.last_target, "last_reason": self.last_reason,
                "throttle_count": self.throttle_count, "health": health}
