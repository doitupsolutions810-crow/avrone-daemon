"""Round 64: MemoryStore.merge_many was quadratic into a full namespace.

For every incoming NEW key into a namespace at its cap, merge_many scanned the whole namespace
twice: once for "is this entry older than everything kept" and once in _put_locked to pick the key
to evict. With AVRONE_MEMORY_MAX_ENTRIES=5000, a fleet merge of 4000 entries took 6.7 s while
holding the store lock (every save, recall and other merge on the node waited). A per-call
min-heap (_Oldest, lazy deletion) makes it ~0.06 s. A seeded property test checks the resulting
store and stats against the old scanning algorithm exactly, ties included.
"""
from __future__ import annotations

import random
import time

import pytest

from avrone import memstore
from avrone.memstore import MemoryStore, _ts


class _Naive:
    """The pre-round-64 algorithm, behind the _Oldest interface."""

    def min_ts(self, ns, space):
        return min(_ts(r.get("ts", 0)) for r in space.values())

    def pop(self, ns, space):
        return min(space, key=lambda k: space[k].get("ts", 0))

    def put(self, ns, space, key, rec):
        pass


def _dump(st: MemoryStore):
    return {ns: {k: (r["value"], r["ts"]) for k, r in sp.items()} for ns, sp in st.namespaces().items()}


def test_merge_into_full_namespace_is_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "5000")
    st = MemoryStore(tmp_path / "m.jsonl")
    now = time.time()
    st.merge_many({"a": [{"key": f"k{i}", "value": "v", "ts": now - 100_000 + i} for i in range(5000)]})
    newer = [{"key": f"n{i}", "value": "v", "ts": now - 50_000 + i} for i in range(4000)]
    t = time.monotonic()
    assert st.merge_many({"a": newer}) == {"merged": 4000, "skipped": 0, "rejected": 0}
    older = [{"key": f"o{i}", "value": "v", "ts": now - 200_000 + i} for i in range(4000)]
    assert st.merge_many({"a": older}) == {"merged": 0, "skipped": 4000, "rejected": 0}
    assert time.monotonic() - t < 1.5
    space = st.load("a")
    assert len(space) == 5000
    assert all(f"n{i}" in space for i in range(4000)) and "k3999" not in space and "k4000" in space


@pytest.mark.parametrize("seed", range(10))
def test_heap_eviction_matches_old_scan(tmp_path, monkeypatch, seed):
    rnd = random.Random(seed)
    cap = rnd.randint(1, 6)
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", str(cap))
    base = 1_700_000_000.0
    groups_seq = []
    for _ in range(rnd.randint(1, 4)):
        groups = {}
        for ns in rnd.sample(["a", "b", "c"], rnd.randint(1, 3)):
            groups[ns] = [{"key": f"k{rnd.randint(0, 9)}", "value": f"v{rnd.randint(0, 99)}",
                           "ts": base + rnd.choice([0, 1, 1, 2, 3, 5, rnd.randint(0, 20)])}
                          for _ in range(rnd.randint(0, 15))]
        groups_seq.append(groups)
    pre = [{"key": f"p{i}", "value": "x", "ts": base + rnd.randint(0, 6)} for i in range(rnd.randint(0, cap))]

    def run(path, oldest_cls):
        monkeypatch.setattr(memstore, "_Oldest", oldest_cls)
        st = MemoryStore(path)
        stats = [st.merge_many({"a": list(pre)})]
        stats += [st.merge_many({k: list(v) for k, v in g.items()}) for g in groups_seq]
        return stats, _dump(st)

    real = memstore._Oldest
    new = run(tmp_path / "new.jsonl", real)
    old = run(tmp_path / "old.jsonl", _Naive)
    monkeypatch.setattr(memstore, "_Oldest", real)
    assert new == old


def test_same_ts_same_key_never_compares_dicts(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "2")
    st = MemoryStore(tmp_path / "m.jsonl")
    ents = [{"key": "k", "value": f"v{i}", "ts": 5.0 + (i // 3)} for i in range(9)] + \
           [{"key": f"z{i}", "value": "z", "ts": 9.0} for i in range(5)]
    st.merge_many({"a": ents})  # TypeError: '<' not supported between dict and dict, before
    assert len(st.load("a")) == 2
