"""Integer tool arguments, one rule for every tool (hardening round 45).

Tool arguments come from a model's JSON. Python's json accepts ``1e999`` (valid JSON) and the
``Infinity``/``NaN`` extensions, so a tool could receive ``inf``: ``int(inf)`` raised
OverflowError, which half a dozen ``except (TypeError, ValueError)`` blocks did not catch, and the
tool call failed instead of using its default. Strings went through ``int()``, which also takes
``"1_000"``, ``"\u0663"`` (Arabic-Indic three) and full-width digits, while ``str.isdigit()`` says
True for ``"\u00b2"`` (superscript two) that ``int()`` then refuses.

``tool_int`` accepts: an int (not bool); a finite float, truncated as before; ``+inf``/``-inf``
clamped to ``hi``/``lo`` when that bound exists; an ASCII decimal string with optional sign and
surrounding spaces, or an ASCII float string. Anything else is ``bad`` (the default unless the
caller passes ``bad=None`` to report an error).
"""
from __future__ import annotations

import math
import re
from typing import Any

_INT_TEXT = re.compile(r"[+-]?[0-9]{1,18}")
_FLOAT_TEXT = re.compile(r"[+-]?(?:[0-9]{1,18}(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?")
_UNSET: Any = object()


def ascii_int(text: str) -> int | None:
    """A plain ASCII decimal integer (sign and surrounding ASCII spaces allowed), else None."""
    if not isinstance(text, str):
        return None
    t = text.strip(" \t")
    return int(t) if _INT_TEXT.fullmatch(t) else None


def tool_int(v: Any, default: int, lo: int | None = None, hi: int | None = None, bad: Any = _UNSET) -> Any:
    if bad is _UNSET:
        bad = default
    if v is None or (isinstance(v, str) and not v.strip()):
        n = default
    elif isinstance(v, bool):
        return bad
    elif isinstance(v, int):
        n = v
    elif isinstance(v, float):
        n = _from_float(v, lo, hi)
        if n is None:
            return bad
    elif isinstance(v, str):
        n = ascii_int(v)
        if n is None:
            t = v.strip(" \t")
            if not _FLOAT_TEXT.fullmatch(t):
                return bad
            n = _from_float(float(t), lo, hi)
            if n is None:
                return bad
    else:
        return bad
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _from_float(f: float, lo: int | None, hi: int | None) -> int | None:
    if math.isnan(f):
        return None
    if math.isinf(f):
        b = hi if f > 0 else lo
        return b  # None when unbounded on that side
    return int(f)
