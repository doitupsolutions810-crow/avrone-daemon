"""Round 17 coverage: pack_list survives a pack whose stat fails; memory refuses an empty key."""
import asyncio
import os
from types import SimpleNamespace as S

import pytest

from avrone import agentpack
from avrone import tools_packpeer as tp
from avrone.memstore import MemoryRefused, MemoryStore


def test_pack_list_stat_error(tmp_path, monkeypatch):
    d = agentpack.pack_dir(tmp_path / "data")
    d.mkdir(parents=True)
    (d / "a-1.tar.gz").write_bytes(b"junk")
    real_scandir = os.scandir

    class _Entry:  # round 95: pack_list sizes entries from one scandir; a stat error means -1
        def __init__(self, de):
            self._de, self.name, self.path = de, de.name, de.path

        def is_symlink(self):
            return self._de.is_symlink()

        def is_dir(self, follow_symlinks=True):
            return self._de.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, follow_symlinks=True):
            if self.name == "a-1.tar.gz":
                raise OSError("gone")
            return self._de.stat(follow_symlinks=follow_symlinks)

    class _It:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()

        def __iter__(self):
            return (_Entry(de) for de in self._it)
    monkeypatch.setattr(os, "scandir", lambda path=".": _It(path))
    ctx = S(core=S(settings=S(data_dir=tmp_path / "data")))
    out = asyncio.run(tp.t_pack_list(ctx, {}))
    assert "a-1.tar.gz (-1 bytes): REFUSED" in out


def test_memory_empty_key_refused(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    with pytest.raises(MemoryRefused, match="key is required"):
        m.save("ns", "", "v")
