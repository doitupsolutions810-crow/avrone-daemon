"""OSINT aggregation (v0.11): data-driven profiles from the bounded recon tools.

Honesty and scope (unchanged from recon.py):
  * Public data only — DNS, certificate-transparency logs, TLS certs, HTTP headers,
    RDAP. One domain/host per call. No sweeps.
  * This profiles DOMAINS and INFRASTRUCTURE you name. It does NOT profile, locate,
    or deanonymize private individuals, and nothing here reaches private
    infrastructure or any capability AVRONE does not actually have.
  * Honours AVRONE_RECON / AVRONE_RECON_ALLOWLIST and logs every call to Codex.

Tools: osint_domain (aggregate a domain's public footprint), osint_metrics (recon/
osint activity rolled up from the Codex action logs).

AUDIT NOTE (v0.13 hardening): osint_domain is gated exactly like the recon tools and
BEFORE any sub-probe runs: the AVRONE_RECON kill switch (fails closed on unknown values),
recon._host_arg target syntax (no IP literals, CIDR, lists, wildcards), recon._reject_private
(no localhost/loopback/private/link-local/metadata/internal targets) and the allowlist. Each
sub-probe then re-applies its own gates and connect-time checks. Nothing here accepts a
person, username, email or phone number as a target.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import recon
from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext


MAX_SECTION_CHARS = 20_000


async def _section(fn, ctx: "AgentContext", args: dict[str, Any]) -> str:
    """One sub-probe's text, never an exception; clipped so one section cannot swamp the profile."""
    try:
        out = await fn(ctx, args)
    except Exception as e:  # noqa: BLE001 - one failed section never sinks the profile
        return f"ERROR: {type(e).__name__}: {str(e)[:200]}"
    if not isinstance(out, str):
        out = "" if out is None else str(out)
    if len(out) > MAX_SECTION_CHARS:
        out = out[:MAX_SECTION_CHARS] + f"\n... [clipped {len(out) - MAX_SECTION_CHARS} chars]"
    return out


async def t_osint_domain(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """One structured public-footprint profile of a domain: DNS, CT subdomains, TLS, HTTP."""
    # kill switch, target syntax, _reject_private and allowlist; refusals are audited
    host, err = recon._gate(ctx, "osint_domain", args)
    if err:
        return err
    report: dict[str, Any] = {"host": host}
    # Each section degrades independently: a failure in one does not sink the profile.
    # Before: the comment promised this, but a raising probe (or one returning None) sank the whole
    # profile with a raw exception, and a None CT result crashed on .splitlines().
    report["dns"] = await _section(recon.t_dns_enum, ctx, {"host": host, "types": ["A", "AAAA", "MX", "NS", "TXT"]})
    report["tls"] = await _section(recon.t_tls_inspect, ctx, {"host": host})
    report["http"] = await _section(recon.t_http_fingerprint, ctx, {"host": host})
    ct = await _section(recon.t_cert_transparency, ctx, {"host": host})
    report["subdomains"] = ct
    try:
        ctx.core.codex.emit(ctx.run_id, "osint.domain", ctx.name, host=host)
    except Exception:  # noqa: BLE001
        pass
    # Render compact, section by section.
    sub_count = ""
    first = ct.splitlines()[0] if ct else ""
    if "unique" in first:
        sub_count = first
    return (f"OSINT profile — {host} (public data)\n"
            f"\n[DNS]\n{report['dns']}\n"
            f"\n[TLS]\n{report['tls']}\n"
            f"\n[HTTP]\n{report['http']}\n"
            f"\n[Subdomains] {sub_count or ct[:200]}")


MAX_EVENT_LINE = 1024 * 1024
MAX_KINDS_SHOWN = 30


def _iter_lines(p: Path):
    """The file's lines, read one at a time (a multi-GB run log used to be read whole into memory);
    a line longer than MAX_EVENT_LINE is skipped. None when the file cannot be opened."""
    try:
        if not p.is_file():
            return None
        f = p.open("rb")
    except OSError:
        return None

    def gen():
        with f:
            skipping = False
            while True:
                try:
                    chunk = f.readline(MAX_EVENT_LINE + 1)
                except OSError:
                    return
                if not chunk:
                    return
                complete = chunk.endswith(b"\n")
                if skipping or (len(chunk) > MAX_EVENT_LINE and not complete):
                    skipping = not complete
                    continue
                yield chunk.decode("utf-8", "replace")
    return gen()


def _event(line: str) -> dict[str, Any] | None:
    """One JSONL event object, or None for a blank, corrupt, too-deep or non-object line."""
    try:
        ev = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return ev if isinstance(ev, dict) else None


async def t_osint_metrics(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Roll up recon/OSINT activity from the Codex run logs: tool counts and top targets."""
    data_dir = Path(ctx.core.settings.data_dir)
    log_dir = data_dir / "runs"
    kinds = Counter()
    hosts = Counter()
    refused = Counter()
    runs = 0
    if log_dir.exists():
        for p in log_dir.glob("*.jsonl"):
            lines = _iter_lines(p)
            if lines is None:  # vanished or not a regular file: skip it, never sink the rollup
                continue
            runs += 1
            for line in lines:
                ev = _event(line)
                if ev is None:
                    continue
                k = ev.get("kind", "")
                if not isinstance(k, str):
                    continue
                data = ev.get("data")
                data = data if isinstance(data, dict) else {}
                if k == recon.REFUSED_KIND:  # refusals are audited, not probes
                    refused[str(data.get("tool", "?"))[:80]] += 1
                    continue
                if k.startswith(("recon.", "osint.")):
                    kinds[k[:80]] += 1
                    h = data.get("host")
                    if h and isinstance(h, str):
                        hosts[h[:253]] += 1  # a 5 MB "host" used to be echoed whole into the report
    if not kinds and not refused:
        return "no recon/OSINT activity recorded yet"
    lines = [f"recon/OSINT metrics across {runs} run logs", "", "by tool:"]
    lines += [f"  {k}: {n}" for k, n in kinds.most_common(MAX_KINDS_SHOWN)]
    if len(kinds) > MAX_KINDS_SHOWN:  # 100k distinct junk kinds used to become a 100k-line report
        lines.append(f"  ... {len(kinds) - MAX_KINDS_SHOWN} more kinds")
    lines += ["", "top targets:"]
    lines += [f"  {h}: {n}" for h, n in hosts.most_common(10)]
    lines += ["", f"total probes: {sum(kinds.values())}"]
    if refused:
        lines += [f"refused by guardrails: {sum(refused.values())} "
                  f"({', '.join(f'{t}: {n}' for t, n in refused.most_common(MAX_KINDS_SHOWN))}"
                  f"{', ...' if len(refused) > MAX_KINDS_SHOWN else ''})"]
    return "\n".join(lines)


OSINT_TOOLS = [
    ("osint_domain", "aggregate one domain's public footprint (DNS, TLS, HTTP, certificate-transparency subdomains) "
     "into a single profile. Public data, one domain per call; no private-individual targeting.",
     '{"host": str}', t_osint_domain),
    ("osint_metrics", "roll up recorded recon/OSINT activity from the action logs: counts per tool and top targets.",
     "{}", t_osint_metrics),
]


def register_osint_tools(registry) -> None:
    for name, desc, args_doc, fn in OSINT_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
