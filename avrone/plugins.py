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


def plugins_dir() -> Path:
    return Path(os.getenv("AVRONE_PLUGINS_DIR", "./plugins")).resolve()


def _run_register(name: str, source: str, register: Callable[..., Any],
                  registry: ToolRegistry, settings: Any) -> dict[str, Any]:
    before = {n: registry.get(n) for n in registry.names()}
    try:
        register(registry, settings)
        error = None
    except (Exception, SystemExit) as e:  # sys.exit() inside register() used to take the node down
        error = f"{type(e).__name__}: {e}"
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


def _load_file(path: Path) -> Any:
    target = path / "__init__.py" if path.is_dir() else path
    mod_name = f"avrone_plugin_{path.stem}"
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
        candidates = sorted(
            p for p in d.iterdir()
            if not p.name.startswith(("_", "."))
            and ((p.is_file() and p.suffix == ".py") or (p.is_dir() and (p / "__init__.py").exists())))
        for p in candidates:
            name = p.stem
            try:
                module = _load_file(p)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise AttributeError("plugin has no register(registry, settings) function")
            except (Exception, SystemExit) as e:  # a top-level sys.exit() must not kill the node
                log.warning("plugin %s failed to import: %s", name, e)
                reports.append({"name": name, "source": str(p), "tools": [], "error": f"{type(e).__name__}: {e}"})
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
            except (Exception, SystemExit) as e:
                reports.append({"name": ep.name, "source": src, "tools": [], "error": f"{type(e).__name__}: {e}"})
                continue
            reports.append(_run_register(ep.name, src, register, registry, settings))

    for r in reports:
        if r["error"]:
            log.warning("plugin %s: %s", r["name"], r["error"])
        else:
            log.info("plugin %s loaded: %s", r["name"], ", ".join(r["tools"]) or "(no tools)")
    return reports
