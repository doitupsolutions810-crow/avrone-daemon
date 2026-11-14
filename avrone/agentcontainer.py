"""Persistent, per-agent containers ("agent homes") for the Docker runner.

    AVRONE_AGENT_CONTAINER   off | persistent   (default off)

With ``persistent`` and the Docker runner active, every agent that runs a command gets ONE
long-lived, named container

    avrone-agent-<node>-<agent_id>

that mounts ONLY that agent's own dir (``workspace/agents/<id>``) at ``/agent``. It is created
lazily on the agent's first command, reused for every later command and run (state in /agent and
the container's own filesystem survive between commands), and reaped (``docker rm -f``) once it
has been idle for AVRONE_AGENT_IDLE_SECS. It is hardened like a throwaway sandbox:

    --cap-drop ALL  --security-opt no-new-privileges  --pids-limit 512
    --memory <AVRONE_AGENT_MEM_MB>m (default 512)   --cpus <AVRONE_AGENT_CPUS> (default 1.0)
    --network none unless AVRONE_ALLOW_NETWORK

Every docker argv is built by a PURE function here (``persistent_run_argv`` / ``exec_argv`` /
``inspect_argv`` / ``rm_argv``) so it is unit-testable without Docker. ``AgentContainerPool``
drives them through an injected async ``exec`` callable and an injected clock.

Off (the default) changes nothing: the runner keeps its v0.16 behaviour (shared container, or a
throwaway container per command with AVRONE_AGENT_DOCKER_ISOLATE=1).
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import agentfs

DEFAULT_MEM_MB, MIN_MEM_MB, MAX_MEM_MB = 512, 64, 65536
DEFAULT_CPUS, MIN_CPUS, MAX_CPUS = 1.0, 0.1, 64.0
DEFAULT_IDLE_SECS, MIN_IDLE_SECS = 600.0, 5.0
PIDS_LIMIT = 512
NAME_PREFIX = "avrone-agent"
_NAME_BAD = re.compile(r"[^a-z0-9_.-]+")


def container_mode() -> str:
    """'persistent' or 'off' (anything unrecognised is off: fails closed to v0.16 behaviour)."""
    v = (os.environ.get("AVRONE_AGENT_CONTAINER") or "off").strip().lower()
    return "persistent" if v == "persistent" else "off"


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        v = default
    if v != v or v <= 0:  # NaN / non-positive
        v = default
    return max(lo, min(hi, v))


def _clamp_num(v: Any, default: Any, lo: float, hi: float, cast: Callable[[float], Any]) -> Any:
    """An explicit mem/cpu value: NaN, 0, negative, junk or huge used to reach docker verbatim
    ("--cpus nan", "--memory 0m" = unlimited, OverflowError on inf)."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if f != f or f <= 0:
        return default
    return cast(max(lo, min(hi, f)))


def mem_mb() -> int:
    return int(_num("AVRONE_AGENT_MEM_MB", DEFAULT_MEM_MB, MIN_MEM_MB, MAX_MEM_MB))


def cpus() -> float:
    return round(_num("AVRONE_AGENT_CPUS", DEFAULT_CPUS, MIN_CPUS, MAX_CPUS), 2)


def idle_secs() -> float:
    return _num("AVRONE_AGENT_IDLE_SECS", DEFAULT_IDLE_SECS, MIN_IDLE_SECS, 7 * 86400.0)


def local_node_id() -> str:
    raw = (os.environ.get("AVRONE_NODE_ID") or "").strip() or socket.gethostname()
    return _NAME_BAD.sub("-", raw.lower()).strip("-.")[:40] or "node"


def container_name(node: str, agent_id: str) -> str:
    """Docker-safe, deterministic name for one agent's home container."""
    n = _NAME_BAD.sub("-", str(node).lower()).strip("-.")[:40] or "node"
    return f"{NAME_PREFIX}-{n}-{agentfs.sanitize_agent_id(agent_id)}"


