"""research_note: a long-horizon agent records a durable research note that feeds the learning loop.

    research_note {"text": str, "kind"?: "finding"|"conclusion"|"question", "topic"?: str}
        text   the note (<= 4000 characters; longer is refused, never silently cut)
        kind   finding (default), conclusion or question
        topic  optional short label (<= 200 chars); stored with the lesson and shown on the post

What it does
  (a) posts the note to this run's Bridge through the same path bridge_post uses
      (avrone.bridge.for_core(...).post), so sibling and sub-agents of the run see it; the
      Bridge's own bounds (AVRONE_BRIDGE_MAX_POSTS, 4000-char posts) apply;
  (b) for kind finding|conclusion, when the recursive learning loop is on (AVRONE_LESSONS, see
      avrone.bridge.lessons_enabled) and the core has a data_dir, writes the note into the
      ``lessons`` memstore namespace under the same key distill_lessons uses
      (lesson_key of the whitespace-normalised text cut to LESSON_TEXT chars). A note whose key is
      already stored is NOT written again: the reply says "already known". The namespace stays
      capped at AVRONE_LESSONS_MAX (oldest evicted), like distill_lessons.

Questions are posted only (an open question is not a lesson). Secret-shaped tokens in the text are
masked before anything is posted or stored. No network, no subprocess; the only writes are the
run's bridge file and data_dir/memory.jsonl.
"""
from __future__ import annotations

import re

from . import textsafe as _textsafe
from typing import TYPE_CHECKING, Any

from . import bridge as bridgemod
from . import secretshapes as _shapes
from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

RESEARCH_NOTE_NAME = "research_note"
RESEARCH_NOTE_KINDS = ("finding", "conclusion", "question")
RESEARCH_NOTE_MAX_TEXT = bridgemod.MAX_TEXT
RESEARCH_NOTE_MAX_TOPIC = 200
LEARNABLE_KINDS = ("finding", "conclusion")

# credential-shaped substrings (same families env_report/memory_peek refuse to show)
_TOKEN_RES = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)(?:sk|pk|rk|xai|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs])[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]+)?"),
    # round 50 (51): userinfo up to the LAST @ before the host; "u:p@ss@host" leaked "ss"
    re.compile(r"(?i)([a-z][a-z0-9+.\-]{0,31}://)[^/\s]+@"),
    _shapes.TOKEN_PREFIX_RE,  # round 152
)
_BLOB_RE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
REDACTED = "***redacted***"


# zero-width / word-joiner / soft hyphen / BOM: invisible, so a token split by one ("sk-\u200b...")
# slipped past every pattern while reading the same to a human (round 51)
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e]")


def redact_text(text: str) -> str:
    """Mask credential-shaped tokens inside free text; the rest of the text is kept.
    Round 51: invisible joiners are dropped first so they cannot split a token."""
    out = _INVISIBLE_RE.sub("", str(text))
    for rx in _TOKEN_RES:
        if rx.groups:
            out = rx.sub(lambda m: m.group(1) + "***@", out)
        else:
            out = rx.sub(REDACTED, out)

    def blob(m: "re.Match[str]") -> str:
        b = m.group(0)
        if any(c.isdigit() for c in b) and any(c.islower() for c in b) and any(c.isupper() for c in b):
            return REDACTED
        return b
    return _BLOB_RE.sub(blob, out)


def lesson_text(text: str) -> str:
    """The form distill_lessons stores (and hashes): whitespace collapsed, cut to LESSON_TEXT."""
    return re.sub(r"\s+", " ", str(text)).strip()[: bridgemod.LESSON_TEXT]


def learn(data_dir: Any, text: str, run_id: str, topic: str = "", kind: str = "finding",
          agent: str = "") -> tuple[str, str]:
    """Write one lesson unless its key exists. Returns (status, key); status is
    'learned' | 'known' | 'refused: <why>'."""
    t = lesson_text(text)
    key = bridgemod.lesson_key(t)
    from .memstore import MemoryRefused
    try:
        with bridgemod.lessons_lock(data_dir):  # same lock distill_lessons holds: one writer per key
            store = bridgemod._store(data_dir)
            if key in store.load(bridgemod.LESSONS_NS):
                return "known", key
            store.save(bridgemod.LESSONS_NS, key, t, run_id=str(run_id), task=str(topic)[:200],
                       kind=kind, agent=str(agent)[:80], source=RESEARCH_NOTE_NAME)
            store.trim(bridgemod.LESSONS_NS, bridgemod.lessons_max())
    except MemoryRefused as e:
        return f"refused: {e}", key
    except (OSError, ValueError, TypeError, AttributeError) as e:
        # round 51: a corrupt store (ValueError/AttributeError) raised AFTER the note was posted,
        # so the agent was told the whole tool failed and posted it again
        return f"refused: {type(e).__name__}", key
    return "learned", key


