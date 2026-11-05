"""doctor: offline, read-only preflight of a node's hosting readiness.

    avrone-cli doctor [--json PATH] [--strict]

Checks the Python version, the configuration (it loads), the data dir and
workspace (writable, or creatable, without creating anything), the runner
choice, Docker availability when the Docker runner is configured, which
security toggles are on, which tokens are present (never their values), and
warns on insecure combinations.

Guarantees:
  * Pure/offline: no network calls, no subprocesses, nothing is started.
  * Read-only: no file or directory is created or changed (writability is
    judged with os.access on the path or its nearest existing parent).
  * Secrets redacted: token values never appear in the report; only "set" /
    "unset" (and a weakness verdict) are shown. URLs lose any userinfo.

Statuses: ok | info | warn | fail. The report is "ok" when nothing failed;
with strict=True a warning also makes it not ok.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
_RANK = {OK: 0, INFO: 0, WARN: 1, FAIL: 2}

RUNNERS = ("auto", "docker", "local")
MIN_PY = (3, 11)       # pyproject requires-python
REC_PY = (3, 12)       # what the container image ships
WEAK_TOKEN_LEN = 16
PLACEHOLDERS = {"change-me", "changeme", "change_me", "secret", "password", "token", "admin",
                "test", "test-token", "xxx", "todo", "replace-me"}
TOKEN_ENV = ("AVRONE_ADMIN_TOKEN", "AVRONE_FLEET_TOKEN", "AVRONE_LLM_API_KEY", "OPENCLAW_GATEWAY_TOKEN",
             "XAI_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY")
_TRUE = {"1", "true", "yes", "on", "allow"}
_FALSE = {"0", "false", "no", "off", "deny"}
_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]{0,31}://)[^/@\s]*@")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def redact_url(url: str) -> str:
    """Strip user:pass@ from a URL so it can be shown."""
    return _USERINFO_RE.sub(r"\1***@", url or "")


def token_state(value: str | None) -> str:
    """Presence/strength of a secret, never its value: unset | set | weak | placeholder."""
    if not value:
        return "unset"
    v = value.strip()
    if v.lower() in PLACEHOLDERS:
        return "placeholder"
    if len(v) < WEAK_TOKEN_LEN:
        return "weak"
    return "set"


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _cleartext_remote(url: str) -> bool:
    """True when url is plain http:// to a non-loopback host."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme == "http" and not _is_loopback_host(parts.hostname or "") \
        and (parts.hostname or "") != "host.docker.internal"


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE


def _nearest_existing(path: Path) -> Path | None:
    p = path
    while True:
        if p.exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def check_writable(label: str, path: Path) -> Check:
    """Writable now, or creatable (nearest existing parent writable). Never creates anything."""
    path = Path(path)
    if path.exists():
        if not path.is_dir():
            return Check(label, FAIL, f"{path} exists but is not a directory")
        if os.access(path, os.W_OK | os.X_OK):
            return Check(label, OK, f"{path} is writable")
        return Check(label, FAIL, f"{path} is not writable by uid {_uid()}",
                     "chown/chmod it for the AVRONE user, or point the env var elsewhere")
    parent = _nearest_existing(path.parent)
    if parent is not None and parent.is_dir() and os.access(parent, os.W_OK | os.X_OK):
        return Check(label, OK, f"{path} does not exist yet; will be created under {parent}")
    return Check(label, FAIL, f"{path} does not exist and {parent} is not writable",
                 "create it with the right owner, or point the env var elsewhere")


def _uid() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else "?"


def check_python(version: tuple[int, ...] | None = None) -> Check:
    v = tuple(version or sys.version_info[:3])
    s = ".".join(str(x) for x in v)
    if v[:2] < MIN_PY:
        return Check("python", FAIL, f"Python {s} < {MIN_PY[0]}.{MIN_PY[1]} (required)", "use Python 3.12")
    if v[:2] < REC_PY:
        return Check("python", WARN, f"Python {s}; 3.12 is the tested/container version")
    return Check("python", OK, f"Python {s}")


