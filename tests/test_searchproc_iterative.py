"""Regression for the Python 3.11 deep-workspace os.walk recursion crash."""
import io
import json
from avrone import searchproc

def test_search_scan_never_needs_recursive_os_walk(tmp_path, monkeypatch):
    folder = tmp_path / "a" / "b"
    folder.mkdir(parents=True)
    (folder / "needle.txt").write_text("needle\n", encoding="utf-8")
    def recursion_dependency(*_args, **_kwargs):
        raise RecursionError("old os.walk frame-depth failure")
    monkeypatch.setattr(searchproc.os, "walk", recursion_dependency)
    matches = list(searchproc._iter_files(tmp_path, set(), set()))
    assert [x.relative_to(tmp_path).as_posix() for x in matches] == ["a/b/needle.txt"]
    output = io.StringIO()
    searchproc.run({"root": str(tmp_path), "pattern": "needle"}, output)
    rows = [json.loads(x) for x in output.getvalue().splitlines()]
    assert rows[0]["text"] == "needle"
    assert rows[-1]["done"] is True

def test_iterative_scan_keeps_sorted_order_and_no_symlink_descent(tmp_path):
    for name in ("b", "a", "skip"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "hit.txt").write_text("hit", encoding="utf-8")
    (tmp_path / "top.txt").write_text("hit", encoding="utf-8")
    (tmp_path / "linked").symlink_to(tmp_path / "a", target_is_directory=True)
    paths = [p.relative_to(tmp_path).as_posix() for p in
             searchproc._iter_files(tmp_path, {"skip"}, set())]
    assert paths == ["top.txt", "a/hit.txt", "b/hit.txt"]
    paths = [p.relative_to(tmp_path).as_posix() for p in
             searchproc._iter_files(tmp_path, set(), {str(tmp_path / "a")})]
    assert paths == ["top.txt", "b/hit.txt", "skip/hit.txt"]


def test_search_max_files_is_a_hard_upper_bound(tmp_path):
    (tmp_path / "a.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle", encoding="utf-8")
    for limit in (0, 1):
        output = io.StringIO()
        searchproc.run({"root": str(tmp_path), "pattern": "needle", "max_files": limit}, output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        assert len([row for row in rows if "path" in row]) <= limit
        assert rows[-1]["scanned"] == limit
