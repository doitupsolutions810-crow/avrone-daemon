"""One character class for "show this outside text on one line" (round 136).

Round 135 found nine sanitizers that each had their own copy of the class, and each one had missed
something. They all import this now. It covers every ``str.splitlines()`` break, C0 and C1
controls, DEL, and the Unicode bidi controls. Bidi controls are U+200E, U+200F, U+061C, U+202A to
U+202E and U+2066 to U+2069. A peer's node name holding U+202E could reverse the rest of its row in
a terminal or the admin UI ("Trojan Source"), so a node read as "online" when it was not. The
zero-width joiner and similar characters stay: emoji and many scripts need them.
"""
from __future__ import annotations

import re

UNSAFE_CLASS = "[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069]"
UNSAFE_RUN = re.compile(UNSAFE_CLASS + "+")
UNSAFE_ONE = re.compile(UNSAFE_CLASS)


def _esc(m: "re.Match[str]") -> str:
    c = ord(m.group())
    return f"\\x{c:02x}" if c < 0x100 else f"\\u{c:04x}"


def escape(s: object, keep: str = "") -> str:
    """Round 146: a name shown inside a listing (a file name, a process command line), with each
    unsafe character written as a visible escape (\\x0a, \\u202e). File names and argv are chosen
    by whoever created them: "a\\nFORGED" printed a line of its own in list_files, read_file's
    directory hint and proc_status, and U+202E reversed the rest of the row. Escaping, not
    blanking, keeps two names that differ only in a control character apart. Characters in `keep`
    (a tab in a matched source line) are left as they are."""
    if keep:
        return UNSAFE_ONE.sub(lambda m: m.group() if m.group() in keep else _esc(m), str(s))
    return UNSAFE_ONE.sub(_esc, str(s))