def _no_surrogates(s: str) -> str:
    """Lone surrogates -> U+FFFD, so the lesson store (UTF-8) can hold every note that was posted."""
    return re.sub("[\ud800-\udfff]", "\ufffd", s)


async def t_research_note(ctx: "AgentContext", args: dict[str, Any]) -> str:
    raw = args.get("text")
    if isinstance(raw, str):
        raw = _no_surrogates(raw)
    if not isinstance(raw, str) or not _INVISIBLE_RE.sub("", raw).strip():
        # round 51: zero-width-only text passed and posted an empty-looking note
        return "ERROR: text is required (a non-empty string)"
    if len(raw) > RESEARCH_NOTE_MAX_TEXT:
        return f"ERROR: text is {len(raw)} chars; the cap is {RESEARCH_NOTE_MAX_TEXT}"
    kind = args.get("kind")
    if kind is not None and not isinstance(kind, str):
        return "ERROR: kind must be one of " + ", ".join(RESEARCH_NOTE_KINDS)
    kind = "finding" if not kind else kind.strip().lower()
    if kind not in RESEARCH_NOTE_KINDS:
        return "ERROR: kind must be one of " + ", ".join(RESEARCH_NOTE_KINDS)
    topic = args.get("topic")
    if topic is not None and not isinstance(topic, str):
        return "ERROR: topic must be text"  # round 51: {"a": 1} was str()-ed into the label
    topic = "" if topic is None else " ".join(
        _textsafe.UNSAFE_ONE.sub(" ", _INVISIBLE_RE.sub("", _no_surrogates(topic))).split())
    if len(topic) > RESEARCH_NOTE_MAX_TOPIC:
        return f"ERROR: topic is longer than {RESEARCH_NOTE_MAX_TOPIC} chars"
    text = redact_text(raw.strip()).strip()
    masked = text != raw.strip()
    t2 = redact_text(topic)
    masked = masked or t2 != topic
    topic = t2
    if len(text) > RESEARCH_NOTE_MAX_TEXT:
        # round 51: masking can lengthen ("a@" -> "***@", "bearer x" -> "***redacted***"), and the
        # bridge silently cut the result at 4000; the note must never be cut without saying so
        return (f"ERROR: after masking secret-shaped tokens the note is {len(text)} chars; "
                f"the cap is {RESEARCH_NOTE_MAX_TEXT}")
    body = f"[{topic}] {text}" if topic else text
    if len(body) > RESEARCH_NOTE_MAX_TEXT:
        body = text  # the topic still travels with the lesson; never cut the note itself
    core = getattr(ctx, "core", None)
    b = bridgemod.for_core(core, getattr(ctx, "run_id", "run"))
    name, role = bridgemod._who(ctx)
    try:
        p = b.post(name, kind, body, role=role)
    except bridgemod.BridgeError as e:
        return f"ERROR: {e}"
    except OSError as e:  # round 51: a full disk / read-only bridge file raised out of the tool
        return f"ERROR: could not write the run bridge ({type(e).__name__})"
    lines = [f"posted {p['kind']} [{p['id']}] to the run bridge ({len(b)} posts)"
             + (" (secret-shaped tokens masked)" if masked else "")]
    if kind not in LEARNABLE_KINDS:
        lines.append("lessons: not stored (a question is posted only; answer it with a finding/conclusion)")
    elif not bridgemod.lessons_enabled():
        lines.append("lessons: off (AVRONE_LESSONS) - posted to the bridge only, nothing learned")
    else:
        data_dir = getattr(getattr(core, "settings", None), "data_dir", None)
        if data_dir is None:
            lines.append("lessons: no data_dir on this node - posted to the bridge only")
        else:
            status, key = learn(data_dir, text, b.run_id, topic=topic, kind=kind, agent=name)
            if status == "learned":
                lines.append(f"lessons: NEWLY LEARNED as {key} (future runs on related tasks will see it)")
            elif status == "known":
                lines.append(f"lessons: already known as {key} (same text stored before; not duplicated)")
            else:
                lines.append(f"lessons: not stored ({status})")
    from .tools import one_line
    preview = one_line(body)
    lines.append("note: " + (preview if len(preview) <= 300 else preview[:300] + "..."))
    return "\n".join(lines)


RESEARCH_NOTE_TOOLS: list[Tool] = [
    Tool(RESEARCH_NOTE_NAME,
         "record a durable research note: posts it to this run's shared bridge (siblings and sub-agents "
         "see it) and, for kind finding|conclusion, stores it as a lesson future runs recall (deduped by "
         "text hash; says whether it was newly learned or already known). kind finding (default)|"
         "conclusion|question; text <= 4000 chars; optional topic label. Secret-shaped tokens are masked.",
         '{"text": str, "kind"?: "finding"|"conclusion"|"question", "topic"?: str}',
         t_research_note, source="builtin",
         schema={"required": {"text": "str"}, "optional": {"kind": "str", "topic": "str"}}),
]


def register_researchnote_tools(registry) -> None:
    for t in RESEARCH_NOTE_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
