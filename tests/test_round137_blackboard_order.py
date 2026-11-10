"""Round 137: the blackboard fan-in order survives a restart at any size.

v0.129 (probe /workspace/work/bbprobe.py): _save_order wrote the order file at any size, but
_load_order refused one over _ORDER_MAX_BYTES (1 MiB, about 12k 80-character ids or 95k short
ones) and rebuilt the order by timestamp. After a restart, a post accepted late with an older ts
sorted before the relay's `since` cursor, so the relay never saw it. That is the case the append
order exists to prevent.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as S

from avrone import fusion_blackboard as fb

RUNS, PER = 30, 450


def _seed(d: Path) -> None:
    (d / "bridge").mkdir(parents=True)
    for r in range(RUNS):
        with open(d / "bridge" / f"run{r}.jsonl", "w") as f:
            for i in range(PER):
                pid = (f"r{r}p{i}-" + "x" * 80)[:80]
                f.write(json.dumps({"id": pid, "run_id": f"run{r}", "agent": "a", "role": "", "kind": "finding",
                                    "text": "t", "refs": [], "ts": 1_700_000_000 + r * PER + i,
                                    "origin_node": "local"}) + "\n")


def test_late_old_post_after_cursor_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "5000")
    _seed(tmp_path)
    core = S(settings=S(data_dir=tmp_path))
    posts = fb.blackboard_posts(core)
    assert len(posts) == RUNS * PER
    cursor = posts[-1]["id"]
    late, err = fb.normalize_blackboard_post({"id": "late-old", "text": "arrived late", "run_id": "run0",
                                              "ts": 1_600_000_000})
    assert err is None
    dup, _ = fb.blackboard_accept(core, late)
    assert not dup
    assert [p["id"] for p in fb.blackboard_posts(core, since=cursor)] == ["late-old"]
    order_file = tmp_path / "bridge" / "_fanin_order.json"
    assert order_file.stat().st_size > 1_048_576  # the size v0.129 refused to read back
    restarted = S(settings=S(data_dir=tmp_path))
    assert [p["id"] for p in fb.blackboard_posts(restarted, since=cursor)] == ["late-old"]


def test_order_cap_holds_many_ids():
    assert fb._ORDER_MAX_BYTES >= 64 * 1024 * 1024
