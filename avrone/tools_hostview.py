"""proc_status + api_surface: what an agent can see of its own host and its own node's front door.
Both read-only, own process tree / own app only, bounded output.

    proc_status {"all"?: bool}
        Child processes of this AVRONE process (from /proc; Linux only): pid, state, age, RSS and a
        truncated command line. Flags long-running children (possible orphans/runaways) past
        AVRONE_PROC_LONG_S (default 600). Never shows environment variables or other users' processes.

    api_surface {}
        This node's HTTP routes with their auth class (public, admin token, fleet-signed), plus
        whether admin and fleet are actually enabled right now. Lets a squad reason about what a
        peer or a browser can reach without probing it.
"""
from __future__ import annotations

import inspect
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext

PROC = Path("/proc")
MAX_PROCS = 50
CMD_MAX = 160


def _long_s() -> float:
    import math
    try:
        v = float(os.getenv("AVRONE_PROC_LONG_S", "600") or 600)
    except ValueError:
        return 600.0
    # round 54: "inf" disabled the runaway flag silently and printed "running >= infs"
    return v if v > 0 and math.isfinite(v) else 600.0


def _boot_time() -> float | None:
    try:
        for line in (PROC / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


from .textsafe import escape as _escape  # noqa: E402
import re  # noqa: E402

# round 65: a credential passed on a command line ("--password=x", "--token x", "-H 'Authorization:
# Bearer x'", "postgres://u:pw@db") was printed in the child's argv: proc_status hid env vars but
# not argv. Flag values whose NAME says secret are masked; the shapes go through redact_text.
_ARGV_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(--?[A-Za-z0-9_.\-]*(?:passw(?:or)?d|passwd|pwd|secret|token|api[-_]?key|"
    r"access[-_]?key|private[-_]?key|credential|auth)[A-Za-z0-9_.\-]*)(=|\s+)(?!-)(\S+)")


def _redact_cmd(cmd: str) -> str:
    """argv with credentials masked; never raises (a read-only view must not fail on one process)."""
    try:
        out = _ARGV_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", cmd)
        from .tools_researchnote import redact_text
        return redact_text(out)
    except Exception:  # noqa: BLE001 - mask everything rather than show an unchecked argv
        return cmd.split(" ", 1)[0] + " ***"


def _children(root_pid: int) -> dict[int, dict[str, Any]]:
    """pid -> info for every descendant of root_pid (bounded scan of /proc)."""
    info: dict[int, dict[str, Any]] = {}
    try:
        pids = [int(p.name) for p in PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return {}
    try:  # round 54: sysconf can raise (ValueError/OSError) or report 0 -> ZeroDivisionError
        tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4
    except (ValueError, OSError):
        tck, page_kb = 100, 4
    tck = tck if isinstance(tck, int) and tck > 0 else 100
    page_kb = page_kb if isinstance(page_kb, int) and page_kb > 0 else 4
    btime = _boot_time()
    for pid in pids:
        try:
            stat = (PROC / str(pid) / "stat").read_text()
            rparen = stat.rindex(")")
            fields = stat[rparen + 2:].split()
            state, ppid, start_ticks, rss_pages = fields[0], int(fields[1]), int(fields[19]), int(fields[21])
            raw = (PROC / str(pid) / "cmdline").read_bytes()
        except (OSError, ValueError, IndexError):
            continue  # raced with exit, or not ours to read
        cmd = raw.replace(b"\x00", b" ").decode(errors="replace").strip() or stat[stat.index("(") + 1:rparen]
        started = btime + start_ticks / tck if btime is not None else None
        info[pid] = {"pid": pid, "ppid": ppid, "state": state, "started": started,
                     "rss_kb": max(0, rss_pages) * page_kb,
                     # round 65: redact the FULL argv before the cut, so a cut never shows half a secret
                     "cmd": _escape(_redact_cmd(cmd)[:CMD_MAX])}  # round 146: argv/comm are the process's own choice
    out: dict[int, dict[str, Any]] = {}
    frontier = {root_pid}
    while frontier:
        nxt = {p for p, i in info.items() if i["ppid"] in frontier and p not in out and p != root_pid}
        for p in nxt:
            out[p] = info[p]
        frontier = nxt
    return out


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_proc_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not PROC.is_dir():
        return "proc_status: /proc is not available on this host (Linux only)"
    kids = _children(os.getpid())
    now, long_s = time.time(), _long_s()
    rows = sorted(kids.values(), key=lambda i: i["started"] or 0)
    long_ = [r for r in rows if r["started"] is not None and now - r["started"] >= long_s]
    zombies = [r for r in rows if r["state"] == "Z"]
    from .argnum import tool_bool  # round 65: "true" (a string, as many models send it) was ignored
    show = rows if tool_bool(args.get("all"), False) is True else (long_ + [z for z in zombies if z not in long_] or rows)
    lines = [f"proc_status: {len(rows)} child process(es) of pid {os.getpid()}; "
             f"{len(long_)} running >= {long_s:.0f}s, {len(zombies)} zombie(s)"]
    for r in show[:MAX_PROCS]:
        age = f"{now - r['started']:.0f}s" if r["started"] is not None else "?"
        lines.append(f"- {r['pid']} [{r['state']}] age {age} rss {r['rss_kb']}KB: {r['cmd']}")
    if len(show) > MAX_PROCS:
        lines.append(f"... {len(show) - MAX_PROCS} more")
    return "\n".join(lines)


def _auth_class(fn: Any) -> str:
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return "unknown"
    if "_gateway(request" in src or "_verified(request" in src:
        return "fleet-signed"
    if "require_blackboard_admin(" in src or "require_admin(" in src:
        return "admin"
    if "require_local(" in src or "caller_allowed(" in src:
        return "local"  # loopback or admin token, per AVRONE_PUBLIC_ACCESS
    return "public"


def api_surface() -> dict[str, Any]:
    from . import server  # lazy: the server imports the tool modules
    routes = []
    for r in server.app.routes:
        path = getattr(r, "path", "")
        if not path or path.startswith(("/openapi", "/docs", "/redoc", "/static")):
            continue
        methods = sorted(getattr(r, "methods", None) or {"WS"})
        methods = [m for m in methods if m != "HEAD"]
        routes.append({"path": path, "methods": methods, "auth": _auth_class(getattr(r, "endpoint", None))})
    return {"routes": sorted(routes, key=lambda x: x["path"]),
            "admin_enabled": bool(getattr(server.settings, "admin_token", None)),
            "access_policy": server.access_policy(),
            "fleet_enabled": bool(os.getenv("AVRONE_FLEET_TOKEN"))}


async def t_api_surface(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        a = api_surface()
    except Exception as e:  # noqa: BLE001 - a CLI-only node has no app to describe
        return f"api_surface: no HTTP app on this node ({type(e).__name__})"
    by: dict[str, int] = {}
    for r in a["routes"]:
        by[r["auth"]] = by.get(r["auth"], 0) + 1
    lines = [f"api_surface: {len(a['routes'])} route(s); " + ", ".join(f"{k}={v}" for k, v in sorted(by.items())),
             f"admin token {'SET' if a['admin_enabled'] else 'unset (admin routes answer 403)'}; "
             f"fleet {'ON' if a['fleet_enabled'] else 'off (signed routes refuse)'}; "
             f"AVRONE_PUBLIC_ACCESS={a.get('access_policy', '?')}"]
    for r in a["routes"]:
        lines.append(f"- {','.join(r['methods'])} {r['path']} [{r['auth']}]")
    return "\n".join(lines)


_PLACEHOLDERS = ("changeme", "secret", "password", "token", "admin", "test", "default", "example",
                 "your-token", "yourtoken")


def _token_strength(v: Any) -> str:
    if v is None:
        return "unset"
    if hasattr(v, "get_secret_value"):  # a pydantic SecretStr
        try:
            v = v.get_secret_value()
        except Exception:  # noqa: BLE001
            return "unknown"
    if not isinstance(v, str):  # round 54: a non-str token raised AttributeError -> "no HTTP app"
        return "unknown"
    t = v.strip()
    if not t:
        # round 54: a whitespace-only token was "unset" only if empty; 16 spaces read "ok"
        return "unset" if not v else "weak"
    low = t.lower()
    # round 54: "changeme123", "CHANGEME" and "secret-secret-secret" all read "ok"
    if low in _PLACEHOLDERS or any(low.startswith(p) and len(set(low[len(p):])) <= 3 for p in _PLACEHOLDERS):
        return "placeholder"
    # round 65: the check above only looked at ONE placeholder prefix, so "secret-secret-secret",
    # "passwordpassword", "adminadminadmin123" and "tokentokentoken12345" all read "ok". Strip every
    # placeholder word, digit and separator: nothing left means it is made only of placeholders.
    rest = low
    for p in sorted(_PLACEHOLDERS + ("changeme", "letmein", "qwerty", "pass"), key=len, reverse=True):
        rest = rest.replace(p, "")
    if not re.sub(r"[0-9\s_.\-:/+=!@#*]", "", rest):
        return "placeholder"
    if len(t) < 16:  # round 54: measured on the stripped token ("short" + 12 spaces was "ok")
        return "weak"
    # round 65: "aaaaaaaaaaaaaaaaaaaa", "abab...", all-digit and keyboard/alphabet runs read "ok"
    if len(set(low)) < 8 or t.isdigit() or any(low in seq or seq.startswith(low[:12]) for seq in _SEQUENCES):
        return "weak"
    return "ok"


_SEQUENCES = ("qwertyuiopasdfghjklzxcvbnm", "abcdefghijklmnopqrstuvwxyz", "01234567890123456789",
              "1qaz2wsx3edc4rfv5tgb6yhn", "zyxwvutsrqponmlkjihgfedcba")


def exposure() -> dict[str, Any]:
    """Who can reach what on this node right now, as a remote (non-loopback) caller with no token."""
    host = os.getenv("AVRONE_HOST") or "127.0.0.1"
    from .fusion_loopback import is_access_loopback
    exposed = not is_access_loopback(host)
    a = api_surface()
    policy, admin_on, fleet_on = a["access_policy"], a["admin_enabled"], a["fleet_enabled"]
    remote_open = [f"{','.join(r['methods'])} {r['path']}" for r in a["routes"]
                   if r["auth"] == "public" or (r["auth"] == "local" and policy == "all")]
    risky = [x for x in remote_open if any(k in x for k in ("/api/chat", "/api/runs", "/ws/events", "/api/telemetry"))]
    from . import server
    admin_tok = _token_strength(getattr(server.settings, "admin_token", None))
    fleet_tok = _token_strength(os.getenv("AVRONE_FLEET_TOKEN"))
    issues = []
    if exposed and risky:
        issues.append(f"bound to {host} with {len(risky)} agent/run route(s) open to anyone")
    for name, st in (("admin", admin_tok), ("fleet", fleet_tok)):
        if st in ("weak", "placeholder"):
            issues.append(f"{name} token is {st}")
    adm = getattr(server.settings, "admin_token", None)
    if hasattr(adm, "get_secret_value"):
        try:
            adm = adm.get_secret_value()
        except Exception:  # noqa: BLE001
            adm = None
    fl = os.getenv("AVRONE_FLEET_TOKEN") or ""
    # round 54: "tok" and "tok\n" (a token pasted with its newline) were not seen as the same secret
    if admin_tok != "unset" and fl.strip() and isinstance(adm, str) and adm.strip() == fl.strip():
        issues.append("admin and fleet tokens are the same secret")
    return {"host": _escape(host), "exposed": exposed, "policy": policy, "admin_token": admin_tok,
            "fleet_token": fleet_tok if fleet_on else "unset", "remote_open": remote_open, "risky": risky,
            "issues": issues, "verdict": "AT RISK" if issues else ("LOCAL" if not exposed else "GUARDED")}


async def t_exposure_check(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        e = exposure()
    except Exception as ex:  # noqa: BLE001
        return f"exposure_check: no HTTP app on this node ({type(ex).__name__})"
    lines = [f"exposure_check: {e['verdict']} - bind {e['host']} ({'exposed' if e['exposed'] else 'loopback'}), "
             f"AVRONE_PUBLIC_ACCESS={e['policy']}, admin token {e['admin_token']}, fleet token {e['fleet_token']}",
             f"open to a remote caller without a token: {len(e['remote_open'])} route(s)"
             + (f"; agent/run routes among them: {', '.join(e['risky'])}" if e["risky"] else "")]
    lines += [f"- issue: {i}" for i in e["issues"]]
    return "\n".join(lines)


HOSTVIEW_TOOLS = [
    Tool("proc_status", "read-only: child processes of this AVRONE node (pid, state, age, RSS, command); "
         "flags long-running ones and zombies. Own process tree only, never env vars.",
         '{"all"?: bool}', t_proc_status, source="builtin", schema={"optional": {"all": "bool"}}),
    Tool("api_surface", "read-only: this node's HTTP routes and who can call each (public/admin/fleet-signed), "
         "and whether admin and fleet are enabled.", "{}", t_api_surface, source="builtin", schema={}),
    Tool("exposure_check", "read-only: is this node safe to expose? bind address, AVRONE_PUBLIC_ACCESS, token "
         "strength (never values), and which routes a remote caller reaches with no token.",
         "{}", t_exposure_check, source="builtin", schema={}),
]


def register_hostview_tools(registry) -> None:
    for t in HOSTVIEW_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
