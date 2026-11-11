"""Round 150: log arguments from outside can't forge log lines.

Many log calls put text from outside into a message: an MCP server's name, protocol version, tool
names and stderr lines, a fleet peer's error text, a plugin's exception, a checkpoint or queue file
name. A "\\n" in one of them wrote a line of its own into the server log, which read as another
event ("... WARNING fleet: node xyz trusted"). "\\r", ESC sequences and U+202E reached the operator's
terminal through the stderr handler.

Only avrone.* records change, and only their string or exception arguments, which are escaped with
textsafe.escape. The message templates stay as written, so a template's own line breaks stay.
Tracebacks (exc_info) are formatted by the handler and are not touched. Install once with
install(); it chains whatever record factory was already set.
"""
from __future__ import annotations

import logging
import numbers
import os
import re
from typing import Any

from .textsafe import UNSAFE_ONE, escape

_installed = False

# Hardening round 42: a template keeps its own "\n" and "\t", but nothing else in the unsafe class.
# log.warning(f"peer {name}: %s", err) put outside text into the TEMPLATE, where ESC, "\r" and U+202E
# went straight to the operator's terminal because only args were escaped once args were present.
_TEMPLATE_KEEP = "\n\t"
_NUMBERS = (numbers.Number,)  # Decimal, Fraction, numpy scalars: "%d" must keep working
DEFAULT_ARG_CHARS, MIN_ARG_CHARS, HARD_ARG_CHARS = 65536, 256, 4 * 1024 * 1024
TRUNC = "...[log arg truncated: {n} chars]"


def arg_chars() -> int:
    """AVRONE_LOG_ARG_MAX_CHARS (default 65536, clamped 256..4 MiB): the most of one argument a log
    line keeps. An MCP server writing a 50 MB stderr line put all of it into one server-log line and
    ran every redaction pattern over it."""
    try:
        n = int(str(os.environ.get("AVRONE_LOG_ARG_MAX_CHARS", "")).strip() or DEFAULT_ARG_CHARS)
    except (TypeError, ValueError):
        n = DEFAULT_ARG_CHARS
    return max(MIN_ARG_CHARS, min(HARD_ARG_CHARS, n))


def _cap(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + TRUNC.format(n=len(s))


def _safe_str(obj: Any, fn: Any = str) -> str:
    try:
        out = fn(obj)
        return out if isinstance(out, str) else str(out)
    except Exception as e:  # noqa: BLE001  an object whose __str__ raises must not stop the whole record
        return f"<unprintable {type(obj).__name__}: {type(e).__name__}>"


def _scrub(s: str) -> str:
    lim, total = arg_chars(), len(s)
    s = _redact(s[: lim * 4] if total > lim * 4 else s)  # bound the redaction work too
    if total > lim or len(s) > lim:
        s = s[:lim] + TRUNC.format(n=total)
    return escape(s) if UNSAFE_ONE.search(s) else s


class _Shown:
    """Stands in for a non-string argument (a Path, an object, a list holding outside text): both
    "%s" and "%r" render it cleaned. Path("ckpt\nWARNING avrone.fleet: trusted") used to pass
    through untouched because only str and exception arguments were cleaned."""

    __slots__ = ("_s", "_r")

    def __init__(self, obj: Any) -> None:
        self._s = _scrub(_safe_str(obj, str))
        self._r = _scrub(_safe_str(obj, repr))

    def __str__(self) -> str:
        return self._s

    def __repr__(self) -> str:
        return self._r

    def __format__(self, spec: str) -> str:
        return format(self._s, spec)


def _redact(s: str) -> str:
    """Round 154: secrets are masked too. MCP stderr ("using token ghp_..."), a peer's error body
    or an exception quoting a URL with credentials went to the server log verbatim, which is the
    one sink CodeX redaction never covered."""
    try:
        from .ctxspawn import redact
        return redact(s)
    except Exception:  # noqa: BLE001  (import cycle at startup, or anything else: keep the text)
        return s


def _clean(a: Any) -> Any:
    try:
        if a is None or isinstance(a, (bool, *_NUMBERS)):
            return a  # %d / %.2f must keep working
        if isinstance(a, BaseException):
            return _scrub(_safe_str(a))
        if isinstance(a, str):
            return _scrub(a)
        if isinstance(a, (bytes, bytearray)):
            return a  # "%s" renders their repr, which already escapes every control byte
        return _Shown(a)
    except Exception:  # noqa: BLE001
        return "<unloggable argument>"


def clean_template(msg: str) -> str:
    """The template keeps "\n" and "\t"; any other unsafe character is escaped (round 42)."""
    if UNSAFE_ONE.search(msg):
        return escape(msg, keep=_TEMPLATE_KEEP)
    return msg


def clean_args(args: Any) -> Any:
    if isinstance(args, tuple):
        return tuple(_clean(a) for a in args)
    if isinstance(args, dict):
        return {k: _clean(v) for k, v in args.items()}
    return args


def install() -> None:
    global _installed
    if _installed:
        return
    old = logging.getLogRecordFactory()

    def factory(*a: Any, **kw: Any) -> logging.LogRecord:
        rec = old(*a, **kw)
        try:
            if rec.name == "avrone" or rec.name.startswith("avrone."):
                if not isinstance(rec.msg, str):
                    # log.warning(exc) / log.info(obj): getMessage() calls str(msg), unescaped before
                    # round 42. Templates are only ever str, so a non-str msg is all outside text.
                    if rec.args:
                        rec.msg = _safe_str(rec.msg)
                    else:
                        rec.msg = _scrub(_safe_str(rec.msg))
                if rec.args:
                    rec.args = clean_args(rec.args)
                    if isinstance(rec.msg, str):
                        rec.msg = clean_template(rec.msg)
                elif isinstance(rec.msg, str):
                    rec.msg = _redact(rec.msg)  # round 154
                    if UNSAFE_ONE.search(rec.msg.rstrip("\n")):
                        # a message built with an f-string: no template to keep, so all of it is escaped
                        rec.msg = escape(rec.msg)
        except Exception:  # noqa: BLE001  logging must never raise from here
            pass
        return rec

    logging.setLogRecordFactory(factory)
    _installed = True
