"""Regression: a lone surrogate in a docstring ("r\\ud800" written without escaping the
backslash) made `import avrone.bridge` raise UnicodeEncodeError on Python 3.13+, whose
compiler dedents docstrings, so 141 test modules failed to collect. No string constant in
the package may contain a lone surrogate; tests that need one build it at runtime."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "avrone"


def _lone_surrogates(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in node.value):
                yield node.lineno


def test_no_lone_surrogates_in_package_string_constants():
    bad = [f"{p.relative_to(PKG.parent)}:{line}" for p in sorted(PKG.rglob("*.py")) for line in _lone_surrogates(p)]
    assert not bad, f"lone surrogate in string constant (escape the backslash): {bad}"


def test_bridge_imports_and_documents_the_example_literally():
    import avrone.bridge as bridge
    assert "\\ud800" in bridge.safe_run_id.__doc__
