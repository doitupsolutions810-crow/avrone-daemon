"""Round 168: lesson metadata 'task' was stored raw in memory.jsonl; scrub() skipped metadata."""
from __future__ import annotations

from avrone import bridge as br
from avrone.memstore import MemoryStore


def _secret() -> str:
    return "xai-" + "Q7" * 20  # built at run time (push protection)


class _B:
    run_id = "r1"


def test_scrub_masks_string_metadata(tmp_path):
    p = tmp_path / "memory.jsonl"
    st = MemoryStore(p)
    st.save("lessons", "k1", "plain lesson", run_id="r1", task=f"deploy {_secret()}")
    before = st.load("lessons")["k1"]
    r = MemoryStore(p).scrub()
    assert r.get("meta") == 1 and r["values"] == 0
    raw = p.read_text()
    assert _secret() not in raw and "Q7Q7Q7" not in raw
    rec = MemoryStore(p).load("lessons")["k1"]
    assert rec["value"] == "plain lesson" and rec["run_id"] == "r1"
    assert rec["ts"] > before["ts"]  # newer LWW ts so a raw peer copy cannot win
    assert MemoryStore(p).scrub() == {"values": 0, "keys": 0}


def test_distill_lessons_redacts_task(tmp_path):
    class B:
        run_id = "r1"

        def posts(self):
            return [{"kind": "conclusion", "text": "always pin versions"}]

    saved = br.distill_lessons(tmp_path, B(), task=f"do it with {_secret()} " + "x" * 300, k=1)
    assert saved, "no lesson saved"
    raw = (tmp_path / "memory.jsonl").read_text()
    assert _secret() not in raw and "Q7Q7Q7" not in raw
    recs = list(MemoryStore(tmp_path / "memory.jsonl").load(br.LESSONS_NS).values())
    assert recs[0]["task"].startswith("do it with") and len(recs[0]["task"]) <= 200
