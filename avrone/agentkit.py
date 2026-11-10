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
import contextlib
import threading
import hashlib
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
    from .safeio import NotRegularFile, open_read
    try:
        # round 98: one fd, opened O_NONBLOCK and checked regular by fstat. is_file() then
        # Image.open(path) let a FIFO swapped in between hang the worker, and a file grown past
        # the cap after stat() was decoded anyway.
        with open_read(p, follow=True) as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > MAX_IMAGE_BYTES:
                return f"ERROR: {p.name} is {size} bytes; cap is {MAX_IMAGE_BYTES}"
            data = fh.read(MAX_IMAGE_BYTES + 1)
    except (NotRegularFile, IsADirectoryError):
        return f"ERROR: {raw!r} is not a regular file"
    except FileNotFoundError:
        return f"ERROR: {raw!r} is not a regular file"
    except OSError as e:
        return f"ERROR: cannot read {raw!r}: {e.strerror or e}"
    if not data:
        return f"ERROR: {p.name} is empty"
    if len(data) > MAX_IMAGE_BYTES:
        return f"ERROR: {p.name} grew past {MAX_IMAGE_BYTES} bytes while being read"
    try:
        import io
        im = Image.open(io.BytesIO(data))
        im.info["avrone_bytes"] = len(data)
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
    return (f"{p.name}: {w}x{h} {im.mode} format={im.format} size={im.info.get('avrone_bytes', 0)}B\n"
            f"entropy={ent:.3f} bits/px  dark_ratio={sum(hist[:128]) / total:.3f}\n"
            f"top colors: {[(c, rgb) for c, rgb in top]}\nestimated grid: {grid}")


async def t_image_inspect(ctx: "AgentContext", args: dict[str, Any]) -> str:
    # round 98: the full decode (im.load, up to MAX_IMAGE_PIXELS) ran on the event loop
    got = await asyncio.to_thread(_open_image, ctx, (args or {}).get("path"))
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
    # round 98: the full decode (im.load, up to MAX_IMAGE_PIXELS) ran on the event loop
    got = await asyncio.to_thread(_open_image, ctx, (args or {}).get("path"))
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


SKILL_HEAD_BYTES = 16_384     # skill_list reads this much of each skill for its description
SKILLS_SCAN_MAX = 20_000      # directory entries looked at per listing (a 50k-file dir was sorted whole)


def _read_skill(f: Path, head: int | None = None) -> str | None:
    """A skill's text, or None when it is missing, special, oversized or unreadable. One fd,
    O_NONBLOCK and fstat-checked (round 99): is_file() then open() let a FIFO swapped in between
    block the tool forever. With head, only the first `head` bytes are read (the size cap still
    applies to the whole file)."""
    from .safeio import open_read
    try:
        with open_read(f, follow=True) as fh:
            if os.fstat(fh.fileno()).st_size > SKILL_MAX_BYTES:
                return None
            data = fh.read(SKILL_MAX_BYTES + 1 if head is None else min(head, SKILL_MAX_BYTES + 1))
        if len(data) > SKILL_MAX_BYTES:  # grew after fstat
            return None
        return data.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _skill_files(d: Path) -> list[tuple[str, Path]]:
    """(name, path) candidates: <name>/SKILL.md in name order, then <name>.md in name order, from at
    most SKILLS_SCAN_MAX directory entries (was sorted(glob) of the whole directory every call)."""
    dirs: list[str] = []
    flats: list[str] = []
    try:
        with os.scandir(d) as it:
            for n, e in enumerate(it):
                if n >= SKILLS_SCAN_MAX:
                    break
                if e.name.startswith("."):
                    continue
                if e.name.endswith(".md"):
                    flats.append(e.name)
                else:
                    try:
                        if e.is_dir():  # follows a link: the resolve() check below confines it
                            dirs.append(e.name)
                    except OSError:
                        continue
    except OSError:
        return []
    dirs.sort()
    flats.sort()
    out = [(n, d / n / "SKILL.md") for n in dirs]
    return out + [(n[:-3], d / n) for n in flats]


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
    candidates = _skill_files(d)
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
        text = _read_skill(f, SKILL_HEAD_BYTES)  # was every skill read whole (500 x 256 KB per call)
        if text is None:
            continue
        fm, body = parse_skill(text)
        desc = _skill_desc(fm, body)
        if len(text.encode("utf-8", "surrogatepass")) >= SKILL_HEAD_BYTES and (
                desc == "(no description)" or (not fm and text.lstrip("\ufeff").startswith("---"))):
            full = _read_skill(f)  # description or front matter past the head: read it all
            if full is None:
                continue
            fm, body = parse_skill(full)
            desc = _skill_desc(fm, body)
        found[name] = (desc, f)
    return dict(sorted(found.items()))


