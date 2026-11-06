"""Round 12 coverage: checkpoint store write/lock failures, governor ramps and readers, hostview
/proc edge cases."""
import asyncio
import sys

import pytest

from avrone import checkpoint, governor
from avrone import tools_hostview as hv


def test_checkpoint_store_write_failure_counts(tmp_path, monkeypatch):
    st = checkpoint._Store(tmp_path / "ck" / "a.json")
    st.data["steps"]["s"] = {"x": 1}
    assert st.save() is True
    monkeypatch.setattr(checkpoint.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("ro")))
    assert st.save() is False and st.write_errors == 1
    assert not [p for p in (tmp_path / "ck").iterdir() if p.name.endswith(".tmp")]


def test_checkpoint_file_lock_resolve_error(tmp_path, monkeypatch):
    def bad_resolve(self, *a, **k):
        raise OSError("loop")
    monkeypatch.setattr(checkpoint.Path, "resolve", bad_resolve)
    lk1 = checkpoint._file_lock(tmp_path / "x.json")
    lk2 = checkpoint._file_lock(tmp_path / "x.json")
    assert lk1 is lk2


def test_ramp_down_both_orientations():
    assert governor._ramp_down(50, 60, 60) == 1.0 and governor._ramp_down(70, 60, 60) == 0.0
    assert governor._ramp_down(70, 60, 80) == pytest.approx(0.5)
    assert governor._ramp_down(70, 80, 60) == pytest.approx(0.5)


def test_psutil_reader(monkeypatch, tmp_path):
    class VM:
        percent = 42.5

    class PS:
        @staticmethod
        def virtual_memory():
            return VM
    monkeypatch.setitem(sys.modules, "psutil", PS)
    assert governor._psutil() is PS and governor.read_mem_pct(cgroup_root=str(tmp_path)) == 42.5


def test_disk_reader_walks_to_root(monkeypatch):
    monkeypatch.setattr(governor.os.path, "exists", lambda p: False)
    monkeypatch.setattr(governor.os.path, "dirname", lambda p: p)  # a path whose parent is itself
    assert governor.make_disk_reader("weird")() is None  # unreadable path: no reading, no exception


def test_hostview_boot_time_missing(monkeypatch, tmp_path):
    (tmp_path / "stat").write_text("cpu 1\n")
    monkeypatch.setattr(hv, "PROC", tmp_path)
    assert hv._boot_time() is None


def test_hostview_proc_unlistable(monkeypatch, tmp_path):
    class P(type(tmp_path)):
        def iterdir(self):
            raise PermissionError("no")
    monkeypatch.setattr(hv, "PROC", P(tmp_path))
    assert hv._children(1) == {}
    out = asyncio.run(hv.t_proc_status(None, {}))
    assert "0 child process(es)" in out