def hardening_args(allow_network: bool, mem: int | None = None, cpu: float | None = None) -> list[str]:
    """Flags every agent home container runs with (no capabilities, no privilege escalation,
    bounded memory/cpu/pids, no network unless allowed)."""
    args = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(PIDS_LIMIT),
            "--memory", f"{mem_mb() if mem is None else _clamp_num(mem, mem_mb(), MIN_MEM_MB, MAX_MEM_MB, int)}m",
            "--cpus", str(cpus() if cpu is None else _clamp_num(cpu, cpus(), MIN_CPUS, MAX_CPUS, lambda v: round(v, 2)))]
    if not allow_network:
        args += ["--network", "none"]
    return args


_IMAGE_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,511}\Z")


def check_image(image: Any) -> str:
    """The image reference, or ValueError. Round 60: the image is the first positional argument
    of `docker run`, so AVRONE_DOCKER_IMAGE="--privileged" (or "-v=/:/host") was parsed as an
    option: the home ran privileged with image "sleep". A reference starts with a letter or digit
    and holds only name/tag/digest characters (no spaces, controls or leading '-')."""
    s = image if isinstance(image, str) else ""
    if not _IMAGE_OK.match(s):
        raise ValueError(f"invalid docker image reference {str(image)[:80]!r}: it must start with a letter or "
                         "digit and contain only letters, digits and ._/:@+-")
    return s


def persistent_run_argv(name: str, host_dir: Path, image: str, allow_network: bool,
                        mem: int | None = None, cpu: float | None = None, agent_id: str = "") -> list[str]:
    """`docker run -d` for a long-lived agent home: ONLY the agent dir is mounted, at /agent."""
    image = check_image(image)
    mnt = agentfs.CONTAINER_AGENT_MOUNT
    labels = ["--label", "avrone.agent-home=1"]
    if agent_id:
        labels += ["--label", f"avrone.agent={agentfs.sanitize_agent_id(agent_id)}"]
    return ["docker", "run", "-d", "--name", name, *labels,
            *hardening_args(allow_network, mem, cpu),
            *agentfs.agent_volume_args(host_dir), "-w", mnt, "-e", f"AVRONE_AGENT_DIR={mnt}",
            image, "sleep", "infinity"]


# In-container launcher. `docker exec`/`docker run` clients that we kill on a timeout do NOT kill
# the process inside the container, so the command enforces its own deadline there: `timeout -s
# KILL` when the image has a timeout that understands that syntax (GNU, busybox >= 1.30; probed,
# so an old busybox just runs without it), a couple of seconds after the client gives up. It also
# runs images with no bash (alpine, busybox) under sh instead of failing with exit 127.
# Called as: sh -c GUARD <deadline-secs> <cmd>   ($0 = deadline, $1 = cmd)
GUARD = ('t=$0; c=$1; if command -v bash >/dev/null 2>&1; then i=bash; f=-lc; else i=sh; f=-c; fi; '
         'if [ "$t" -gt 0 ] 2>/dev/null && command -v timeout >/dev/null 2>&1 '
         '&& timeout -s KILL 5 true >/dev/null 2>&1; then exec timeout -s KILL "$t" "$i" "$f" "$c"; fi; '
         'exec "$i" "$f" "$c"')
DEADLINE_SLACK = 2
MAX_GUARD_SECS = 7 * 86400


def launcher(cmd: str, timeout: float | None = None) -> list[str]:
    """Tail of a docker argv that runs `cmd`. With no timeout: the historical `bash -lc cmd`."""
    if timeout is None:
        return ["bash", "-lc", cmd]
    try:
        secs = int(min(float(timeout), MAX_GUARD_SECS))  # inf / 10**400 raised OverflowError here
    except (TypeError, ValueError, OverflowError):
        secs = 0
    return ["sh", "-c", GUARD, str(max(0, secs) + DEADLINE_SLACK if secs > 0 else 0), cmd]


_DEAD_RE = re.compile(r"Error(?: response from daemon)?:\s*(?:No such container|[Cc]ontainer \S+ is "
                      r"(?:not running|paused|restarting))")


