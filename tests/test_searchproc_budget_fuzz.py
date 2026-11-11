"""Scan budgets and differential traversal checks for the iterative scanner."""
import io
import json
import os
import random
from avrone import searchproc
import pytest

@pytest.mark.parametrize("limits", [
    {"cap": 0}, {"cap": -1}, {"max_files": 0}, {"max_files": -4},
    {"cap": 1, "max_files": 0}, {"cap": 0, "max_files": 1},
])
def test_empty_budgets_must_not_even_open_filesystem(monkeypatch, limits):
    def fail(*args, **kwargs):
        raise AssertionError("called directory walker with no scan budget")
    monkeypatch.setattr(searchproc, "_iter_files", fail)
    out = io.StringIO()
    searchproc.run({"root": "/nonexistent", "pattern": "needle", **limits}, out)
    assert json.loads(out.getvalue().strip()) == {"done": True, "scanned": 0}

@pytest.mark.parametrize("seed", list(range(20)))
def test_iterative_walk_equivalent_to_oswalk_for_shallow_fuzz(tmp_path, seed):
    rng = random.Random(seed + 48291)
    root = tmp_path / "root"
    root.mkdir()
    dirs = [root]
    for i in range(30):
        parent = rng.choice(dirs)
        if rng.random() < 0.35:
            d = parent / f"d{i:02}"
            d.mkdir()
            dirs.append(d)
        else:
            (parent / f"f{i:02}.txt").write_text("needle")
    (root / "to_dir").symlink_to(rng.choice(dirs), target_is_directory=True)
    (root / "to_file").symlink_to(root / "f99_does_not_exist.txt")
    skip_names = {"d05", "d11", "d18"}
    skip_paths = {str(root / "d03"), str(root / "d08")}
    old = []
    for path, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(x for x in dirnames
                             if x not in skip_names and os.path.join(path, x) not in skip_paths)
        for name in sorted(filenames):
            old.append(os.path.join(path, name))
    new = [str(p) for p in searchproc._iter_files(root, skip_names, skip_paths)]
    assert new == old
