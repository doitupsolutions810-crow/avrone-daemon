"""Sovereign OS: the environment agents live and work in.

On launch AVRONE establishes this environment first (workspace, runner,
health check) so hard requests never fail on a missing sandbox. Commands run
in a Docker container when Docker is available (the "VM environment"),
otherwise in a local subprocess confined to the workspace directory.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import re
import signal
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import agentcontainer, agentfs
from .config import Settings


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_SIGNALS = {int(v): v.name for v in signal.Signals}
OUTPUT_HEAD_BYTES = 1024 * 1024   # per stream, kept in memory from the start ...
OUTPUT_TAIL_BYTES = 256 * 1024    # ... plus the last 256 KB; the middle is counted, not kept
CMD_ECHO_MAX = 400


def _pipe_grace() -> float:
    try:
        return max(0.0, min(10.0, float(os.environ.get("AVRONE_SHELL_PIPE_GRACE", "0.5"))))
    except ValueError:
        return 0.5


def clean_output(s: str) -> str:
    """Terminal noise out, content in: ANSI colour/cursor codes dropped, and a line redrawn with
    carriage returns (pip/npm/curl progress bars) collapsed to what the terminal finally showed."""
    if not s:
        return s
    s = _ANSI_RE.sub("", s).replace("\r\n", "\n")
    if "\r" in s:
        s = "\n".join(seg.rstrip("\r").rsplit("\r", 1)[-1] for seg in s.split("\n"))
    return s


def _head_tail(s: str, budget: int) -> str:
    """Keep the start and the end of `s` (errors and summaries live at the end)."""
    if len(s) <= budget:
        return s
    marker = "\n...[{} chars cut]...\n"
    room = budget - len(marker.format(len(s)))
    if room < 40:
        return s[:max(0, budget)]
    head = room * 2 // 5
    tail = room - head
    return s[:head] + marker.format(len(s) - head - tail) + s[len(s) - tail:]


MAX_COMMAND_TIMEOUT_S = 24 * 3600
DEFAULT_COMMAND_TIMEOUT_S = 120


def _norm_timeout(t: Any, default: Any = None) -> int | float:
    """A usable timeout in seconds. Before: 10**400 or "5" raised AFTER the process was spawned
    (the child and its pipe readers leaked), inf meant never time out, NaN/negative/tiny values
    killed the command at once and reported it as a TIMEOUT."""
    for v in (t, default, DEFAULT_COMMAND_TIMEOUT_S):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        try:
            f = float(v)
        except OverflowError:
            return MAX_COMMAND_TIMEOUT_S
        if math.isnan(f) or f <= 0:
            continue
        f = min(f, float(MAX_COMMAND_TIMEOUT_S))
        return int(f) if f.is_integer() else f  # "timed out after 5s", as before
    return DEFAULT_COMMAND_TIMEOUT_S


def exit_label(code: int, timed_out: bool = False) -> str:
    if not isinstance(code, int) or isinstance(code, bool):
        code = -1
    if timed_out:
        return f"exit {code} TIMEOUT"
    if code < -1 and -code in _SIGNALS:
        return f"exit {code} {_SIGNALS[-code]}"
    if 128 < code < 160 and code - 128 in _SIGNALS:
        name = _SIGNALS[code - 128]
        hint = ", often out of memory" if name == "SIGKILL" else ""
        return f"exit {code} = killed by {name}{hint}"
    return f"exit {code}"


async def _reap(proc) -> None:
    """Kill a child (and, when it leads its own session, its whole process group: `cmd &`
    grandchildren included) that may already have exited, then wait for it. Never raises."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and getattr(proc, "_avrone_pgroup", False):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 - reaping is best effort
        pass


