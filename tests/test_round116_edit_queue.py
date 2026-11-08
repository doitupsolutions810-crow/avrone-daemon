"""Round 116: queued edit_file calls wait on the loop, not in worker threads; cancelled edits
release the per-file lock; secret fix_runs rewrites run logs with long names."""
from __future__ import annotations

import asyncio

from tests.test_round115_edit_races import _core, _ctx

def test_queued_edits_do_not_starve_worker_threads(tmp_path):
    """v0.108: every queued edit of one file parked a to_thread worker on the per-path lock, so
    other off-loop tools waited ~the whole queue (0.43 s at 24 edits of a 7 MB file)."""
    import time
    core = _core(tmp_path)
    n = 24
    f = core.settings.workspace / "big.txt"
    f.write_text("".join(f"line{i}=old\n" for i in range(n)) + ("x" * 120 + "\n") * 60000)

    async def go():
        lat: list[float] = []

        async def probe():
            while True:
                t = time.perf_counter()
                await asyncio.to_thread(lambda: None)
                lat.append(time.perf_counter() - t)
                await asyncio.sleep(0.005)
        pt = asyncio.create_task(probe())
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        outs = await asyncio.gather(*[core.tools.call(_ctx(core, i), "edit_file",
                                                      {"path": "big.txt", "old": f"line{i}=old", "new": f"line{i}=new"})
                                      for i in range(n)])
        el = time.perf_counter() - t0
        pt.cancel()
        return outs, el, max(lat)
    outs, el, worst = asyncio.run(go())
    assert all(o.startswith("edited") for o in outs), outs[:3]
    assert f.read_text().count("=new") == n
    assert worst < max(0.15, 0.4 * el), f"worker pool starved {worst:.3f}s during {el:.3f}s of edits"


def test_cancelled_edit_releases_lock(tmp_path):
    core = _core(tmp_path)
    f = core.settings.workspace / "c.txt"
    f.write_text("a\nb\n")

    async def go():
        tasks = [asyncio.create_task(core.tools.call(_ctx(core, i), "edit_file",
                                                     {"path": "c.txt", "old": "a" if i == 0 else "b", "new": f"X{i}"}))
                 for i in range(2)]
        await asyncio.sleep(0)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.2)  # a started worker finishes and releases
        return await asyncio.wait_for(core.tools.call(_ctx(core), "edit_file",
                                                      {"path": "c.txt", "old": "\n", "new": "\n", "replace_all": True}), 5)
    out = asyncio.run(go())
    assert "identical" in out  # reached the tool body: the lock was free


def test_cancelled_edit_then_edit_succeeds(tmp_path):
    core = _core(tmp_path)
    f = core.settings.workspace / "d.txt"
    f.write_text("p\nq\n")

    async def go():
        t = asyncio.create_task(core.tools.call(_ctx(core), "edit_file", {"path": "d.txt", "old": "p", "new": "P"}))
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        await asyncio.sleep(0.2)
        return await asyncio.wait_for(core.tools.call(_ctx(core), "edit_file", {"path": "d.txt", "old": "q", "new": "Q"}), 5)
    out = asyncio.run(go())
    assert out.startswith("edited"), out
    assert f.read_text().endswith("Q\n")


def test_secret_fix_long_run_log_name(tmp_path):
    from avrone import tools_secrets
    root = tmp_path / "runs"; root.mkdir()
    name = ("r" * 240) + ".jsonl"
    (root / name).write_text('{"msg": "tok ghp_' + "a" * 36 + '"}\n')
    n = tools_secrets.fix_runs(root, {name})
    assert n == 1 and "ghp_" + "a" * 36 not in (root / name).read_text()
