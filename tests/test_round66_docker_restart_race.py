"""Round 66: concurrent commands racing to restart a dead shared environment container.

When the shared `avrone-env-*` container died (daemon restart, OOM), every command that hit it at
once ran the restart: each did `docker rm -f` + `docker run --name <same>`. The second remover
deleted the container the first had just started, so the first one's retry failed ("No such
container"), and overlapping `docker run --name` calls failed with a name conflict. N concurrent
commands meant N restarts and most of them returning errors. Now a restart lock plus a generation
counter: the first runner restarts, the rest see the new generation and simply retry.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from avrone.sovereign import CommandResult, DockerRunner


class FakeDocker:
    def __init__(self):
        self.containers: dict[str, bool] = {}
        self.runs = 0
        self.removes = 0

    async def __call__(self, argv, cmd, timeout, *a, **k):
        op = argv[1]
        if op == "run":
            name = argv[argv.index("--name") + 1]
            await asyncio.sleep(0.02)
            if name in self.containers:
                return CommandResult(cmd, 125, "", f'docker: Error response from daemon: Conflict. The container '
                                                   f'name "/{name}" is already in use.')
            self.containers[name] = True
            self.runs += 1
            return CommandResult(cmd, 0, "abc123\n", "")
        if op == "rm":
            await asyncio.sleep(0.005)
            self.removes += 1
            self.containers.pop(argv[-1], None)
            return CommandResult(cmd, 0, "", "")
        if op == "exec":
            name = next(x for x in argv[2:] if x.startswith("avrone-env-"))
            await asyncio.sleep(0.01)
            if not self.containers.get(name):
                return CommandResult(cmd, 1, "", f"Error response from daemon: No such container: {name}")
            return CommandResult(cmd, 0, "ok\n", "")
        raise AssertionError(argv)


def _runner(tmp_path):
    s = SimpleNamespace(workspace=tmp_path / "ws", docker_image="img", allow_network=False, command_timeout=30)
    r = DockerRunner(s)
    fake = FakeDocker()
    r._exec = fake  # type: ignore[assignment]
    return r, fake


def test_concurrent_commands_restart_the_dead_container_once(tmp_path):
    r, fake = _runner(tmp_path)

    async def go():
        await r.start()
        fake.containers.clear()  # the container died
        return await asyncio.gather(*(r.run(f"echo {i}") for i in range(6)))
    results = asyncio.run(go())
    assert [x.exit_code for x in results] == [0] * 6, [x.stderr for x in results]
    assert r.restarts == 1 and fake.runs == 2  # the boot + exactly one restart
    assert fake.containers == {r.container: True}


def test_second_death_after_restart_is_restarted_again(tmp_path):
    r, fake = _runner(tmp_path)

    async def go():
        await r.start()
        fake.containers.clear()
        a = await r.run("echo a")
        fake.containers.clear()  # dies again later: a new generation must restart it, not skip
        b = await r.run("echo b")
        return a, b
    a, b = asyncio.run(go())
    assert a.exit_code == 0 and b.exit_code == 0 and r.restarts == 2


def test_failed_restart_is_reported_and_retried_by_the_next_command(tmp_path):
    r, fake = _runner(tmp_path)
    real = fake.__call__
    fail = {"on": True}

    async def flaky(argv, cmd, timeout, *a, **k):
        if argv[1] == "run" and fail["on"] and fake.runs >= 1:
            return CommandResult(cmd, 125, "", "docker: Error response from daemon: out of disk")
        return await real(argv, cmd, timeout)
    r._exec = flaky  # type: ignore[assignment]

    async def go():
        await r.start()
        fake.containers.clear()
        a = await r.run("echo a")
        fail["on"] = False
        b = await r.run("echo b")
        return a, b
    a, b = asyncio.run(go())
    assert a.exit_code != 0 and "could not restart the environment container" in a.stderr
    assert b.exit_code == 0