def _skill_path(ctx: "AgentContext", name: str) -> Path | None:
    """The file _skills() would pick for `name`: <name>/SKILL.md over <name>.md, confined to the
    skills dir, readable and within the size cap; None otherwise."""
    d = _skills_dir(ctx)
    try:
        root = d.resolve()
    except OSError:
        return None
    for f in (d / name / "SKILL.md", d / f"{name}.md"):
        try:
            if not f.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        if _read_skill(f, 1) is not None:
            return f
    return None


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
    path = _skill_path(ctx, name)  # round 99: was every skill read to find one (3.6 s at 500 x 200 KB)
    if path is None:
        return f"ERROR: no skill {name!r}. Available: {_names(_skills(ctx))}"
    text = _read_skill(path)
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
        await asyncio.to_thread(record_change, ctx, f"{mgr} install (denied)", pkg, False, "AVRONE_SELF_CHANGES=deny")
        return "ERROR: self-changes are disabled on this node (AVRONE_SELF_CHANGES=deny)"
    # round 101: the ledger scan (up to 16 MB parsed) and the lock open ran on the event loop
    prior = await asyncio.to_thread(_installed_before, ctx, mgr, pkg)
    if prior:
        return f"{pkg} already installed via {mgr} by {prior} (resource ledger); no change made"
    try:
        lf = await _install_lock(_data(ctx) / f".install-{mgr}.lock")
    except TimeoutError:
        return f"ERROR: another {mgr} install has held the install lock for over {INSTALL_LOCK_WAIT_S:.0f}s; try again later"
    except OSError as e:
        return f"ERROR: install lock unusable ({e.__class__.__name__}: {e}); remove .install-{mgr}.lock from the data dir"
    with lf:  # one installer per package manager per node: no apt/dpkg lock fights
        import fcntl
        try:
            prior = await asyncio.to_thread(_installed_before, ctx, mgr, pkg)
            if prior:
                return f"{pkg} already installed via {mgr} by {prior} (resource ledger); no change made"
            return await _do_install(ctx, mgr, pkg)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


INSTALL_LOCK_WAIT_S = 900.0


