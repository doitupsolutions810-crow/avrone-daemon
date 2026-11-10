"""Round 136: one shared unsafe-character class, now covering Unicode bidi controls.

v0.128: nine modules each had their own copy of the one-line class (round 135 had to fix all nine),
and none removed bidi controls, so a peer's node name holding U+202E ("Trojan Source") could reverse
the rest of its fleet_nodes/fleet_agents row on screen.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from tests.test_round135_c1_breaks import FUNCS

BIDI = ["\u200e", "\u200f", "\u061c", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069"]


def test_textsafe_module():
    try:
        from avrone import textsafe
    except ImportError:
        pytest.fail("no avrone.textsafe")
    for ch in BIDI + ["\n", "\x85", "\u2028", "\x00", "\x9f"]:
        assert textsafe.UNSAFE_ONE.fullmatch(ch)
    for ch in ["a", " ", "\u200d", "\ufe0f", "é", "运"]:  # ZWJ / variation selector kept for emoji
        assert not textsafe.UNSAFE_ONE.fullmatch(ch)


@pytest.mark.parametrize("name", sorted(FUNCS))
@pytest.mark.parametrize("ch", BIDI, ids=lambda c: f"U+{ord(c):04X}")
def test_bidi_removed(name, ch):
    out = FUNCS[name](f"node{ch}enilffo [offline]")
    assert ch not in out, (name, out)


@pytest.mark.parametrize("name", sorted(FUNCS))
def test_emoji_zwj_kept(name):
    s = "team \U0001F468\u200d\U0001F469\u200d\U0001F467 ok"
    assert FUNCS[name](s) == s


def test_no_private_copies():
    root = pathlib.Path(__file__).resolve().parents[1] / "avrone"
    pat = re.compile(r"\\x00-\\x1f")
    hits = [p.name for p in root.glob("*.py") if p.name != "textsafe.py" and pat.search(p.read_text())]
    assert not hits, f"use avrone.textsafe instead of a private control-char class: {hits}"
