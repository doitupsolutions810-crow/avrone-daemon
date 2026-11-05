"""avrone-cli: run AVRONE headless from a terminal or a script.

    avrone-cli squad "build a todo app" [-n 4] [--dispatch local|network|hybrid]
    avrone-cli chat  "what's in the workspace?"
    avrone-cli deep  "is X true?" [--first-answer TEXT | --first-answer-file PATH] [-n 4] [--rounds 1..5]
    (squad and deep take --checkpoint PATH or --checkpoint-id ID to record progress and resume an interrupted run)
    avrone-cli tools
    avrone-cli runs [--limit 20]
    avrone-cli doctor [--strict]   (offline hosting preflight; starts nothing)

The markdown answer (or deep report) goes to stdout; live event lines go to
stderr unless --quiet; --json PATH writes the full result as JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .bootstrap import build_core, run_cleanups
from .codex import Event
from .config import Settings
from .trinity import clamp_deep_rounds


def _event_line(ev: Event) -> str:
    d = ev.data
    k = ev.kind
    if k == "run.start":
        detail = f"mode={d.get('mode')} size={d.get('squad_size', '-')}"
    elif k == "agent.spawn":
        detail = f"{d.get('role')}" + (f" (under {d['parent']})" if d.get("parent") else "")
    elif k == "tool.call":
        detail = f"{d.get('tool')} {json.dumps(d.get('args'), default=str)[:100]}"
    elif k == "tool.result":
        detail = str(d.get("output", "")).replace("\n", " ")[:100]
    elif k == "agent.done":
        detail = str(d.get("result", "")).replace("\n", " ")[:100]
    elif k == "lead.planned":
        detail = ", ".join(str(s.get("role")) for s in d.get("subtasks", []))
    elif k == "error":
        detail = str(d.get("message", ""))[:200]
    elif k == "run.done":
        detail = "done"
    else:
        detail = json.dumps(d, default=str)[:100]
    return f"[{ev.run_id}] {k:<14} {ev.agent:<18} {detail}"


async def _pump(q: asyncio.Queue[Event]) -> None:
    while True:
        ev = await q.get()
        print(_event_line(ev), file=sys.stderr, flush=True)


MAX_CLI_STEPS = 200
MAX_ANSWER_FILE_BYTES = 1024 * 1024


def _read_answer_file(path: str) -> str:
    """--first-answer-file: a regular file of at most 1 MB. A FIFO (or /dev/stdin with nothing
    piped) blocked forever, /dev/zero never finished, and a 2 GB file was read whole."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"--first-answer-file {path!r} is not a regular file")
    with p.open("rb") as f:
        data = f.read(MAX_ANSWER_FILE_BYTES + 1)
    if len(data) > MAX_ANSWER_FILE_BYTES:
        raise ValueError(f"--first-answer-file is larger than {MAX_ANSWER_FILE_BYTES} bytes")
    return data.decode("utf-8", errors="replace")