async def _install_lock(path: Path):
    """The per-manager install lock, held (round 101). Opened with safeio.open_lock: open(lock, "w")
    followed a symlink planted there (truncating its target) and a FIFO there blocked the event loop
    forever. Acquired by polling LOCK_NB on the loop instead of a blocking flock in a worker thread:
    a cancelled install left that thread waiting on a file the cancel had already closed."""
    import fcntl
    from .safeio import open_lock
    lf = await asyncio.to_thread(open_lock, path)
    deadline = time.monotonic() + INSTALL_LOCK_WAIT_S
    delay = 0.02
    try:
        while True:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lf
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("install lock busy") from None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.25)
    except BaseException:
        lf.close()
        raise


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
    await asyncio.to_thread(record_change, ctx, f"{mgr} install", pkg, ok, (res.stdout or res.stderr)[-300:])
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
    rid = str(ctx.run_id)
    clean = re.sub(r'[^A-Za-z0-9_-]', '', rid)[:96] or 'run'
    if clean != rid:  # "a.b" and "ab" (or two all-punctuation ids) used to share one board
        clean += "-" + hashlib.sha256(rid.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return d / f"{clean}.json"


_BOARD_LOCKS: dict[str, threading.Lock] = {}
_BOARD_LOCKS_GUARD = threading.Lock()


class TaskBoardBusy(RuntimeError):
    pass


@contextlib.contextmanager
def _board_locked(p: Path):
    """One writer per board: a thread lock for this process, flock for other nodes on the data dir.
    The read-modify-write used to rely on running on the event loop; two nodes (or threads) lost
    each other's tasks."""
    with _BOARD_LOCKS_GUARD:
        lk = _BOARD_LOCKS.setdefault(str(p), threading.Lock())
    with lk:
        from .safeio import open_lock
        try:
            fh = open_lock(p.with_name(p.name + ".lock"))
        except OSError:
            fh = None  # a planted non-regular lock file: still serialized in-process
        try:
            if fh is not None:
                try:
                    from .safeio import flock_wait
                    held = flock_wait(fh.fileno())
                except (ImportError, OSError):
                    held = True
                if not held:  # round 104: a hung peer used to pin this worker thread forever
                    raise TaskBoardBusy("task board is locked by another process; try again later")
            yield
        finally:
            if fh is not None:
                fh.close()


@off_loop  # round 97: file read-modify-write now under _board_locked
async def t_task_board(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _board(ctx)
    try:
        with _board_locked(p):
            return _task_board_locked(ctx, args, p)
    except TaskBoardBusy as e:
        return f"ERROR: {e}"


def _task_board_locked(ctx: "AgentContext", args: dict[str, Any], p: Path) -> str:
    bad = False
    try:
        from .safeio import read_capped
        raw = read_capped(p, BOARD_READ_MAX)
        board: Any = json.loads(raw) if raw else []
    except FileNotFoundError:
        board = []
    except (OSError, ValueError, RecursionError):
        # read_regular used to hand back the tail of a >8 MB board, which never parses; the next
        # write then silently replaced every task. The old file is now kept as *.corrupt-<ts>.
        board, bad = [], True
    if not isinstance(board, list):
        board, bad = [], True
    # round 133: entries without a plain int id (a hand-edited or older board: "id": "3", true,
    # a non-dict) were dropped from the file on the next write; they are now kept as they were
    # and only left out of the listing and the id space
    keep = [t for t in board if not _board_task(t)]
    board = [t for t in board if _board_task(t)]
    op = str(args.get("op", "list"))
    if op == "add":
        title = str(args.get("title") or "").strip()
        if not title:
            return "ERROR: op=add needs a title"
        if len(board) >= MAX_BOARD_TASKS:  # every op rewrites and lists the whole board
            return f"ERROR: the board is full ({MAX_BOARD_TASKS} tasks); update existing ones instead"
        item = {"id": max((t["id"] for t in board), default=0) + 1, "title": title[:200], "status": "todo",
                "owner": str(ctx.name if args.get("owner") is None else args["owner"])[:200],
                "note": "", "by": ctx.name}
        board.append(item)
    elif op == "update":
        tid = _board_id(args.get("id"))
        if tid is None:  # round 133: 1e999 raised OverflowError, 1.7 updated task 1, true task 1
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
        if bad:
            try:
                os.replace(p, p.with_name(f"{p.name}.corrupt-{int(time.time())}"))
            except OSError:
                pass
        from .safeio import atomic_write  # random temp name: a planted .board.<pid>.tmp link is never followed
        atomic_write(p, json.dumps(board + keep, indent=1))  # atomic: a crash never leaves a half-written board
    if op != "list":
        _emit(ctx, "task.board", op=op, board=board[-10:])
    # round 133: a newline in a title/note/owner/status printed lines that read as other tasks
    # ("x\n#7 [done] deploy (owner Lead)"); fields are now shown on one line
    return "\n".join(f"#{t['id']} [{_one_line(t.get('status', '?'))}] {_one_line(t.get('title', ''))} "
                     f"(owner {_one_line(t.get('owner', '?'))})"
                     f"{' - ' + _one_line(t['note']) if t.get('note') else ''}" for t in board) or "board empty"


_CTRL = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")


def _one_line(v: Any) -> str:
    return _CTRL.sub(" ", str(v))


def _board_task(t: Any) -> bool:
    return isinstance(t, dict) and type(t.get("id")) is int


def _board_id(v: Any) -> int | None:
    """A task id from tool args: an int, an integral finite float, or a digit string; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None  # is_integer() is False for inf and NaN
    if isinstance(v, str) and re.fullmatch(r"-?[0-9]{1,18}", v.strip()):  # "²".isdigit() is True
        return int(v.strip())
    return None


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
