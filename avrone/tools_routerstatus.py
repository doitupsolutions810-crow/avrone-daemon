"""router_status: a read-only, bounded, redacted view of the model-router bridge (avrone.gateway).

    router_status {"format"?: "text"|"json"}

Reports what Gateway would route through right now: the mode (live / mock), the primary upstream
(the configured AVRONE_PROVIDER) and the ordered fallback chain from AVRONE_LLM_FALLBACKS, each as
provider:model (plus its lead model when one is set), and each upstream's circuit-breaker state:

    closed     healthy, calls go through (fails < AVRONE_BREAKER_THRESHOLD, or never tripped)
    open       tripped: skipped until its cooldown ends (cooldown_s = seconds remaining)
    half-open  the cooldown ended but the last result was still a failure: the next call is a trial;
               one success closes it, one more failure re-opens it for AVRONE_BREAKER_COOLDOWN

with its consecutive failure count, total calls / errors, and the gateway's last route.

Never shown: API keys, base URLs (they can carry credentials or tokens), request bodies or error
text. Provider / model labels are charset-limited, any configured key that still appears in a
label is masked, upstream rows are capped at ROUTER_STATUS_MAX_UPSTREAMS and the whole output at
ROUTER_STATUS_MAX_CHARS. Nothing here calls a model, touches the network, or changes breaker state.
"""
from __future__ import annotations

import math
import json
import re
import time
from typing import TYPE_CHECKING, Any

from .tools import Tool, off_loop

if TYPE_CHECKING:
    from .trinity import AgentContext

ROUTER_STATUS_MAX_UPSTREAMS = 20
ROUTER_STATUS_MAX_CHARS = 6000
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.:/@+\-]")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:  # a peer-reported 400-digit int
        return None
    return f if math.isfinite(f) else None


def _label(v: Any, secrets: list[str], n: int = 96) -> str:
    s = str(v or "")
    for sec in secrets:
        if sec:
            s = s.replace(sec, "***")
    s = _UNSAFE_RE.sub("?", s)[:n]
    return s or "?"


def _gateway(ctx: "AgentContext") -> Any:
    from .gateway import Gateway
    llm = getattr(getattr(ctx, "core", None), "llm", None)
    if isinstance(llm, Gateway):
        return llm
    gw = getattr(llm, "gateway", None) if llm is not None else None
    return gw if isinstance(gw, Gateway) else None


def route_of(base_url: str) -> str:
    """How calls to this upstream leave the node: direct (loopback, or no proxy applies) or
    env-proxy (an HTTP(S)_PROXY in the environment will carry them). The proxy itself is never shown."""
    import urllib.request
    from urllib.parse import urlsplit
    from .gateway import is_loopback_url
    if is_loopback_url(base_url):
        return "direct"
    try:
        parts = urlsplit(str(base_url or ""))
        host = parts.hostname or ""
    except ValueError:
        return "direct"
    proxies = urllib.request.getproxies()
    if not proxies.get(parts.scheme or "https") or not host:
        return "direct"
    try:
        if urllib.request.proxy_bypass(host):
            return "direct"
    except Exception:  # noqa: BLE001 - odd NO_PROXY never breaks a read-only report
        pass
    return "env-proxy"


def circuit_state(up: Any, threshold: int, now: float | None = None) -> tuple[str, float]:
    """(closed|open|half-open, cooldown seconds remaining) from Upstream's real breaker fields."""
    now = time.monotonic() if now is None else now  # round 112: the breaker runs on monotonic time
    open_until = _num(getattr(up, "open_until", 0.0)) or 0.0
    fails = _num(getattr(up, "fails", 0)) or 0
    if open_until > now:
        return "open", round(open_until - now, 1)
    if open_until > 0 and fails >= max(1, threshold):
        return "half-open", 0.0
    return "closed", 0.0