def _write_json(path: str | None, data: Any) -> None:
    if path:
        Path(path).write_text(json.dumps(data, indent=2, default=str))


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    if getattr(args, "max_steps", None) is not None:
        # --max-steps -5 used to give every agent zero turns (silent empty answers); 10**9 meant no cap
        settings.max_steps = max(1, min(MAX_CLI_STEPS, args.max_steps))
    core, cleanups = await build_core(settings, extras=not args.no_extras)
    pump = None
    q = None
    try:
        if args.cmd == "tools":
            items = core.tools.list()
            _write_json(args.json, items)
            for t in items:
                print(f"{t['name']:<28} [{t['source']}] {t['args']}\n    {t['description']}")
            return 0
        if args.cmd == "runs":
            items = core.codex.runs(args.limit)
            _write_json(args.json, items)
            for r in items:
                print(f"{r['run_id']}  {str(r.get('prompt', ''))[:90]}")
            return 0

        if args.cmd in ("telemetry", "map"):
            from .telemetry import system_map, telemetry, to_mermaid
            hours = args.hours
            if args.cmd == "telemetry":
                data = telemetry(core, hours * 3600)
                _write_json(args.json, data)
                print(json.dumps(data, indent=2, default=str))
            else:
                data = system_map(core, hours * 3600)
                _write_json(args.json, data)
                print(to_mermaid(data) if args.format == "mermaid" else json.dumps(data, indent=2))
            return 0

        if args.cmd == "evolve":
            from .evolve import list_accepted
            rows = list_accepted(core.settings)
            _write_json(args.json, rows)  # --json PATH was treated as a flag: rows printed, PATH never written
            print(("\n".join(f"{r['pid']} {r['branch']}  {r['goal']}  ({', '.join(r['files'])})" for r in rows)
                   or "no accepted proposals"))
            return 0

        if not args.quiet:
            q = core.codex.subscribe()
            pump = asyncio.create_task(_pump(q))
        # Only pass checkpoint when asked, so cores without the knob keep working.
        ck = {"checkpoint": args.checkpoint} if getattr(args, "checkpoint", None) else {}
        if getattr(args, "checkpoint_id", None):  # server-managed: <data_dir>/checkpoints/<id>.json
            from .checkpoint import check_compatible, managed_path, sanitize_client_id
            path = managed_path(core.settings.data_dir, sanitize_client_id(args.checkpoint_id))
            check_compatible(path, args.prompt, args.cmd)  # ValueError -> "avrone-cli: error: ..."
            ck = {"checkpoint": path}
        if args.cmd == "squad":
            dk = {"dispatch": args.dispatch} if getattr(args, "dispatch", None) else {}
            result = await core.squad(args.prompt, args.n, **ck, **dk)
            text = result["answer"]
        elif args.cmd == "chat":
            result = await core.chat(args.prompt)
            text = result["answer"]
        else:  # deep
            first = args.first_answer
            if args.first_answer_file:
                first = _read_answer_file(args.first_answer_file)
            # Only pass rounds when asked, so cores without the knob keep working.
            rounds = clamp_deep_rounds(args.rounds)
            kw = {"rounds": rounds} if rounds is not None else {}
            kw.update(ck)
            result = await core.deep(args.prompt, first, args.n, **kw)
            text = result["report"]
        await asyncio.sleep(0)  # let the pump print the last events
        _write_json(args.json, result)
        print(text)
        return 0
    finally:
        if pump:
            pump.cancel()
        if q is not None:
            core.codex.unsubscribe(q)
        await run_cleanups(cleanups)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="avrone-cli", description="Run AVRONE agents headless.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", metavar="PATH", help="write the full result as JSON to PATH")
    common.add_argument("--quiet", "-q", action="store_true", help="no live event lines on stderr")
    common.add_argument("--no-extras", action="store_true", help="skip extra tools, plugins and MCP servers")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("squad", parents=[common], help="parallel squad + lead merge")
    s.add_argument("prompt")
    s.add_argument("-n", type=int, default=None, help="squad size")
    s.add_argument("--max-steps", type=int, default=None)
    s.add_argument("--dispatch", choices=("local", "network", "hybrid"), default=None,
                   help="netsquad: place subtasks on fleet nodes (network), past the governor limit (hybrid) "
                        "or not at all (local); default AVRONE_SQUAD_DISPATCH")
    sg = s.add_mutually_exclusive_group()
    sg.add_argument("--checkpoint", metavar="PATH", default=None,
                    help="record progress to PATH; re-run with the same task and PATH to resume")
    sg.add_argument("--checkpoint-id", metavar="ID", default=None,
                    help="like --checkpoint, but managed by AVRONE under data_dir/checkpoints/ID.json "
                         "(the same id the API, MCP and fleet use; list with GET /api/checkpoints)")

    c = sub.add_parser("chat", parents=[common], help="one tool-using agent")
    c.add_argument("prompt")
    c.add_argument("--max-steps", type=int, default=None)

    d = sub.add_parser("deep", parents=[common], help="second-wave deep analysis of an answer")
    d.add_argument("prompt", help="the question")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--first-answer", metavar="TEXT", help="the answer to stress-test")
    g.add_argument("--first-answer-file", metavar="PATH", help="read the answer to stress-test from a file")
    d.add_argument("-n", type=int, default=None, help="number of reviewers (max 6)")
    d.add_argument("--rounds", type=int, default=None,
                   help="max deep waves (1-5, default AVRONE_DEEP_ROUNDS or 1); stops early on convergence")
    d.add_argument("--max-steps", type=int, default=None)
    dg = d.add_mutually_exclusive_group()
    dg.add_argument("--checkpoint", metavar="PATH", default=None,
                    help="record progress to PATH; re-run with the same task and PATH to resume")
    dg.add_argument("--checkpoint-id", metavar="ID", default=None,
                    help="like --checkpoint, but managed by AVRONE under data_dir/checkpoints/ID.json "
                         "(the same id the API, MCP and fleet use; list with GET /api/checkpoints)")

    sub.add_parser("tools", parents=[common], help="list registered tools")
    r = sub.add_parser("runs", parents=[common], help="list recent runs")
    r.add_argument("--limit", type=int, default=20)
    t = sub.add_parser("telemetry", parents=[common], help="run/agent/tool metrics from the action logs")
    t.add_argument("--hours", type=float, default=24)
    m = sub.add_parser("map", parents=[common], help="system map of AVRONE's own nodes and traffic")
    m.add_argument("--hours", type=float, default=24)
    m.add_argument("--format", choices=["mermaid", "json"], default="mermaid")
    sub.add_parser("evolve", parents=[common], help="list accepted self-evolution proposals (code AVRONE wrote for itself)")
    doc = sub.add_parser("doctor", parents=[common],
                         help="offline, read-only hosting preflight (python, paths, runner, docker, toggles, tokens)")
    doc.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    return p


def _doctor(args: argparse.Namespace) -> int:
    """Runs without build_core: nothing is started, created or contacted."""
    from .doctor import format_report, run_doctor
    report = run_doctor(strict=args.strict)
    _write_json(args.json, report)
    print(format_report(report))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "doctor":
        return _doctor(args)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as e:
        print(f"avrone-cli: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
