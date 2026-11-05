"""lesson_forget + bridge_status: the pruning half of the recursive learning loop, and a view of
the run's conclude bridge.

    lesson_forget {"key": "l-<16 hex>", "reason": str}
        Removes ONE stored lesson that turned out wrong or stale, so later runs stop being primed
        with it. reason (<= 300 chars) is required: it is posted to the run bridge as a
        `correction` (siblings see what was retracted and why) and written to the codex audit.
        Refused when lessons are off, the key is malformed or unknown.

    bridge_status {}
        Read-only summary of THIS run's bridge: posts by kind and by agent, how many came from
        other nodes, open questions with no reply yet, conclusions, and the ring bound.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from . import bridge as bridgemod
from .tools import Tool, off_loop
from .tools_researchnote import redact_text

if TYPE_CHECKING:
    from .trinity import AgentContext

KEY_RE = re.compile(r"^l-[0-9a-f]{16}$")
REASON_MAX = 300
STATUS_MAX_AGENTS = 20
STATUS_MAX_CHARS = 4000


def _data_dir(ctx: "AgentContext") -> Any:
    return getattr(getattr(getattr(ctx, "core", None), "settings", None), "data_dir", None)


@off_loop
async def t_lesson_forget(ctx: "AgentContext", args: dict[str, Any]) -> str:
    key = str(args.get("key") or "").strip()
    reason = re.sub(r"[\x00-\x1f\x7f]", " ", str(args.get("reason") or "")).strip()
    if not KEY_RE.match(key):
        return "ERROR: key must look like l-<16 hex> (see lesson_recall)"
    if not reason:
        return "ERROR: reason is required (why this lesson is wrong or stale)"
    if len(reason) > REASON_MAX:
        return f"ERROR: reason is {len(reason)} chars; the cap is {REASON_MAX}"
    if not bridgemod.lessons_enabled():
        return "lessons are off on this node (AVRONE_LESSONS); nothing to forget"
    data_dir = _data_dir(ctx)
    if data_dir is None:
        return "lesson_forget: no data_dir on this node, so no lessons are stored"
    reason = redact_text(reason)
    try:
        with bridgemod.lessons_lock(data_dir):
            store = bridgemod._store(data_dir)
            old = store.load(bridgemod.LESSONS_NS).get(key)
            if old is None:
                return f"ERROR: no lesson {key} (lesson_recall lists them)"
            store.delete(bridgemod.LESSONS_NS, key)
    except OSError as e:
        return f"ERROR: could not update lessons ({type(e).__name__})"
    text = redact_text(str(old.get("value", "")))[:200]
    note = f"retracted lesson {key}: {text} | reason: {reason}"
    posted = ""
    try:
        p = bridgemod.for_core(ctx.core, ctx.run_id).post(getattr(ctx, "name", "agent"), "correction", note,
                                                          role=str(getattr(ctx, "role", "") or ""))
        posted = f" (correction [{p['id']}] posted to the run bridge)"
    except Exception:  # noqa: BLE001 - the forget already happened; the post is a courtesy
        posted = " (bridge post failed)"
    codex = getattr(ctx.core, "codex", None)
    if codex is not None:
        try:
            codex.emit(ctx.run_id, "lesson.forget", getattr(ctx, "name", "agent"), key=key, reason=reason)
        except Exception:  # noqa: BLE001
            pass
    return f"forgot lesson {key}{posted}"


def bridge_status(ctx: "AgentContext") -> dict[str, Any]:
    b = bridgemod.for_core(ctx.core, ctx.run_id)
    posts = b.posts()
    kinds: dict[str, int] = {}
    agents: dict[str, int] = {}
    remote = 0
    replied: set[str] = set()
    for p in posts:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        agents[p["agent"]] = agents.get(p["agent"], 0) + 1
        if p.get("origin_node") not in (None, "", b.node_id):
            remote += 1
        replied.update(p.get("refs") or [])
    open_q = [p for p in posts if p["kind"] == "question" and p["id"] not in replied]
    return {"run_id": b.run_id, "posts": len(posts), "limit": b.limit, "kinds": kinds,
            "agents": dict(sorted(agents.items(), key=lambda x: -x[1])[:STATUS_MAX_AGENTS]),
            "more_agents": max(0, len(agents) - STATUS_MAX_AGENTS), "remote": remote,
            "open_questions": [{"id": p["id"], "agent": p["agent"], "text": redact_text(p["text"])[:120]}
                               for p in open_q[-5:]],
            "open_question_count": len(open_q),
            "conclusions": sum(1 for p in posts if p["kind"] == "conclusion")}


async def t_bridge_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    st = bridge_status(ctx)
    if not st["posts"]:
        return f"bridge_status run {st['run_id']}: no posts yet (ring holds {st['limit']})"
    lines = [f"bridge_status run {st['run_id']}: {st['posts']}/{st['limit']} posts, "
             f"{st['remote']} relayed from other nodes, {st['conclusions']} conclusion(s)",
             "kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(st["kinds"].items())),
             "agents: " + ", ".join(f"{a}={n}" for a, n in st["agents"].items())
             + (f" (+{st['more_agents']} more)" if st["more_agents"] else "")]
    if st["open_question_count"]:
        lines.append(f"open questions (no reply yet): {st['open_question_count']}")
        lines += [f"- [{q['id']}] {q['agent']}: {q['text']}" for q in st["open_questions"]]
    else:
        lines.append("open questions: none")
    out = "\n".join(lines)
    return out if len(out) <= STATUS_MAX_CHARS else out[:STATUS_MAX_CHARS] + "\n...[truncated]"


LESSON_FORGET_TOOLS: list[Tool] = [
    Tool("lesson_forget", "retract ONE stored lesson that proved wrong or stale (key from lesson_recall); "
         "reason required, posted to the run bridge as a correction and audited.",
         '{"key": "l-<16 hex>", "reason": str}', t_lesson_forget, source="builtin",
         schema={"required": {"key": "str", "reason": "str"}}),
    Tool("bridge_status", "read-only: this run's bridge at a glance - posts by kind and agent, relayed posts, "
         "unanswered questions, conclusions.", "{}", t_bridge_status, source="builtin", schema={}),
]


def register_lessonforget_tools(registry) -> None:
    for t in LESSON_FORGET_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
