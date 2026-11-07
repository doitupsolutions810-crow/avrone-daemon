"""Agent kit (v0.8): richer tooling plus better handling of tasks, resources and skills.

Tools
  image_inspect  size, mode, colors, entropy and an estimated module grid of an image
  image_decode   decode QR / DataMatrix / Aztec / PDF417 / 1D barcodes (zxing-cpp)
  skill_list     list skills (markdown playbooks in AVRONE_SKILLS_DIR, default ./skills)
  skill_load     load one skill's instructions into the agent's context
  env_install    install a package into the agent's environment (pip, or apt with sudo fallback);
                 every change is recorded in the resource ledger
  resources      the resource ledger: what each agent installed or changed, when, and the outcome
  task_board     shared per-run board: add / update / list subtasks with status and owner

Self-changes policy (AVRONE_SELF_CHANGES): allow (default) | log | deny.
  allow and log both execute and record; deny refuses env_install.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool, _safe_path, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext

PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,80}(==[A-Za-z0-9.*+\-]{1,40})?$")


def _ws(ctx: "AgentContext") -> Path:
    return Path(ctx.core.runner.workspace).resolve()


def _data(ctx: "AgentContext") -> Path:
    d = Path(ctx.core.settings.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    try:
        ctx.core.codex.emit(ctx.run_id, kind, ctx.name, **data)
    except Exception:  # noqa: BLE001
        pass


def _img_path(ctx: "AgentContext", p: str) -> Path:
    path = Path(p)
    if path.is_absolute() and path.exists() and os.getenv("AVRONE_ALLOW_ABS_READ", "1") == "1":
        return path
    return _safe_path(_ws(ctx), p)


# ---------------------------------------------------------------- images
MAX_IMAGE_PIXELS = 40_000_000     # inspect/decode refuse bigger images (a 48 KB PNG can be 400 Mpx)
MAX_IMAGE_BYTES = 64 * 1024 * 1024
DECODE_UPSCALE_MAX_PIXELS = 4_000_000  # the 2x pass only for small images (4x the pixels)


def _open_image(ctx: "AgentContext", raw: Any):
    """(path, PIL image) or an "ERROR: ..." string. Before: a FIFO or /dev/stdin path hung the event
    loop inside Image.open; a non-image, empty, truncated or directory path raised a raw exception;
    a 400 Mpx PNG of 48 KB raised DecompressionBombError, and one just under PIL's limit (~179 Mpx)
    was decoded at ~0.5 GB per copy, with the 2x decode pass at 4x that."""
    from PIL import Image
    if not isinstance(raw, str) or not raw.strip():
        return "ERROR: path is required (an image file in your workspace)"
    try:
        p = _img_path(ctx, raw)
    except ValueError as e:
        return f"ERROR: {e}"
    try:
        if not p.is_file():
            return f"ERROR: {raw!r} is not a regular file"
        size = p.stat().st_size
    except OSError as e:
        return f"ERROR: cannot read {raw!r}: {e.strerror or e}"
    if size == 0:
        return f"ERROR: {p.name} is empty"
    if size > MAX_IMAGE_BYTES:
        return f"ERROR: {p.name} is {size} bytes; cap is {MAX_IMAGE_BYTES}"
    try:
        im = Image.open(p)
        w, h = im.size
        if w <= 0 or h <= 0 or w * h > MAX_IMAGE_PIXELS:
            im.close()
            return f"ERROR: {p.name} is {w}x{h} ({w * h} px); cap is {MAX_IMAGE_PIXELS} px"
        im.load()
    except Image.DecompressionBombError:
        return f"ERROR: {p.name} declares too many pixels (decompression bomb)"
    except (OSError, ValueError, SyntaxError, EOFError) as e:  # UnidentifiedImageError is an OSError
        return f"ERROR: {p.name} is not a readable image ({type(e).__name__}: {str(e)[:120]})"
    return p, im


def _inspect(p: Path, im) -> str:
    g = im.convert("L")
    hist = g.histogram()
    total = sum(hist) or 1
    ent = -sum((h / total) * math.log2(h / total) for h in hist if h) + 0.0
    ent = abs(ent) if ent == 0 else ent  # a one-colour image printed "-0.000"
    colors = im.convert("RGB").getcolors(maxcolors=1 << 16)
    top = sorted(colors, reverse=True)[:6] if colors else []
    # estimate module size from run lengths along three rows (barcode-ish images)
    bw = g.point(lambda v: 255 if v > 127 else 0)
    w, h = bw.size
    runs: Counter[int] = Counter()
    for y in sorted({h // 3, h // 2, 2 * h // 3}):
        row = [bw.getpixel((x, y)) for x in range(w)]
        n = 1
        for a, b in zip(row, row[1:]):
            if a == b:
                n += 1
            else:
                runs[n] += 1
                n = 1
    unit = min((r for r, c in runs.most_common(8) if r >= 2), default=None)
    grid = f"~{round(w / unit)}x{round(h / unit)} modules (unit {unit}px)" if unit else "n/a"
    return (f"{p.name}: {w}x{h} {im.mode} format={im.format} size={p.stat().st_size}B\n"
            f"entropy={ent:.3f} bits/px  dark_ratio={sum(hist[:128]) / total:.3f}\n"
            f"top colors: {[(c, rgb) for c, rgb in top]}\nestimated grid: {grid}")


async def t_image_inspect(ctx: "AgentContext", args: dict[str, Any]) -> str:
    got = _open_image(ctx, (args or {}).get("path"))
    if isinstance(got, str):
        return got
    p, im = got
    try:  # off the event loop: a big image used to stall every other agent and the API for seconds
        return await asyncio.to_thread(_inspect, p, im)
    except (OSError, ValueError, MemoryError) as e:
        return f"ERROR: could not inspect {p.name}: {type(e).__name__}"
    finally:
        im.close()


def _decode(zxingcpp, p: Path, im) -> dict[tuple[str, str], str]:
    from PIL import Image
    g = im.convert("L")
    seen: dict[tuple[str, str], str] = {}
    for scale in (1.0, 0.5, 2.0, 0.25):
        if scale > 1 and g.width * g.height > DECODE_UPSCALE_MAX_PIXELS:
            continue
        s = g.resize((max(1, int(g.width * scale)), max(1, int(g.height * scale))), Image.NEAREST)
        for r in zxingcpp.read_barcodes(s, try_rotate=True, try_downscale=True):
            key = (str(r.format), r.text)
            if key not in seen and len(seen) < 200:
                pos = r.position
                extra = getattr(r, "extra", None) or {}
                ver = f" version={extra['Version']}" if isinstance(extra, dict) and extra.get("Version") else ""
                seen[key] = (f"- {r.format.name}{ver} valid={r.valid} ec={getattr(r, 'ec_level', '?')} "
                             f"scale={scale} at ({pos.top_left.x},{pos.top_left.y})-({pos.bottom_right.x},{pos.bottom_right.y})"
                             f"\n  text[{len(r.text)}]: {r.text[:4000]}")
    return seen


async def t_image_decode(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        import zxingcpp
    except ImportError:
        return ("ERROR: zxing-cpp not installed. It is a pip package (not apt): "
                "env_install {\"package\": \"zxing-cpp\", \"manager\": \"pip\"} then retry.")
    got = _open_image(ctx, (args or {}).get("path"))
    if isinstance(got, str):
        return got
    p, im = got
    try:
        seen = await asyncio.to_thread(_decode, zxingcpp, p, im)
    except (OSError, ValueError, MemoryError, RuntimeError) as e:
        return f"ERROR: could not decode {p.name}: {type(e).__name__}"
    finally:
        im.close()
    _emit(ctx, "image.decode", path=str(p), found=len(seen))
    return f"{len(seen)} symbol(s) in {p.name}\n" + "\n".join(seen.values()) if seen else f"no symbols decoded in {p.name}"


# ---------------------------------------------------------------- skills
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
SKILL_MAX_BYTES = 256_000
SKILL_LOAD_CHARS = 12000
MAX_SKILLS = 500


def _skills_dir(ctx: "AgentContext") -> Path:
    return Path(os.getenv("AVRONE_SKILLS_DIR", str(Path(__file__).resolve().parent.parent / "skills")))


def _valid_skill_name(name: str) -> bool:
    return bool(SKILL_NAME_RE.match(name)) and ".." not in name


def parse_skill(text: str) -> tuple[dict[str, str], str]:
    """Split a skill file into (frontmatter, body).

    Frontmatter is an optional leading block between two `---` lines holding simple
    `key: value` pairs. A missing or unterminated block yields {} and the whole text as body.
    """
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm: dict[str, str] = {}
            for line in lines[1:i]:
                if ":" not in line or line.lstrip().startswith("#"):
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1].strip()
                if k and k not in fm:
                    fm[k] = v
            return fm, "\n".join(lines[i + 1:])
    return {}, text  # unterminated frontmatter: treat as plain body


def _skill_desc(fm: dict[str, str], body: str) -> str:
    desc = fm.get("description", "").strip()
    if not desc:
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line and line != "---":
                desc = line
                break
    desc = " ".join(desc.split())
    return (desc[:200] if desc else "(no description)")


def _read_skill(f: Path) -> str | None:
    try:
        if not f.is_file() or f.stat().st_size > SKILL_MAX_BYTES:
            return None
        with f.open("rb") as fh:  # bounded: a file that grew after stat() used to be read whole
            data = fh.read(SKILL_MAX_BYTES + 1)
        if len(data) > SKILL_MAX_BYTES:
            return None
        return data.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _skills(ctx: "AgentContext") -> dict[str, tuple[str, Path]]:
    """name -> (description, path), sorted by name.

    Directory skills (<name>/SKILL.md) win over flat <name>.md files with the same name; the
    first one found wins and later duplicates are ignored. Files outside the skills dir (e.g.
    via symlinks), oversized or unreadable files, and unsafe names are skipped.
    """
    d = _skills_dir(ctx)
    found: dict[str, tuple[str, Path]] = {}
    if not d.is_dir():
        return found
    root = d.resolve()
    candidates = [(f.parent.name, f) for f in sorted(d.glob("*/SKILL.md"))] + \
                 [(f.stem, f) for f in sorted(d.glob("*.md"))]
    for name, f in candidates:
        if len(found) >= MAX_SKILLS:  # 100k files used to be read (and listed) on every call
            break
        if not _valid_skill_name(name) or name in found:
            continue
        try:
            if not f.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        text = _read_skill(f)
        if text is None:
            continue
        fm, body = parse_skill(text)
        found[name] = (_skill_desc(fm, body), f)
    return dict(sorted(found.items()))


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_skill_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    sk = _skills(ctx)
    return "\n".join(f"- {n}: {d}" for n, (d, _) in sk.items()) or "no skills installed"


def _names(sk: dict[str, Any], n: int = 50) -> str:
    names = list(sk)
    return ", ".join(names[:n]) + (f", ... (+{len(names) - n} more; skill_list)" if len(names) > n else "") or "none"


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_skill_load(ctx: "AgentContext", args: dict[str, Any]) -> str:
    name = str((args or {}).get("name", "")).strip()
    if not name:
        return 'ERROR: give {"name": str}; use skill_list to see available skills.'
    if not _valid_skill_name(name):
        return f"ERROR: invalid skill name {name!r} (letters, digits, '.', '_', '-' only; no paths)."
    sk = _skills(ctx)
    if name not in sk:
        return f"ERROR: no skill {name!r}. Available: {_names(sk)}"
    text = _read_skill(sk[name][1])
    if text is None:
        return f"ERROR: skill {name!r} could not be read."
    if not text.strip():
        return f"ERROR: skill {name!r} is empty."
    _emit(ctx, "skill.load", skill=name)
    return text[:SKILL_LOAD_CHARS]


# ---------------------------------------------------------------- resources
LEDGER_READ_MAX = 16 * 1024 * 1024  # resources.jsonl is read from its newest 16 MB
BOARD_READ_MAX = 8 * 1024 * 1024


def _ledger(ctx: "AgentContext") -> Path:
    return _data(ctx) / "resources.jsonl"


def record_change(ctx: "AgentContext", action: str, target: str, ok: bool, detail: str = "") -> None:
    entry = {"ts": time.time(), "run_id": ctx.run_id, "agent": ctx.name, "action": action, "target": target,
             "ok": ok, "detail": detail[:500]}
    from .safeio import open_append
    try:  # a symlink at resources.jsonl appended to its target; a FIFO blocked the tool forever
        with open_append(_ledger(ctx), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        entry["detail"] = (entry["detail"] + f" [ledger not written: {e}]")[:600]
    _emit(ctx, "env.change", **{k: v for k, v in entry.items() if k not in ("run_id", "agent")})


async def t_env_install(ctx: "AgentContext", args: dict[str, Any]) -> str:
    policy = os.getenv("AVRONE_SELF_CHANGES", "allow").lower()
    pkg, mgr = str(args.get("package") or "").strip(), str(args.get("manager", "pip")).lower()
    if not PKG_RE.match(pkg):
        return "ERROR: invalid package name"
    if mgr not in ("pip", "apt"):
        # checked BEFORE the lock file is opened: the manager is part of its name, and
        # manager="x/../../../../srv/app/poetry" opened (and truncated) <that>.lock outside the data dir
        return "ERROR: manager must be pip or apt"
    if policy == "deny":
        record_change(ctx, f"{mgr} install (denied)", pkg, False, "AVRONE_SELF_CHANGES=deny")
        return "ERROR: self-changes are disabled on this node (AVRONE_SELF_CHANGES=deny)"
    prior = _installed_before(ctx, mgr, pkg)
    if prior:
        return f"{pkg} already installed via {mgr} by {prior} (resource ledger); no change made"
    lock = _data(ctx) / f".install-{mgr}.lock"
    with open(lock, "w") as lf:  # one installer per package manager per node: no apt/dpkg lock fights
        import fcntl
        await asyncio.to_thread(fcntl.flock, lf, fcntl.LOCK_EX)
        try:
            prior = _installed_before(ctx, mgr, pkg)
            if prior:
                return f"{pkg} already installed via {mgr} by {prior} (resource ledger); no change made"
            return await _do_install(ctx, mgr, pkg)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _installed_before(ctx: "AgentContext", mgr: str, pkg: str) -> str | None:
    p = _ledger(ctx)
    if not p.exists():
        return None
    from .safeio import read_regular
    raw = read_regular(p, LEDGER_READ_MAX)
    for line in reversed((raw or b"").decode("utf-8", "replace").splitlines()):
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(r, dict):  # a "[1]" or "5" line used to raise on every later env_install
            continue
        if r.get("ok") and r.get("action") == f"{mgr} install" and r.get("target") == pkg:
            return r.get("agent", "another agent")
    return None


async def _do_install(ctx: "AgentContext", mgr: str, pkg: str) -> str:
    if mgr == "pip":
        cmd = f"python3 -m pip install -q {pkg} 2>&1 | tail -n 3"
    elif mgr == "apt":
        cmd = (f"(apt-get install -y -q {pkg} || sudo -n apt-get install -y -q {pkg} || "
               f"(sudo -n apt-get update -q && sudo -n apt-get install -y -q {pkg})) 2>&1 | tail -n 3")
    else:
        return "ERROR: manager must be pip or apt"
    res = await ctx.core.runner.run(cmd, timeout=600)
    check = await ctx.core.runner.run(
        f"python3 -m pip show {pkg.split('==')[0]} >/dev/null 2>&1" if mgr == "pip" else f"dpkg -s {pkg} >/dev/null 2>&1",
        timeout=30)
    ok = check.exit_code == 0
    record_change(ctx, f"{mgr} install", pkg, ok, (res.stdout or res.stderr)[-300:])
    return f"{'installed' if ok else 'FAILED'} {pkg} via {mgr} (logged to resource ledger)\n{res.text(1500)}"


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_resources(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _ledger(ctx)
    if not p.exists():
        return "resource ledger empty"
    # limit: "abc" raised, 0 meant every line ([-0:]), negatives sliced from the front
    try:
        limit = int(args.get("limit", 30))
    except (TypeError, ValueError, OverflowError):
        limit = 30
    limit = max(1, min(500, limit))
    rows = []
    from .safeio import read_regular
    for line in (read_regular(p, LEDGER_READ_MAX) or b"").decode("utf-8", "replace").splitlines():
        try:  # one torn or hand-edited line used to break the tool for good
            r = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(r, dict):
            rows.append(r)
    rows = rows[-limit:]

    def when(v: Any) -> str:
        try:
            return time.strftime("%H:%M:%S", time.localtime(float(v)))
        except (TypeError, ValueError, OverflowError, OSError):
            return "??:??:??"
    return "\n".join(f"{when(r.get('ts'))} {str(r.get('agent', '?'))[:80]} {str(r.get('action', '?'))[:80]} "
                     f"{str(r.get('target', '?'))[:120]} {'ok' if r.get('ok') else 'FAIL'}" for r in rows) \
        or "resource ledger empty"


# ---------------------------------------------------------------- task board
MAX_BOARD_TASKS = 500


def _board(ctx: "AgentContext") -> Path:
    d = _data(ctx) / "boards"
    d.mkdir(exist_ok=True)
    return d / f"{re.sub(r'[^A-Za-z0-9_-]', '', ctx.run_id) or 'run'}.json"


async def t_task_board(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _board(ctx)
    try:
        from .safeio import read_regular
        raw = read_regular(p, BOARD_READ_MAX)
        board: list[dict[str, Any]] = json.loads(raw) if raw else []
    except (OSError, ValueError):
        board = []  # a torn/corrupt board starts fresh instead of breaking the tool for the run
    if not isinstance(board, list):
        board = []
    board = [t for t in board if isinstance(t, dict) and isinstance(t.get("id"), int)]
    op = str(args.get("op", "list"))
    if op == "add":
        title = str(args.get("title") or "").strip()
        if not title:
            return "ERROR: op=add needs a title"
        if len(board) >= MAX_BOARD_TASKS:  # every op rewrites and lists the whole board
            return f"ERROR: the board is full ({MAX_BOARD_TASKS} tasks); update existing ones instead"
        item = {"id": max((t["id"] for t in board), default=0) + 1, "title": title[:200], "status": "todo",
                "owner": str(args.get("owner", ctx.name)), "note": "", "by": ctx.name}
        board.append(item)
    elif op == "update":
        try:
            tid = int(args.get("id"))
        except (TypeError, ValueError):
            return "ERROR: op=update needs an integer id"
        item = next((t for t in board if t["id"] == tid), None)
        if not item:
            return "ERROR: no such task"
        for k in ("status", "note", "owner"):
            if k in args:
                item[k] = str(args[k])[:1000]
    elif op != "list":
        return "ERROR: op must be add, update or list"
    if op != "list":
        from .safeio import atomic_write  # random temp name: a planted .board.<pid>.tmp link is never followed
        atomic_write(p, json.dumps(board, indent=1))  # atomic: a crash never leaves a half-written board
    if op != "list":
        _emit(ctx, "task.board", op=op, board=board[-10:])
    return "\n".join(f"#{t['id']} [{t.get('status', '?')}] {t.get('title', '')} (owner {t.get('owner', '?')})"
                     f"{' - ' + str(t['note']) if t.get('note') else ''}" for t in board) or "board empty"


KIT_TOOLS = [
    ("image_inspect", "inspect an image: size, mode, entropy, dominant colors, estimated barcode module grid.",
     '{"path": str}', t_image_inspect),
    ("image_decode", "decode every QR/DataMatrix/Aztec/PDF417/1D barcode in an image at several scales.",
     '{"path": str}', t_image_decode),
    ("skill_list", "list available skills (playbooks for common jobs).", "{}", t_skill_list),
    ("skill_load", "load a skill's step-by-step playbook before starting that kind of job.", '{"name": str}', t_skill_load),
    ("env_install", "install a missing package into your environment (pip or apt); logged to the resource ledger.",
     '{"package": str, "manager"?: "pip"|"apt"}', t_env_install),
    ("resources", "show the resource ledger: packages/changes agents made to their environments.",
     '{"limit"?: int}', t_resources),
    ("task_board", "shared board for this run: op=add {title, owner?} | update {id, status?, note?, owner?} | list.",
     '{"op": "add"|"update"|"list", "title"?: str, "id"?: int, "status"?: str, "note"?: str, "owner"?: str}',
     t_task_board),
]


KIT_SCHEMAS: dict[str, dict[str, dict[str, str]]] = {
    "image_inspect": {"required": {"path": "str"}},
    "image_decode": {"required": {"path": "str"}},
    "skill_load": {"required": {"name": "str"}},
    "env_install": {"required": {"package": "str"}, "optional": {"manager": "str"}},
    "resources": {},
    "task_board": {},  # op-dependent fields; the body validates them (ids may arrive as "1")
}


def register_kit_tools(registry) -> None:
    for name, desc, args_doc, fn in KIT_TOOLS:
        schema = KIT_SCHEMAS.get(name, {})
        registry.register(Tool(name, desc, args_doc, fn, source="builtin", schema=schema))
