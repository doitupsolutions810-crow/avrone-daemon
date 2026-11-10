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