def container_gone(res: Any, name: str) -> bool:
    """True when docker refused to run the command because the container is gone or stopped
    (daemon restart, OOM kill, someone ran `docker rm`). The command never started, so a retry
    after recreating the container cannot run it twice."""
    err = str(getattr(res, "stderr", "") or "")
    return (getattr(res, "exit_code", 0) in (1, 125, 126) and not str(getattr(res, "stdout", "") or "").strip()
            and bool(_DEAD_RE.search(err)))


def exec_argv(name: str, cmd: str, timeout: float | None = None) -> list[str]:
    mnt = agentfs.CONTAINER_AGENT_MOUNT
    return ["docker", "exec", "-w", mnt, "-e", f"AVRONE_AGENT_DIR={mnt}", name, *launcher(cmd, timeout)]


def inspect_argv(name: str) -> list[str]:
    # Running state, then the host dir mounted at /agent: a same-named container can belong to a
    # different workspace (two AVRONE instances on one host share node id + agent role).
    fmt = ("{{.State.Running}}|{{range .Mounts}}{{if eq .Destination \"%s\"}}{{.Source}}{{end}}{{end}}"
           % agentfs.CONTAINER_AGENT_MOUNT)
    return ["docker", "inspect", "-f", fmt, name]


def parse_inspect(stdout: Any) -> tuple[bool, str]:
    """(running, mounted host dir or "") from inspect_argv's output. Older output (just "true")
    reports no mount, which is accepted as before."""
    if isinstance(stdout, (bytes, bytearray)):  # round 60: str(b"true|/x") was "b'true|/x'" = not running,
        stdout = bytes(stdout).decode("utf-8", "replace")  # so a live home got rm -f'd and recreated
    text = str(stdout or "").strip().splitlines()
    first = text[0] if text else ""
    state, _, src = first.partition("|")
    return state.strip().lower() == "true", src.strip()


class ForeignContainer(RuntimeError):
    """A container with this agent's name mounts some other directory."""


def rm_argv(name: str) -> list[str]:
    return ["docker", "rm", "-f", name]


def _same_dir(src: str, d: Path) -> bool:
    try:
        return Path(src).resolve() == Path(d).resolve()
    except (OSError, ValueError, RuntimeError):
        return src == str(d)


Exec = Callable[[list[str], int], Awaitable[Any]]  # (argv, timeout) -> CommandResult-like


@dataclass
class _Home:
    name: str
    agent_dir: Path
    last_used: float
    created: float = 0.0
    uses: int = 0
    active: int = 0  # commands running in it now (round 60): never reaped while > 0
    extra: dict[str, Any] = field(default_factory=dict)


