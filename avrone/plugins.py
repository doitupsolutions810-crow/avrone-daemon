"""Plugin system.

A plugin is either
  * a ``*.py`` file (or a package dir with ``__init__.py``) in the plugins dir
    (env AVRONE_PLUGINS_DIR, default ./plugins), or
  * an installed package exposing an entry point in the group ``avrone.plugins``.

Each plugin defines::

    def register(registry, settings):
        registry.register(Tool("my_tool", "what it does", '{"x": int}', fn))

Tools a plugin adds without setting ``source`` are tagged ``plugin:<name>``.
A failing plugin is reported, never fatal.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from .tools import ToolRegistry

log = logging.getLogger("avrone.plugins")
ENTRY_POINT_GROUP = "avrone.plugins"
ERROR_MAX = 500  # round 55: a plugin's exception text is clipped in its report


def _err(e: BaseException) -> str:
    """Round 55: the report carried str(e) whole (a plugin raising a 10 MB message put it in
    /api/plugins and every log line), with any newline or bidi control in it raw."""
    from .textsafe import escape
    try:
        msg = str(e)
    except Exception:  # noqa: BLE001 - a __str__ that raises
        msg = "<unprintable>"
    return escape(f"{type(e).__name__}: {msg}"[:ERROR_MAX])


def _fatal(e: BaseException) -> bool:
    """Interrupts propagate; every other BaseException a plugin raises is its failure, not the node's
    (round 55: a custom BaseException subclass escaped `except (Exception, SystemExit)`)."""
    import asyncio
    return isinstance(e, (KeyboardInterrupt, asyncio.CancelledError))


def plugins_dir() -> Path:
    return Path(os.getenv("AVRONE_PLUGINS_DIR", "./plugins")).resolve()


def _run_register(name: str, source: str, register: Callable[..., Any],
                  registry: ToolRegistry, settings: Any) -> dict[str, Any]:
    before = {n: registry.get(n) for n in registry.names()}
    try:
        register(registry, settings)
        error = None
    except BaseException as e:  # sys.exit() inside register() used to take the node down
        if _fatal(e):
            raise
        error = _err(e)
        log.warning("plugin %s register() failed: %s", name, error, exc_info=True)  # round 150: traceback via exc_info
    # A plugin may ADD tools, never silently REPLACE one that was already there (a builtin such as
    # shell/read_file, or another plugin's): the replacement kept source "builtin", so nothing in
    # /api/tools, capgate receipts or the codex showed that the tool now ran third-party code.
    replaced = sorted(n for n, t in before.items() if registry.get(n) is not t)
    for n in replaced:
        registry.register(before[n])  # put the original back (also undoes an unregister)
    added = sorted(set(registry.names()) - set(before))
    # tools added before a register() failure are kept (documented, tests/test_plugins_edges.py)
    if replaced:
        msg = f"refused to replace existing tool(s): {', '.join(replaced)}"
        error = f"{error}; {msg}" if error else msg
    for n in added:
        t = registry.get(n)
        if t is not None and t.source == "builtin":
            t.source = f"plugin:{name}"
    return {"name": name, "source": source, "tools": added, "error": error}


def _plugin_name(path: Path) -> str:
    # round 55: a package dir "x.y" was named "x" (Path.stem), clashing with plugin x.py
    return path.name if path.is_dir() else path.stem


def _module_name(name: str) -> str:
    """Round 55: "avrone_plugin_<stem>" with a dotted or dashed stem ("my.plugin.py") is a submodule
    path of a package that does not exist, and foo.py beside a foo/ package shared one sys.modules
    slot, so the second silently replaced the first. Names are made identifiers and kept unique."""
    import re
    base = "avrone_plugin_" + (re.sub(r"\W", "_", name, flags=re.ASCII) or "_")
    mod, i = base, 2
    while mod in sys.modules:
        mod, i = f"{base}_{i}", i + 1
    return mod


def _load_file(path: Path) -> Any:
    target = path / "__init__.py" if path.is_dir() else path
    mod_name = _module_name(_plugin_name(path))
    spec = importlib.util.spec_from_file_location(
        mod_name, target, submodule_search_locations=[str(path)] if path.is_dir() else None)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


def load_plugins(registry: ToolRegistry, settings: Any = None,
                 directory: str | os.PathLike | None = None,
                 include_entry_points: bool = True) -> list[dict[str, Any]]:
    """Load every plugin; return one report per plugin: {name, source, tools, error}."""
    reports: list[dict[str, Any]] = []
    d = Path(directory).resolve() if directory else plugins_dir()
    if d.is_dir():
        try:  # round 55: an unreadable plugins dir raised PermissionError out of node startup
            entries = list(d.iterdir())
        except OSError as e:
            entries = []
            reports.append({"name": "(plugins dir)", "source": str(d), "tools": [], "error": _err(e)})

        def _ok(p: Path) -> bool:
            try:
                return not p.name.startswith(("_", ".")) and (
                    (p.is_file() and p.suffix == ".py") or (p.is_dir() and (p / "__init__.py").is_file()))
            except OSError:
                return False
        candidates = sorted(p for p in entries if _ok(p))
        for p in candidates:
            name = _plugin_name(p)
            try:
                module = _load_file(p)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise AttributeError("plugin has no register(registry, settings) function")
            except BaseException as e:  # a top-level sys.exit() must not kill the node
                if _fatal(e):
                    raise
                log.warning("plugin %s failed to import: %s", name, _err(e))
                reports.append({"name": name, "source": str(p), "tools": [], "error": _err(e)})
                continue
            reports.append(_run_register(name, str(p), register, registry, settings))

    if include_entry_points:
        try:
            eps = list(entry_points(group=ENTRY_POINT_GROUP))
        except Exception as e:  # pragma: no cover
            log.warning("could not read entry points: %s", e)
            eps = []
        for ep in eps:
            src = f"entrypoint:{ep.value}"
            try:
                obj = ep.load()
                register = obj if callable(obj) and not hasattr(obj, "register") else getattr(obj, "register", None)
                if not callable(register):
                    raise AttributeError("entry point has no register(registry, settings)")
            except BaseException as e:
                if _fatal(e):
                    raise
                reports.append({"name": ep.name, "source": src, "tools": [], "error": _err(e)})
                continue
            reports.append(_run_register(ep.name, src, register, registry, settings))

    for r in reports:
        if r["error"]:
            log.warning("plugin %s: %s", r["name"], r["error"])
        else:
            log.info("plugin %s loaded: %s", r["name"], ", ".join(r["tools"]) or "(no tools)")
    return reports