def router_status(ctx: "AgentContext") -> dict[str, Any]:
    gw = _gateway(ctx)
    rep: dict[str, Any] = {"gateway": "off", "mode": None, "threshold": None, "cooldown": None,
                           "primary": None, "fallbacks": [], "chain": [], "upstreams": [], "more": 0,
                           "last_route": [], "summary": None}
    if gw is None:
        return rep
    ups = [u for u in list(getattr(gw, "upstreams", []) or []) if u is not None]
    secrets = sorted({str(getattr(u, attr, "") or "") for u in ups for attr in ("api_key", "base_url")
                      if getattr(u, attr, "")}, key=len, reverse=True)  # keys AND urls are masked
    threshold = int(_num(getattr(gw, "threshold", 3)) or 3)
    cooldown = _num(getattr(gw, "cooldown", 30))
    s = getattr(gw, "s", None)
    mode = str(getattr(s, "llm_mode", "live") or "live")
    rep.update(gateway="on", mode=_label(mode, secrets, 16), threshold=threshold,
               cooldown=cooldown if cooldown is not None else 30.0)
    now = time.monotonic()
    rows = []
    for i, u in enumerate(ups):
        state, remaining = circuit_state(u, threshold, now)
        ref = f"{_label(getattr(u, 'provider', ''), secrets, 32)}:{_label(getattr(u, 'model', ''), secrets)}"
        lead = getattr(u, "lead_model", "") or ""
        rows.append({"position": i, "role": "primary" if i == 0 else "fallback", "upstream": ref,
                     "lead_model": _label(lead, secrets) if lead else None, "state": state,
                     "route": route_of(str(getattr(u, "base_url", "") or "")),
                     "cooldown_s": remaining, "fails": int(_num(getattr(u, "fails", 0)) or 0),
                     "calls": int(_num(getattr(u, "calls", 0)) or 0),
                     "errors": int(_num(getattr(u, "errors", 0)) or 0)})
    chain = [r["upstream"] for r in rows]
    rep["primary"] = chain[0] if chain else None
    rep["fallbacks"] = chain[1:ROUTER_STATUS_MAX_UPSTREAMS]
    rep["chain"] = chain[:ROUTER_STATUS_MAX_UPSTREAMS]
    rep["upstreams"] = rows[:ROUTER_STATUS_MAX_UPSTREAMS]
    rep["more"] = max(0, len(rows) - ROUTER_STATUS_MAX_UPSTREAMS)
    rep["last_route"] = [_label(x, secrets, 48) for x in list(getattr(gw, "last_route", []) or [])[:10]]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    rep["summary"] = counts
    return rep


def _cap(text: str) -> str:
    return text if len(text) <= ROUTER_STATUS_MAX_CHARS else text[:ROUTER_STATUS_MAX_CHARS] + "\n...[truncated]"


def render_text(rep: dict[str, Any]) -> str:
    head = "router_status (read-only; api keys and URLs never shown)"
    if rep["gateway"] != "on":
        return _cap(f"{head}\nno model gateway configured on this node (no LLM router bridge to report)")
    lines = [head, f"mode: {rep['mode']}  breaker: threshold={rep['threshold']} fails, "
                   f"cooldown={rep['cooldown']:g}s"]
    if rep["mode"] == "mock":
        lines.append("mock mode: calls never reach an upstream; breaker state is idle")
    if not rep["chain"]:
        lines.append("no upstreams configured")
        return _cap("\n".join(lines))
    lines.append(f"primary: {rep['primary']}")
    if rep["fallbacks"]:
        lines.append("fallback chain: " + " -> ".join(rep["fallbacks"]))
    else:
        lines.append("no fallbacks configured (AVRONE_LLM_FALLBACKS empty): the primary is the only upstream")
    for r in rep["upstreams"]:
        cd = f" cooldown={r['cooldown_s']:g}s" if r["state"] == "open" else ""
        lead = f" lead={r['lead_model']}" if r["lead_model"] else ""
        lines.append(f"- [{r['position']}] {r['upstream']}{lead} {r['role']} circuit={r['state']}{cd} "
                     f"route={r.get('route', 'direct')} "
                     f"fails={r['fails']} calls={r['calls']} errors={r['errors']}")
    if rep["more"]:
        lines.append(f"...[{rep['more']} more upstreams]")
    lines.append("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["summary"].items())))
    lines.append("last route: " + (" -> ".join(rep["last_route"]) or "(no calls yet)"))
    return _cap("\n".join(lines))


@off_loop  # round 96: await-free, reads/writes files; ran on the event loop
async def t_router_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    fmt = str(args.get("format", "text") or "text").lower()
    if fmt not in {"text", "json"}:
        return 'ERROR: format must be "text" or "json"'
    rep = router_status(ctx)
    if fmt == "json":
        text = json.dumps(rep, indent=1, sort_keys=True)
        while len(text) > ROUTER_STATUS_MAX_CHARS and rep["upstreams"]:
            rep["upstreams"] = rep["upstreams"][:-1]
            rep["more"] += 1
            text = json.dumps(rep, indent=1, sort_keys=True)
        return _cap(text)
    return render_text(rep)


ROUTER_STATUS_TOOLS: list[Tool] = [
    Tool("router_status",
         "read-only: the model-router bridge (gateway) view: mode, primary provider:model, the ordered "
         "AVRONE_LLM_FALLBACKS chain, and each upstream's circuit state (closed/open/half-open) with "
         "cooldown remaining and failure counts, plus the last route. API keys and URLs are never shown.",
         '{"format"?: "text"|"json"}',
         t_router_status, source="builtin",
         schema={"optional": {"format": "str"}}),
]


def register_routerstatus_tools(registry) -> None:
    for t in ROUTER_STATUS_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
