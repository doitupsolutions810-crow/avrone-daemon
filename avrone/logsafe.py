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
from typing import Any

from .textsafe import UNSAFE_ONE, escape

_installed = False


def _clean(a: Any) -> Any:
    if isinstance(a, BaseException):
        a = f"{a}"
    if isinstance(a, str) and UNSAFE_ONE.search(a):
        return escape(a)
    return a


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
                if rec.args:
                    rec.args = clean_args(rec.args)
                elif isinstance(rec.msg, str) and UNSAFE_ONE.search(rec.msg.rstrip("\n")):
                    # a message built with an f-string: no template to keep, so all of it is escaped
                    rec.msg = escape(rec.msg)
        except Exception:  # noqa: BLE001  logging must never raise from here
            pass
        return rec

    logging.setLogRecordFactory(factory)
    _installed = True