class AgentContainerPool:
    """Lazily creates, reuses and idle-reaps one container per agent dir."""

    def __init__(self, exec_fn: Exec, image: str, allow_network: bool, node: str | None = None,
                 clock: Callable[[], float] = time.monotonic, idle: float | None = None):
        self._exec = exec_fn
        self.image = image
        self.allow_network = allow_network
        self.node = node or local_node_id()
        self.clock = clock
        self._idle = idle
        self.homes: dict[str, _Home] = {}
        self._lock = asyncio.Lock()
        self.created = 0
        self.reaped = 0

    @property
    def idle_after(self) -> float:
        if self._idle is None:
            return idle_secs()
        try:
            v = float(self._idle)
        except (TypeError, ValueError, OverflowError):
            return idle_secs()
        # NaN never reaped anything (every comparison False); negative reaped on every call
        return idle_secs() if v != v or v < 0 else min(v, 7 * 86400.0)

    def run_argv(self, agent_dir: Path) -> list[str]:
        d = Path(agent_dir).resolve()
        return persistent_run_argv(container_name(self.node, d.name), d, self.image, self.allow_network,
                                   agent_id=d.name)

    async def ensure(self, agent_dir: Path) -> str:
        """Name of a running home container for `agent_dir`, creating it if needed."""
        d = Path(agent_dir).resolve()
        name = container_name(self.node, d.name)
        await self.reap_idle(skip=name)
        async with self._lock:
            now = self.clock()
            home = self.homes.get(name)
            if home is not None:
                if home.agent_dir != d:  # round 60: never hand one agent's home to another dir
                    raise ForeignContainer(f"container {name} is the home of {str(home.agent_dir)!r}, "
                                           f"not {str(d)!r}")
                home.last_used, home.uses = now, home.uses + 1
                return name
            res = await self._exec(inspect_argv(name), 30)
            exists = getattr(res, "exit_code", 1) == 0
            running, src = parse_inspect(getattr(res, "stdout", "")) if exists else (False, "")
            if exists and src and not _same_dir(src, d):
                # Before: adopted (running) or rm -f'd (stopped) whatever held the name, so this
                # agent ran with another workspace's /agent mounted, or deleted its container.
                raise ForeignContainer(f"container {name} mounts {src!r}, not {str(d)!r}: it belongs to "
                                       f"another workspace on this host; set AVRONE_NODE_ID to tell them apart")
            if not running:
                await self._exec(rm_argv(name), 30)  # a stopped leftover with that name; ignore errors
                res = await self._exec(self.run_argv(d), 300)
                if getattr(res, "exit_code", 1) != 0:
                    raise RuntimeError(f"could not start agent container {name}: "
                                       f"{str(getattr(res, 'stderr', ''))[:300]}")
                self.created += 1
            self.homes[name] = _Home(name, d, now, created=now, uses=1)
            return name

    def begin(self, name: str) -> None:
        """A command starts in `name`. Round 60: idleness was measured from ensure(), so a command
        running longer than AVRONE_AGENT_IDLE_SECS had its container rm -f'd under it as soon as
        any other agent called ensure() (the reaper saw it as idle). Pair with end()."""
        h = self.homes.get(name)
        if h is not None:
            h.active += 1
            h.last_used = self.clock()

    def end(self, name: str) -> None:
        """The command started by begin() finished: idleness counts from now."""
        h = self.homes.get(name)
        if h is not None:
            h.active = max(0, h.active - 1)
            h.last_used = self.clock()

    async def forget(self, name: str) -> None:
        """Drop a home whose container died, so the next ensure() recreates it."""
        async with self._lock:
            self.homes.pop(name, None)

    async def reap_idle(self, now: float | None = None, skip: str = "") -> list[str]:
        """Remove every home container idle for longer than AVRONE_AGENT_IDLE_SECS."""
        now = self.clock() if now is None else now
        limit = self.idle_after
        # The stale decision and the `docker rm` are one critical section. The rm used to run after
        # the lock was released: a concurrent ensure() for that same agent then found no home
        # record, inspected the still-running container, re-registered and handed it out, and the
        # reaper's rm -f killed it under the agent (its next exec failed / its state was lost).
        async with self._lock:
            stale = [h for n, h in self.homes.items()
                     if n != skip and h.active <= 0 and now - h.last_used >= limit]
            for h in stale:
                del self.homes[h.name]
            for h in stale:
                try:
                    await self._exec(rm_argv(h.name), 30)
                    self.reaped += 1
                except Exception:  # noqa: BLE001 - a failed rm never breaks a command
                    pass
        return [h.name for h in stale]

    async def close(self) -> None:
        async with self._lock:  # same as reap_idle: no ensure() can re-adopt a home mid-removal
            names = list(self.homes)
            self.homes.clear()
            for n in names:
                try:
                    await self._exec(rm_argv(n), 30)
                except Exception:  # noqa: BLE001
                    pass

    def state(self) -> dict[str, Any]:
        now = self.clock()
        return {"mode": "persistent", "node": self.node, "idle_secs": self.idle_after,
                "created": self.created, "reaped": self.reaped,
                "homes": {n: {"agent": h.agent_dir.name, "uses": h.uses, "active": h.active,
                              "idle_s": 0.0 if h.active else round(max(0.0, now - h.last_used), 1)}
                          for n, h in self.homes.items()}}
