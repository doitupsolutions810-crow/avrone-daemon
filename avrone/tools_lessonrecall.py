"""lesson_recall: read back what prior runs learned (the read half of the recursive learning loop).

    lesson_recall {"topic"?: str, "limit"?: 1..50}
        topic  keywords; lessons sharing words with it rank first (the same overlap scoring
               avrone.bridge.relevant_lessons uses to inject lessons into new runs), newest
               first on ties. With no topic (or only stopwords) the newest lessons are listed.
        limit  how many to return (default 10, clamped 1..50)

Read-only: opens data_dir/memory.jsonl and the ``lessons`` namespace, never writes. Each line
shows the lesson's age, key, the topic/task it came from, and a digest of its text with
secret-shaped tokens masked. Output is bounded to LESSON_RECALL_MAX_CHARS.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from . import bridge as bridgemod
from .tools import Tool
from .tools import off_loop
from .tools_researchnote import redact_text

if TYPE_CHECKING:
    from .trinity import AgentContext

LESSON_RECALL_NAME = "lesson_recall"
LESSON_RECALL_DEFAULT = 10
LESSON_RECALL_MAX = 50
LESSON_RECALL_MAX_CHARS = 6000
LESSON_DIGEST = 300


AGE_MAX_S = 9999 * 86400  # round 131/132: a ts of 1e300 printed a 300-digit day count


FUTURE_SLACK_S = 300  # round 50: clock skew a peer-synced lesson may carry


def _ts(r: dict[str, Any], now: float | None = None) -> float:
    """A record's ts as a finite float, 0.0 for junk. Round 132: "inf"/NaN (a hand-edited or
    peer-synced store) sorted to the top forever, and NaN made the order arbitrary.
    Round 50: a finite ts far in the future (1e300, or a peer with its clock a year ahead) did the
    same thing and printed "0s ago"; anything past now + FUTURE_SLACK_S is junk too. A bool or a
    non-dict record is junk."""
    if not isinstance(r, dict):
        return 0.0
    raw = r.get("ts", 0)
    if isinstance(raw, bool):
        return 0.0
    try:
        v = float(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    if v > (time.time() if now is None else now) + FUTURE_SLACK_S:
        return 0.0
    return v


def age(seconds: float) -> str:
    # round 132: NaN raised ValueError and +-inf OverflowError out of int(); one corrupt lesson ts
    # failed the whole recall
    try:
        seconds = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return "?"
    if not math.isfinite(seconds):
        return "?"
    if seconds >= AGE_MAX_S:
        return f">{AGE_MAX_S // 86400}d"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def rank(recs: list[dict[str, Any]], topic: str) -> tuple[list[dict[str, Any]], bool]:
    """(ranked records, matched_by_topic)."""
    want = bridgemod._words(topic or "")
    now = time.time()
    recs = [r for r in recs if isinstance(r, dict)]  # round 50: a corrupt entry raised AttributeError

    def ts(r: dict[str, Any]) -> float:
        return _ts(r, now)
    if not want:
        return sorted(recs, key=lambda r: -ts(r)), False
    scored = [(len(want & bridgemod._words(str(r.get("value", "")))), r) for r in recs]
    scored = [x for x in scored if x[0]]
    scored.sort(key=lambda x: (-x[0], -ts(x[1])))
    return [r for _, r in scored], True


@off_loop
async def t_lesson_recall(ctx: "AgentContext", args: dict[str, Any]) -> str:
    topic = args.get("topic")
    if topic is not None and not isinstance(topic, str):
        # round 50: {"a": 1} was str()-ed, and its quote/brace-free words matched lessons
        return "ERROR: topic must be text"
    topic = (topic or "")[:500]
    lim = args.get("limit", LESSON_RECALL_DEFAULT)
    from .argnum import tool_int  # round 45: "²".isdigit() is True and int("²") raised; so did int(inf)
    lim = tool_int(lim, LESSON_RECALL_DEFAULT, 1, LESSON_RECALL_MAX, bad=None)
    if lim is None:
        return "ERROR: limit must be an integer 1..50"
    if not bridgemod.lessons_enabled():
        return "lessons are off on this node (AVRONE_LESSONS); nothing is learned or recalled"
    data_dir = getattr(getattr(getattr(ctx, "core", None), "settings", None), "data_dir", None)
    if data_dir is None:
        return "lesson_recall: no data_dir on this node, so no lessons are stored"
    try:
        recs = list(bridgemod._store(data_dir).load(bridgemod.LESSONS_NS).values())
    except OSError as e:
        return f"ERROR: could not read lessons ({type(e).__name__})"
    if not recs:
        return "lesson_recall: no lessons stored yet (runs distill conclusions; research_note adds findings)"
    recs = [r for r in recs if isinstance(r, dict)]
    if not recs:
        return "lesson_recall: no readable lessons stored (the store holds only corrupt entries)"
    ranked, by_topic = rank(recs, topic)
    if by_topic and not ranked:
        return f"lesson_recall: none of {len(recs)} stored lessons share words with that topic"
    now = time.time()
    head = (f"lesson_recall: {min(lim, len(ranked))} of {len(ranked)} "
            + ("matching" if by_topic else "stored") + f" lessons (of {len(recs)} total)"
            + (", best match first" if by_topic else ", newest first"))
    lines = [head]
    used = len(head)
    shown = 0
    for r in ranked[:lim]:
        t = _ts(r, now)
        when = age(now - t) if t else "?"
        from .tools import one_line  # round 134: \r / U+2028 forged extra lesson lines
        text = redact_text(one_line(r.get("value", "")))
        if len(text) > LESSON_DIGEST:
            text = text[:LESSON_DIGEST] + "..."
        src = redact_text(one_line(r.get("task", "") or ""))[:80]
        line = f"- [{when} ago] {one_line(r.get('key', '?'))}" + (f" ({src})" if src else "") + f": {text}"
        if used + len(line) + 1 > LESSON_RECALL_MAX_CHARS:
            lines.append(f"... {len(ranked[:lim]) - shown} more not shown (output cap)")
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    return "\n".join(lines)


LESSON_RECALL_TOOLS: list[Tool] = [
    Tool(LESSON_RECALL_NAME,
         "read-only: recall lessons prior runs learned (distilled conclusions and research_note findings). "
         "topic ranks by keyword overlap (newest breaks ties); no topic lists newest. limit 1..50 (default 10).",
         '{"topic"?: str, "limit"?: int}', t_lesson_recall, source="builtin",
         schema={"optional": {"topic": "str", "limit": "int"}}),
]


def register_lessonrecall_tools(registry) -> None:
    for t in LESSON_RECALL_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
