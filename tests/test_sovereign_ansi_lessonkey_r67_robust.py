"""Hardening round 67: clean_output strips every ECMA-48 escape (charset designations, DECSC/DECRC,
keypad modes, 8-bit CSI, DCS/APC payloads, a lone or cut-off ESC) without eating real output, and
lesson_recall redacts lesson keys."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from avrone import bridge as b
from avrone.sovereign import CommandResult, clean_output
from avrone.tools_lessonrecall import t_lesson_recall


@pytest.mark.parametrize("raw,want", [
    ("\x1b(B\x1b[mreset", "reset"),                       # tput sgr0
    ("\x1b)0line", "line"),
    ("\x1b#8align", "align"),
    ("\x1b%Gutf", "utf"),
    ("\x1b7save\x1b8", "save"),                            # DECSC / DECRC
    ("\x1b=keypad\x1b>", "keypad"),
    ("\x1bcreset-all", "reset-all"),                       # RIS
    ("\x9b31mred\x9b0m", "red"),                           # 8-bit CSI
    ("\x1b[?25lhide\x1b[?25h", "hide"),
    ("\x1b[1;31mERR\x1b[0m: x", "ERR: x"),
    ("\x1b]0;title\x07x", "x"),
    ("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\", "link"),
    ("\x9d0;title\x9cx", "x"),
    ("\x1bPq#0;2;0;0;0#0~~@@\x1b\\sixel", "sixel"),       # DCS payload dropped whole
    ("\x1b_tmux;payload\x1b\\ok", "ok"),                  # APC
    ("\x1b^pm\x1b\\ok", "ok"),
    ("\x1bXsos\x1b\\ok", "ok"),
    ("\x90dcs8\x9cok", "ok"),
    ("a\x1b", "a"),
    ("a\x1b(", "a"),
    ("x\x1b\ny", "x\ny"),
    ("\u00e9\x1b\u00e9", "\u00e9\u00e9"),
])
def test_escapes_stripped(raw, want):
    assert clean_output(raw) == want


@pytest.mark.parametrize("raw", [
    "\x1bP unterminated dcs\nERROR: real failure",
    "\x1b]0;unterminated osc\nERROR: real failure",
    "\x1b_apc never closed\nERROR: real failure",
])
def test_unterminated_string_never_eats_next_line(raw):
    assert clean_output(raw) == "\nERROR: real failure"


@pytest.mark.parametrize("plain", [
    "plain [1m text", "tab\there", "50% done", "a ~ b ^ c _ d", "C:\\path\\x", "json {\"a\": [1, 2]}",
    "emoji \U0001f600 ok", "\u2603 snow",
])
def test_plain_text_untouched(plain):
    assert clean_output(plain) == plain


def test_no_esc_left_in_fuzz():
    import random
    rnd = random.Random(67)
    alphabet = ["\x1b", "[", "]", "(", ")", "#", "%", "P", "_", "^", "X", "\\", "\x07", "\x9b", "\x9c",
                "\x9d", "\x90", "7", "8", "=", ">", "m", "0", ";", "?", "a", "\n", " ", "\r"]
    for _ in range(3000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 24)))
        out = clean_output(s)
        assert "\x1b" not in out and "\x9b" not in out, repr(s)


def test_progress_bar_still_collapsed():
    assert clean_output("\x1b[32m10%\x1b[0m\r\x1b[32m100%\x1b[0m\ndone") == "100%\ndone"


def test_command_result_text_has_no_esc():
    r = CommandResult("tput sgr0", 0, "\x1b(B\x1b[mhello\x1b7", "\x1b=warn\x1b>")
    out = r.text()
    assert "\x1b" not in out and "hello" in out and "warn" in out


def test_clean_output_empty_and_none_like():
    assert clean_output("") == ""


# ------------------------------------------------------------ lesson_recall key redaction
def _ctx_with(lessons):
    d = Path(tempfile.mkdtemp())
    st = b._store(d)
    for k, v in lessons:
        st.save(b.LESSONS_NS, k, v)
    return S(core=S(settings=S(data_dir=d)))


@pytest.fixture(autouse=True)
def _lessons_on(monkeypatch):
    monkeypatch.setattr(b, "lessons_enabled", lambda: True)


def test_token_shaped_key_redacted():
    tok = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    out = asyncio.run(t_lesson_recall(_ctx_with([(tok, "a note")]), {}))
    assert tok not in out and "a note" in out


def test_keyword_key_redacted():
    out = asyncio.run(t_lesson_recall(_ctx_with([("password=hunter2xyz", "a note")]), {}))
    assert "hunter2xyz" not in out


def test_plain_key_shown():
    out = asyncio.run(t_lesson_recall(_ctx_with([("lesson-deploy-1", "a note")]), {}))
    assert "lesson-deploy-1" in out


def test_key_bidi_and_newline_flattened():
    out = asyncio.run(t_lesson_recall(_ctx_with([("k\u202eevil", "x"), ("k2\nforged: line", "y")]), {}))
    assert "\u202e" not in out
    assert all(ln.startswith(("- ", "lesson_recall")) for ln in out.split("\n"))
