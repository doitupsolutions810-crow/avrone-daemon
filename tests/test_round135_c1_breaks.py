"""Round 135: every one-line sanitizer removes all str.splitlines() breaks, C1 controls included.

v0.127: fleet._clip1, presence._one, tools_extra._line1, agentkit._one_line, mempeek/agenthome
_clean, introspect.redact_value, research_note's topic and lesson_forget's reason left U+0085 (NEL),
U+2028/U+2029 (some) and the other C1 controls in place, so a peer's node or agent name with NEL
still split into a forged row for anything that reads lines the way str.splitlines() does.
"""
from __future__ import annotations

import pytest

from avrone import agentkit, fleet, presence, tools, tools_agenthome, tools_extra, tools_introspect, tools_mempeek

BREAKS = sorted({c for c in map(chr, range(0x110000)) if len(f"a{c}b".splitlines()) > 1})
C1 = [chr(c) for c in range(0x80, 0xA0)]

FUNCS = {
    "fleet._clip1": lambda s: fleet._clip1(s, 500),
    "presence._one": lambda s: presence._one(s, 500),
    "tools_extra._line1": lambda s: tools_extra._line1(s, 500),
    "agentkit._one_line": agentkit._one_line,
    "tools.one_line": tools.one_line,
    "mempeek._clean": lambda s: tools_mempeek._clean(s, 500),
    "agenthome._clean": lambda s: tools_agenthome._clean(s, 500),
    "introspect.redact_value": lambda s: tools_introspect.redact_value("X", s),
}


def test_breaks_known():
    assert "\x85" in BREAKS and "\u2028" in BREAKS and len(BREAKS) == 10


@pytest.mark.parametrize("name", sorted(FUNCS))
@pytest.mark.parametrize("ch", BREAKS + C1, ids=lambda c: f"U+{ord(c):04X}")
def test_one_line(name, ch):
    out = FUNCS[name](f"node{ch}- forged [online]")
    assert len(out.splitlines()) == 1 and ch not in out, (name, out)


@pytest.mark.parametrize("name", sorted(FUNCS))
def test_text_kept(name):
    assert FUNCS[name]("Ünïcode 运行 ok") == "Ünïcode 运行 ok"