def check_docker(runner: str, which: Callable[[str], str | None], env: Mapping[str, str],
                 sockets: tuple[str, ...] = DOCKER_SOCKETS) -> list[Check]:
    """Docker CLI + daemon endpoint presence, judged from PATH and the socket file only."""
    binary = which("docker")
    docker_host = env.get("DOCKER_HOST", "")
    sock = next((s for s in sockets if Path(s).exists()), None)
    endpoint = (f"DOCKER_HOST={redact_url(docker_host)}" if docker_host
                else (f"socket {sock}" if sock else "no daemon socket found"))
    if runner == "docker":
        if not binary:
            return [Check("docker", FAIL, "AVRONE_RUNNER=docker but no `docker` CLI on PATH",
                          "install the Docker CLI, or set AVRONE_RUNNER=local/auto")]
        if not (docker_host or sock):
            return [Check("docker", WARN, f"docker CLI at {binary} but {endpoint}",
                          "mount /var/run/docker.sock or set DOCKER_HOST")]
        out = [Check("docker", OK, f"docker CLI at {binary}; {endpoint}")]
        if sock and not docker_host and not os.access(sock, os.R_OK | os.W_OK):
            out.append(Check("docker.socket", WARN, f"{sock} not accessible by uid {_uid()}",
                             "add the AVRONE user to the socket's group (compose: group_add)"))
        return out
    if runner == "auto":
        picked = "docker" if binary else "local"
        return [Check("docker", INFO, f"AVRONE_RUNNER=auto will pick {picked}"
                      + (f" (docker CLI at {binary}; {endpoint}); falls back to local if it can't start"
                         if binary else " (no docker CLI on PATH)"))]
    return [Check("docker", INFO, "not needed (AVRONE_RUNNER=local)")]