class _Capture:
    """Bounded reader for one pipe: head + rolling tail, middle counted. A command that prints
    forever (`yes`, a chatty server) can no longer grow the server's memory without limit."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = OUTPUT_HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > OUTPUT_TAIL_BYTES:
                del self.tail[:len(self.tail) - OUTPUT_TAIL_BYTES]

    def text(self) -> str:
        cut = self.total - len(self.head) - len(self.tail)
        if cut <= 0:
            return (bytes(self.head) + bytes(self.tail)).decode(errors="replace")
        return (self.head.decode(errors="replace") + f"\n...[{cut} bytes of output not kept]...\n"
                + self.tail.decode(errors="replace"))


async def _exited(proc) -> None:
    """Wait for the process itself to exit. `proc.wait()` also waits for its pipes to close, so
    `server &` (a grandchild holding stdout) used to stall every command until the timeout."""
    delay = 0.005
    while proc.returncode is None:
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.05)


async def _pump(stream, cap: _Capture) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        cap.feed(chunk)


@dataclass
class CommandResult:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def text(self, limit: int = 6000) -> str:
        """Model-facing view: the command echo is clipped (a heredoc can't eat the budget),
        terminal noise is cleaned, and when it must cut it keeps the head and the tail of each
        stream with stderr guaranteed a share, since the error is usually at the end."""
        try:  # a negative limit used to return the whole text uncut
            limit = max(0, int(limit))
        except OverflowError:  # inf: no cut
            limit = sys.maxsize
        except (TypeError, ValueError):
            limit = 6000
        cmd = self.cmd if len(self.cmd) <= CMD_ECHO_MAX else (
            self.cmd[:CMD_ECHO_MAX - 60] + f" ...[command clipped, {len(self.cmd)} chars]")
        label = exit_label(self.exit_code, self.timed_out)
        if self.exit_code == 125 and not self.timed_out and ("Error response from daemon" in self.stderr
                                                             or self.stderr.startswith("docker:")):
            label += " = docker failed before the command ran"
        header = f"$ {cmd}\n[{label}]\n"
        out, err = clean_output(self.stdout), clean_output(self.stderr)
        err_block = ("\n[stderr]\n" + err) if err else ""
        full = header + out + err_block
        if len(full) <= limit:
            return full
        budget = limit - len(header)
        if budget < 200:  # tiny limits: plain head cut, as before
            return full[:limit] + "\n...[truncated]"
        err_b = min(len(err_block), budget // 3) if err_block else 0
        out_b = budget - err_b
        if len(out) < out_b:
            err_b += out_b - len(out)
            out_b = len(out)
        body = _head_tail(out, out_b)
        if err_block:
            body += "\n[stderr]\n" + _head_tail(err, max(0, err_b - len("\n[stderr]\n")))
        return (header + body)[:limit] + "\n...[truncated]"


class Runner:
    kind = "base"

    def __init__(self, settings: Settings):
        self.s = settings
        self.workspace: Path = settings.workspace

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def run(self, cmd: str, timeout: int | None = None, cwd: Path | None = None) -> CommandResult:
        raise NotImplementedError

    def _agent_cwd(self, cwd: Path | None) -> Path | None:
        """Validate a per-agent cwd: it must be one confined workspace/agents/<id> dir."""
        if cwd is None:
            return None
        if not agentfs.is_agent_dir(self.workspace, cwd):
            raise agentfs.AgentFSError(f"cwd {cwd} is not a confined agent dir under the workspace")
        return Path(cwd).resolve()

    def agent_env(self, agent_cwd: Path, in_container: str | None = None) -> dict[str, str]:
        return {"AVRONE_AGENT_DIR": in_container or str(agent_cwd),
                "AVRONE_SHARED_WORKSPACE": agentfs.CONTAINER_WORKSPACE if in_container else str(self.workspace.resolve())}

    async def _exec(self, argv: list[str], cmd: str, timeout: int, cwd: Path | None = None,
                    env: dict[str, str] | None = None) -> CommandResult:
        timeout = _norm_timeout(timeout, getattr(self.s, "command_timeout", None))
        if any("\x00" in a for a in argv):
            return CommandResult(cmd.replace("\x00", "\\0"), -1, "", "refused: command contains a NUL byte")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd) if cwd else None, env={**os.environ, **env} if env else None,
                stdin=asyncio.subprocess.DEVNULL,  # never read (or hang on) the server's own stdin
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)  # own process group: timeout/cancel kills `cmd &` children too
        except (OSError, ValueError) as e:  # E2BIG, missing cwd/binary, bad env: a result, not a crash
            return CommandResult(cmd, -1, "", f"could not start command: {type(e).__name__}: {e}"[:500])
        proc._avrone_pgroup = True
        cap_out, cap_err = _Capture(), _Capture()
        readers = [asyncio.ensure_future(_pump(proc.stdout, cap_out)),
                   asyncio.ensure_future(_pump(proc.stderr, cap_err))]
        note = ""
        try:
            try:
                await asyncio.wait_for(_exited(proc), timeout=timeout)
            except asyncio.TimeoutError:
                await _reap(proc)
                await asyncio.wait(readers, timeout=1.0)
                for t in readers:
                    t.cancel()
                err = cap_err.text()
                err = (err + "\n" if err and not err.endswith("\n") else err) + f"command timed out after {timeout}s"
                return CommandResult(cmd, -1, cap_out.text(), err, timed_out=True)
            # the command exited; a background child (`server &`) may still hold the pipes open.
            # Give the readers a moment, then return instead of waiting for that child forever.
            _done, pending = await asyncio.wait(readers, timeout=_pipe_grace())
            if pending:
                for t in pending:
                    t.cancel()
                tr = getattr(proc, "_transport", None)
                if tr is not None:  # release our pipe ends now instead of leaking them to GC
                    try:
                        tr.close()
                    except Exception:  # noqa: BLE001
                        pass
                note = ("\n[note: a background process still holds the output pipe; "
                        "redirect its output to a file (cmd > log 2>&1 &) to see it]")
        except asyncio.CancelledError:
            # a cancelled run (evolve_cancel, client gone, squad shutdown) must not leave the child running
            for t in readers:
                t.cancel()
            await asyncio.shield(_reap(proc))
            raise
        err = cap_err.text() + note if (cap_err.total or note) else ""
        return CommandResult(cmd, proc.returncode or 0, cap_out.text(), err.lstrip("\n") if not cap_err.total else err)


class LocalRunner(Runner):
    kind = "local"

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def run(self, cmd: str, timeout: int | None = None, cwd: Path | None = None) -> CommandResult:
        agent_cwd = self._agent_cwd(cwd)
        if agent_cwd is None:  # non-agent command: unchanged, runs at the workspace root
            return await self._exec(["bash", "-lc", cmd], cmd, timeout or self.s.command_timeout, cwd=self.workspace)
        return await self._exec(["bash", "-lc", cmd], cmd, timeout or self.s.command_timeout, cwd=agent_cwd,
                                env=self.agent_env(agent_cwd))


class DockerRunner(Runner):
    """One long-lived container per AVRONE instance; workspace is bind-mounted at /workspace."""
    kind = "docker"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.container = f"avrone-env-{uuid.uuid4().hex[:8]}"
        self.agent_pool: agentcontainer.AgentContainerPool | None = None  # AVRONE_AGENT_CONTAINER=persistent
        self.restarts = 0

    def _pool(self) -> agentcontainer.AgentContainerPool:
        if self.agent_pool is None:
            async def _run(argv: list[str], timeout: int) -> CommandResult:
                return await self._exec(argv, " ".join(argv), timeout)
            self.agent_pool = agentcontainer.AgentContainerPool(_run, self.s.docker_image, self.s.allow_network)
        return self.agent_pool

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        argv = ["docker", "run", "-d", "--rm", "--name", self.container,
                "-v", f"{self.workspace}:/workspace", "-w", "/workspace"]
        if not self.s.allow_network:
            argv += ["--network", "none"]
        argv += [self.s.docker_image, "sleep", "infinity"]
        res = await self._exec(argv, " ".join(argv), 300)
        if res.exit_code != 0:
            raise RuntimeError(f"Could not start environment container: {res.stderr}")

    async def stop(self) -> None:
        if self.agent_pool is not None:
            await self.agent_pool.close()
        await self._exec(["docker", "rm", "-f", self.container], "docker rm", 30)

    def exec_argv(self, cmd: str, agent_cwd: Path | None = None, timeout: float | None = None,
                  run_name: str | None = None) -> list[str]:
        """argv for one command. Agent commands run in their own dir.

        Default: `docker exec -w /workspace/agents/<id>` in the shared container (the agent dir
        is the same host dir via the /workspace bind mount). With AVRONE_AGENT_DOCKER_ISOLATE=1:
        a throwaway `docker run --rm` that mounts ONLY that agent dir, as `-v <dir>:/agent`.
        With `timeout`, the command carries its own in-container deadline (agentcontainer.GUARD);
        `run_name` names the throwaway container so a timed-out one can be removed."""
        tail = agentcontainer.launcher(cmd, timeout)
        if agent_cwd is None:
            return ["docker", "exec", self.container, *tail]
        if agentfs.docker_isolate():
            mnt = agentfs.CONTAINER_AGENT_MOUNT
            argv = ["docker", "run", "--rm"]
            if run_name:
                argv += ["--name", run_name]
            argv += [*agentfs.agent_volume_args(agent_cwd), "-w", mnt, "-e", f"AVRONE_AGENT_DIR={mnt}"]
            # same hardening as the persistent agent home (cap_drop ALL, no-new-privileges,
            # mem/cpu/pids limits, --network none unless AVRONE_ALLOW_NETWORK)
            argv += agentcontainer.hardening_args(self.s.allow_network)
            return argv + [self.s.docker_image, *tail]
        inner = agentfs.container_agent_path(self.workspace, agent_cwd)
        argv = ["docker", "exec", "-w", inner]
        for k, v in self.agent_env(agent_cwd, in_container=inner).items():
            argv += ["-e", f"{k}={v}"]
        return argv + [self.container, *tail]

    async def _remove(self, name: str) -> None:
        try:
            await self._exec(["docker", "rm", "-f", name], "docker rm", 30)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass

    async def run(self, cmd: str, timeout: int | None = None, cwd: Path | None = None) -> CommandResult:
        agent_cwd = self._agent_cwd(cwd)
        t = _norm_timeout(timeout, self.s.command_timeout)
        if agent_cwd is not None and agentcontainer.container_mode() == "persistent":
            # long-lived per-agent home container (only the agent dir mounted at /agent), reused
            pool = self._pool()
            name = await pool.ensure(agent_cwd)
            res = await self._exec(agentcontainer.exec_argv(name, cmd, t), cmd, t)
            if agentcontainer.container_gone(res, name):  # the home died under us: recreate once
                await pool.forget(name)
                self.restarts += 1
                name = await pool.ensure(agent_cwd)
                res = await self._exec(agentcontainer.exec_argv(name, cmd, t), cmd, t)
            return res
        if agent_cwd is not None and agentfs.docker_isolate():
            run_name = f"avrone-run-{uuid.uuid4().hex[:12]}"
            try:
                res = await self._exec(self.exec_argv(cmd, agent_cwd, t, run_name), cmd, t)
            except asyncio.CancelledError:
                await asyncio.shield(self._remove(run_name))
                raise
            if getattr(res, "timed_out", False):  # killing the docker client leaves a --rm container running
                await self._remove(run_name)
            return res
        res = await self._exec(self.exec_argv(cmd, agent_cwd, t), cmd, t)
        if agentcontainer.container_gone(res, self.container):
            # the shared environment container died (daemon restart, OOM): bring it back once
            self.restarts += 1
            await self._remove(self.container)
            try:
                await self.start()
            except Exception as e:  # noqa: BLE001
                res.stderr = (res.stderr.rstrip("\n") + f"\n[could not restart the environment container: {e}]")[:2000]
                return res
            res = await self._exec(self.exec_argv(cmd, agent_cwd, t), cmd, t)
        return res


async def establish(settings: Settings) -> Runner:
    """Pick and boot the runner, then health-check it."""
    choice = settings.runner
    if choice == "auto":
        choice = "docker" if shutil.which("docker") else "local"
    runner: Runner = DockerRunner(settings) if choice == "docker" else LocalRunner(settings)
    try:
        await runner.start()
    except Exception:
        if choice == "docker" and settings.runner == "auto":
            runner = LocalRunner(settings)
            await runner.start()
        else:
            raise
    check = await runner.run("echo ready", timeout=60)
    if check.exit_code != 0 and choice == "docker" and settings.runner == "auto":
        # docker answered but the environment can't run a command (image without a shell, a
        # daemon that died after start): auto mode falls back to local instead of refusing to boot
        try:
            await runner.stop()
        except Exception:  # noqa: BLE001
            pass
        runner = LocalRunner(settings)
        await runner.start()
        check = await runner.run("echo ready", timeout=60)
    if check.exit_code != 0:
        raise RuntimeError(f"Environment health check failed: {check.text()}")
    return runner
