"""Regression: a lone surrogate in a docstring ("r\\ud800" written without escaping the
backslash) made `import avrone.bridge` raise UnicodeEncodeError on Python 3.13+, whose
compiler dedents docstrings, so 141 test modules failed to collect. No docstring in the
package may contain a lone surrogate. (Regex character classes such as "[\\ud800-\\udfff]"
are ordinary constants, compile fine, and are intentionally allowed.)"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "avrone"


def _lone_surrogates(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                if any(0xD800 <= ord(ch) <= 0xDFFF for ch in body[0].value.value):
                    yield body[0].lineno


def test_no_lone_surrogates_in_package_docstrings():
    bad = [f"{p.relative_to(PKG.parent)}:{line}" for p in sorted(PKG.rglob("*.py")) for line in _lone_surrogates(p)]
    assert not bad, f"lone surrogate in a docstring (escape the backslash): {bad}"


def test_bridge_imports_and_documents_the_example_literally():
    import avrone.bridge as bridge
    assert "\\ud800" in bridge.safe_run_id.__doc__