def run_doctor(settings: Any = None, *, env: Mapping[str, str] | None = None,
               which: Callable[[str], str | None] = shutil.which,
               python_version: tuple[int, ...] | None = None,
               sockets: tuple[str, ...] = DOCKER_SOCKETS, strict: bool = False) -> dict[str, Any]:
    """Run every check. Returns {"ok", "status", "counts", "checks": [...], "toggles", "tokens"}."""
    env = os.environ if env is None else env
    checks: list[Check] = [check_python(python_version)]

    if settings is None:
        try:
            from .config import Settings
            settings = Settings()
        except Exception as e:  # bad provider, non-numeric int, ...
            checks.append(Check("config", FAIL, f"configuration does not load: {type(e).__name__}: {e}",
                                "fix the AVRONE_* env vars / .env file"))
            return _finish(checks, {}, _tokens(env), strict)
    checks.append(Check("config", OK, f"provider={settings.provider} mode={settings.llm_mode} "
                                      f"base_url={redact_url(settings.llm_base_url)}"))

    checks.append(check_writable("data_dir", Path(settings.data_dir)))
    checks.append(check_writable("workspace", Path(settings.workspace)))

    runner = str(settings.runner or "auto").strip().lower()
    if runner in RUNNERS:
        checks.append(Check("runner", OK, f"AVRONE_RUNNER={runner}"))
    else:
        checks.append(Check("runner", FAIL, f"AVRONE_RUNNER={settings.runner!r} is not one of {', '.join(RUNNERS)}",
                            "unknown values silently fall back to the local (unsandboxed) runner"))
    checks.extend(check_docker(runner, which, env, sockets))
    effective_local = runner == "local" or (runner == "auto" and not which("docker")) or runner not in RUNNERS

    # ---- toggles
    recon_raw = (env.get("AVRONE_RECON") or "allow").strip().lower()
    recon_on = recon_raw in {"allow", "on", "true", "yes", "1"}
    allowlist = [h for h in (env.get("AVRONE_RECON_ALLOWLIST") or "").split(",") if h.strip()]
    recon_any = (env.get("AVRONE_RECON_ALLOWLIST") or "").strip() == "*"
    fleet_allow = [h for h in (env.get("AVRONE_FLEET_ALLOW") or "").split(",") if h.strip()]
    fleet_tok = token_state(env.get("AVRONE_FLEET_TOKEN"))
    toggles = {
        "recon": recon_raw if recon_raw in _TRUE | _FALSE else f"{recon_raw[:40]} (unrecognised: fails closed)",
        "recon_allowlist": f"{len(allowlist)} entr{'y' if len(allowlist) == 1 else 'ies'}" if allowlist and not recon_any
                           else ("* (any public host)" if recon_any else "(none: default-deny)"),
        "network": "on" if settings.allow_network else "off",
        "shell": "on" if settings.agents_can_shell else "off",
        "self_evolve": (env.get("AVRONE_SELF_EVOLVE") or "allow").lower(),
        "self_changes": (env.get("AVRONE_SELF_CHANGES") or "allow").lower(),
        "abs_read": "on" if (env.get("AVRONE_ALLOW_ABS_READ") or "1") == "1" else "off",
        "fleet": "on" if fleet_tok != "unset" else "off",
        "fleet_allowlist": f"{len(fleet_allow)} node(s)" if fleet_allow else "(any node)",
        "host_shell": "on" if (settings.agents_can_shell and effective_local) else "off",
        "fleet_privileged": ("allow" if (env.get("AVRONE_FLEET_PRIVILEGED") or "").strip().lower() == "allow"
                             else "deny"),
        "fleet_offload": ((env.get("AVRONE_FLEET_OFFLOAD") or "off").strip().lower()
                          if (env.get("AVRONE_FLEET_OFFLOAD") or "off").strip().lower() in ("off", "spill", "spread")
                          else "off"),
        "squad_dispatch": ((env.get("AVRONE_SQUAD_DISPATCH") or "local").strip().lower()
                           if (env.get("AVRONE_SQUAD_DISPATCH") or "local").strip().lower() in ("local", "network", "hybrid")
                           else "local"),
        "privileged_nodes": str(len([h for h in (env.get("AVRONE_PRIVILEGED_NODES") or "").split(",") if h.strip()])),
        "agent_container": ("persistent" if (env.get("AVRONE_AGENT_CONTAINER") or "").strip().lower() == "persistent"
                            else "off"),
        "agent_migrate": "on" if (env.get("AVRONE_AGENT_MIGRATE") or "").strip().lower() in _TRUE - {"allow"} else "off",
    }
    checks.append(Check("toggles", INFO, " ".join(f"{k}={v}" for k, v in toggles.items())))

    tokens = _tokens(env)
    checks.append(Check("tokens", INFO, " ".join(f"{k}={v}" for k, v in tokens.items())))

    # ---- llm key
    if settings.llm_mode != "mock":
        from .config import PROVIDERS
        key_env = PROVIDERS.get(settings.provider, ("", "", ""))[2]
        if not settings.llm_api_key and key_env:
            checks.append(Check("llm.key", WARN, f"provider {settings.provider} has no API key "
                                                 f"(AVRONE_LLM_API_KEY / {key_env} unset)"))
        if settings.llm_api_key and _cleartext_remote(settings.llm_base_url):
            checks.append(Check("llm.transport", WARN, "API key is sent over plain http:// to a remote host",
                                "use https or a private network"))

    checks.extend(_insecure_combos(settings, env, toggles, tokens, recon_on, allowlist, fleet_allow,
                                   effective_local))
    return _finish(checks, toggles, tokens, strict)


def _tokens(env: Mapping[str, str]) -> dict[str, str]:
    return {k: token_state(env.get(k)) for k in TOKEN_ENV}


def _insecure_combos(settings: Any, env: Mapping[str, str], toggles: dict[str, str], tokens: dict[str, str],
                     recon_on: bool, allowlist: list[str], fleet_allow: list[str],
                     effective_local: bool) -> list[Check]:
    out: list[Check] = []
    recon_any = (env.get("AVRONE_RECON_ALLOWLIST") or "").strip() == "*"
    host = env.get("AVRONE_HOST") or "127.0.0.1"
    exposed = not _is_loopback_host(host)
    admin, fleet = tokens["AVRONE_ADMIN_TOKEN"], tokens["AVRONE_FLEET_TOKEN"]

    if exposed and admin == "unset":
        out.append(Check("security.admin", WARN, f"API bound to {host} with no AVRONE_ADMIN_TOKEN "
                                                 "(admin endpoints off, everything else reachable)",
                         "set a long random AVRONE_ADMIN_TOKEN and put the node behind a proxy/VPN"))
    if admin in ("weak", "placeholder"):
        out.append(Check("security.admin_token", FAIL if admin == "placeholder" else WARN,
                         f"AVRONE_ADMIN_TOKEN is {admin}", f"use >= {WEAK_TOKEN_LEN} random chars"))
    if fleet in ("weak", "placeholder"):
        out.append(Check("security.fleet_token", FAIL if fleet == "placeholder" else WARN,
                         f"AVRONE_FLEET_TOKEN is {fleet}", f"use >= {WEAK_TOKEN_LEN} random chars"))
    a, f = env.get("AVRONE_ADMIN_TOKEN"), env.get("AVRONE_FLEET_TOKEN")
    if a and f and a == f:
        out.append(Check("security.token_reuse", WARN, "admin and fleet tokens are identical",
                         "a fleet peer could then call admin endpoints; use different secrets"))
    access = (env.get("AVRONE_PUBLIC_ACCESS") or "loopback").strip().lower()
    access = access if access in ("loopback", "admin", "all") else "loopback"
    if exposed and access == "all":
        shell = " and agents can run shell" if settings.agents_can_shell else ""
        out.append(Check("security.exposed_shell" if shell else "security.public_access", WARN,
                         f"API bound to {host} with AVRONE_PUBLIC_ACCESS=all{shell}: anyone who reaches the "
                         "port can start agents and read every run",
                         "AVRONE_PUBLIC_ACCESS=loopback (default) or admin, or front with an authenticating proxy"))
    elif exposed and access == "loopback" and admin == "unset":
        out.append(Check("security.public_access", INFO, f"API bound to {host}: remote callers get 403 on chat, "
                                                         "runs and events (AVRONE_PUBLIC_ACCESS=loopback, no admin token)",
                         "set AVRONE_ADMIN_TOKEN to let remote clients in with X-Admin-Token"))
    if toggles["fleet"] == "on" and not fleet_allow and toggles["host_shell"] == "on":
        out.append(Check("security.fleet", WARN, "privileged fleet (shell on the host runner) with an open "
                                                 "AVRONE_FLEET_ALLOW: any node holding the token can drive this host",
                         "set AVRONE_FLEET_ALLOW to the node ids you trust, or use the docker runner"))
    elif toggles["fleet"] == "on" and not fleet_allow:
        out.append(Check("security.fleet", INFO, "fleet on with open AVRONE_FLEET_ALLOW (any token holder)"))
    if toggles["fleet_privileged"] == "allow":
        if toggles["privileged_nodes"] == "0":
            out.append(Check("security.fleet_privileged", INFO, "AVRONE_FLEET_PRIVILEGED=allow but no "
                                                               "AVRONE_PRIVILEGED_NODES: every privileged request is refused"))
        elif not fleet_allow:
            out.append(Check("security.fleet_privileged", WARN, "privileged tier on with an open AVRONE_FLEET_ALLOW",
                             "pin AVRONE_FLEET_ALLOW to the node ids you trust"))
        else:
            out.append(Check("security.fleet_privileged", INFO, f"privileged tier on for "
                                                               f"{toggles['privileged_nodes']} listed node(s)"))
    if toggles["fleet"] == "on" and toggles.get("fleet_offload", "off") != "off" and not fleet_allow:
        out.append(Check("security.fleet_offload", WARN, f"AVRONE_FLEET_OFFLOAD={toggles['fleet_offload']} with an open "
                                                         "AVRONE_FLEET_ALLOW: squad work (task text) may go to any node "
                                                         "that registers with the token",
                         "pin AVRONE_FLEET_ALLOW to the node ids you trust, or AVRONE_FLEET_OFFLOAD=off"))
    if toggles["fleet"] == "on" and toggles.get("squad_dispatch", "local") != "local" and not fleet_allow:
        out.append(Check("security.squad_dispatch", WARN, f"AVRONE_SQUAD_DISPATCH={toggles['squad_dispatch']} with an "
                                                          "open AVRONE_FLEET_ALLOW: squad subtasks (task text) may be "
                                                          "placed on any node that registers with the token",
                         "pin AVRONE_FLEET_ALLOW to the node ids you trust, or AVRONE_SQUAD_DISPATCH=local"))
    if toggles.get("agent_migrate") == "on":
        if not fleet_allow:
            out.append(Check("security.agent_migrate", WARN, "AVRONE_AGENT_MIGRATE=on with an open AVRONE_FLEET_ALLOW: "
                                                             "agent homes may be sent to any node holding the token "
                                                             "(inbound packs are refused until the allowlist is set)",
                             "set AVRONE_FLEET_ALLOW to the node ids you trust"))
        else:
            out.append(Check("security.agent_migrate", INFO, f"agent migration on, limited to {len(fleet_allow)} "
                                                             "allowlisted node(s)"))
    for name in ("AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
        if toggles["fleet"] == "on" and _cleartext_remote(env.get(name, "")):
            out.append(Check("security.fleet_transport", WARN, f"{name} is plain http:// to a remote host",
                             "HMAC gives integrity, not confidentiality: use https or a private mesh"))
    if toggles["host_shell"] == "on":
        out.append(Check("security.sandbox", WARN, "agents run shell directly on this host (local runner, no sandbox)",
                         "use AVRONE_RUNNER=docker, or AVRONE_AGENTS_CAN_SHELL=false"))
    elif settings.allow_network and not effective_local:
        out.append(Check("security.network", INFO, "sandbox containers have network access",
                         "AVRONE_ALLOW_NETWORK=false runs them with --network none"))
    if recon_on and recon_any and settings.allow_network:
        out.append(Check("security.recon", WARN, "recon allowlist is \"*\" (any public host)",
                         "list only the domains you own or are authorized on in AVRONE_RECON_ALLOWLIST"))
    elif recon_on and not allowlist:
        out.append(Check("security.recon", INFO, "recon is default-deny (no AVRONE_RECON_ALLOWLIST): every target refused",
                         "set AVRONE_RECON_ALLOWLIST to your own/authorized domains to use recon"))
    evolve_on = toggles["self_evolve"] != "deny"
    if evolve_on and toggles["self_changes"] == "allow" and settings.agents_can_shell and exposed:
        out.append(Check("security.self_modify", WARN, "network-exposed node lets agents rewrite AVRONE and "
                                                       "install packages", "AVRONE_SELF_EVOLVE=deny / AVRONE_SELF_CHANGES=log"))
    return out


def _finish(checks: list[Check], toggles: dict[str, str], tokens: dict[str, str], strict: bool) -> dict[str, Any]:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (OK, INFO, WARN, FAIL)}
    worst = max((_RANK[c.status] for c in checks), default=0)
    status = {0: OK, 1: WARN, 2: FAIL}[worst]
    ok = worst < (1 if strict else 2)
    return {"ok": ok, "status": status, "strict": strict, "counts": counts,
            "checks": [asdict(c) for c in checks], "toggles": toggles, "tokens": tokens}


_MARK = {OK: "[ ok ]", INFO: "[info]", WARN: "[WARN]", FAIL: "[FAIL]"}


def format_report(report: dict[str, Any]) -> str:
    lines = ["AVRONE doctor (offline, read-only; secret values never shown)"]
    for c in report["checks"]:
        lines.append(f"{_MARK.get(c['status'], '[ ?? ]')} {c['name']:<24} {c['detail']}")
        if c.get("hint") and c["status"] in (WARN, FAIL):
            lines.append(f"{'':7}{'':<24} -> {c['hint']}")
    n = report["counts"]
    lines.append(f"result: {report['status'].upper()} ({n['ok']} ok, {n['info']} info, {n['warn']} warn, "
                 f"{n['fail']} fail){' [strict]' if report.get('strict') else ''}")
    return "\n".join(lines)
