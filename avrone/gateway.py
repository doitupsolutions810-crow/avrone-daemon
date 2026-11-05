"""Gateway: route model calls across several providers with fallback + circuit breaking.

Primary upstream = the configured provider (AVRONE_PROVIDER). Add more with:

    AVRONE_LLM_FALLBACKS=openrouter:x-ai/grok-4,ollama:llama3.1

Each entry is provider[:model][|lead_model]; the provider's key comes from its
usual env var (see config.PROVIDERS). On a retryable error (429/5xx/network) the
gateway moves to the next healthy upstream. An upstream that fails repeatedly is
"opened" (skipped) for a cooldown, so a throttled provider stops being hammered.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import PROVIDERS, resolve_provider

log = logging.getLogger("avrone.gateway")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Tests may set this to an httpx transport.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


class LLMError(RuntimeError):
    pass


def is_loopback_url(url: str) -> bool:
    """True for http(s)://127.x / localhost / [::1] URLs: those must bypass any env proxy, or a
    sandbox/corporate HTTP_PROXY swallows calls to the local model server (ConnectError / 404)."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Upstream:
    provider: str
    base_url: str
    model: str
    api_key: str = ""
    lead_model: str = ""
    # breaker state
    fails: int = 0
    open_until: float = 0.0
    calls: int = 0
    errors: int = 0

    def healthy(self) -> bool:
        return time.time() >= self.open_until

    def record(self, ok: bool, threshold: int, cooldown: float) -> None:
        self.calls += 1
        if ok:
            self.fails = 0
            self.open_until = 0.0
        else:
            self.errors += 1
            self.fails += 1
            if self.fails >= threshold:
                self.open_until = time.time() + cooldown

    def status(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "state": "open" if not self.healthy() else "closed",
                "fails": self.fails, "calls": self.calls, "errors": self.errors,
                "reopen_in": round(max(0.0, self.open_until - time.time()), 1)}


def parse_upstreams(settings: Any, env: dict[str, str] | None = None) -> list[Upstream]:
    env = os.environ if env is None else env
    ups = [Upstream(settings.provider, settings.llm_base_url, settings.llm_model,
                    settings.llm_api_key, getattr(settings, "lead_model", "") or "")]
    for spec in (env.get("AVRONE_LLM_FALLBACKS", "") or "").split(","):
        spec = spec.strip()
        if not spec:
            continue
        head, _, lead = spec.partition("|")
        provider, _, model = head.partition(":")
        provider = provider.strip().lower()
        if provider not in PROVIDERS:
            continue
        try:
            name, url, mdl, key = resolve_provider(provider, None, model.strip() or None, None, env)
        except ValueError:
            continue
        lead = lead.strip()
        # A model id travels in every request body, the status page and the logs: one with a NUL,
        # newline or 5000 chars (a pasted blob, a broken template) used to become an upstream that
        # failed every call and filled /api/gateway. The same spec twice made two upstreams.
        if not _MODEL_RE.fullmatch(mdl) or (lead and not _MODEL_RE.fullmatch(lead)):
            log.warning("AVRONE_LLM_FALLBACKS: skipping %s spec with an invalid model id", name)
            continue
        if any(u.provider == name and u.model == mdl and u.lead_model == lead for u in ups):
            continue
        if len(ups) > MAX_FALLBACKS:
            log.warning("AVRONE_LLM_FALLBACKS: more than %d fallbacks; the rest are ignored", MAX_FALLBACKS)
            break
        ups.append(Upstream(name, url, mdl, key, lead))
    return ups


MAX_FALLBACKS = 16
_MODEL_RE = re.compile(r"[\x21-\x7e]{1,200}")


class Gateway:
    def __init__(self, settings: Any):
        self.s = settings
        self.upstreams = parse_upstreams(settings)
        self.threshold = int(getattr(settings, "breaker_threshold", 3))
        self.cooldown = float(getattr(settings, "breaker_cooldown", 30))
        self.last_route: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"upstreams": [u.status() for u in self.upstreams], "last_route": self.last_route}

    async def _request(self, up: Upstream, messages: list[dict[str, str]], model: str | None,
                       temperature: float, session: str | None) -> str:
        headers = {"Content-Type": "application/json"}
        if up.api_key:
            headers["Authorization"] = f"Bearer {up.api_key}"
        if session and up.provider in {"openclaw", "custom"}:
            headers["x-openclaw-session-key"] = session
        if up.provider == "openrouter":
            headers["X-Title"] = "AVRONE"
        body = {"model": model or up.model, "messages": sanitize_messages(messages),
                "temperature": safe_temperature(temperature)}
        url = up.base_url.rstrip("/") + "/chat/completions"
        kw: dict[str, Any] = {"timeout": self.s.llm_timeout}
        if HTTP_TRANSPORT is not None:
            kw["transport"] = HTTP_TRANSPORT
        if is_loopback_url(url):
            kw["trust_env"] = False  # a local Ollama/LM Studio is never reached through HTTP(S)_PROXY
        async with httpx.AsyncClient(**kw) as client:
            r = await _bounded_post(client, url, headers, body, up.provider)
        if r.status_code in RETRY_STATUS:
            raise _Retry(f"{up.provider} {r.status_code}: {r.text[:300]}", r)
        if r.status_code >= 400:
            # Permanent for this upstream (bad request, auth): don't retry it, but fail over.
            raise _Permanent(f"{up.provider} {r.status_code}: {r.text[:300]}")
        try:
            choice, msg = extract_choice(r.text)
            content = msg.get("content") or ""
        except _UpstreamError as e:
            if e.permanent:
                raise _Permanent(f"{up.provider} 200: {e}") from e
            raise _Retry(f"{up.provider} 200: {e}", r) from e
        except RecursionError as e:  # absurdly nested JSON: escaped chat() as a raw RecursionError
            raise _Permanent(f"{up.provider} 200: response JSON nested too deeply") from e
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            raise _Retry(f"{up.provider} bad response: {r.text[:200]}", r) from e
        content = content_text(content)
        if content.strip() and not _is_preamble(content, msg.get("tool_calls")):
            return content
        # gpt-oss on Ollama: the action sometimes comes back as a parsed native tool call with
        # empty content. AVRONE's actions are JSON text, so rewrite it into that form instead of
        # handing the agent an empty reply.
        try:
            synth = tool_calls_as_action(msg.get("tool_calls"), choice.get("finish_reason"))
        except RecursionError as e:  # tool-call arguments nested 100k deep
            raise _Permanent(f"{up.provider} 200: tool-call arguments nested too deeply") from e
        if synth:
            return synth
        if content.strip():
            return content  # preamble whose tool calls were unusable: hand back the text after all
        refusal = msg.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return refusal
        if choice.get("finish_reason") == "length" and msg.get("tool_calls"):
            raise _Permanent(f"{up.provider} 200: tool call cut off mid-arguments (finish_reason=length); "
                             f"nothing ran. Raise max tokens or the context window for this model")
        if choice.get("finish_reason") == "length":
            # Every token went to reasoning: retrying gives the same result. Say why plainly.
            raise _Permanent(f"{up.provider} 200: empty reply, output truncated (finish_reason=length); "
                             f"the context window or max tokens is too small for this model")
        return content

    async def chat(self, messages: list[dict[str, str]], model: str | None = None,
                   temperature: float = 0.7, session: str | None = None, lead: bool = False) -> str:
        if getattr(self.s, "llm_mode", "live") == "mock":
            from .llm import _mock_reply
            self.last_route = ["mock"]
            return _mock_reply(messages)
        route: list[str] = []
        errors: list[str] = []
        healthy = [u for u in self.upstreams if u.healthy()] or self.upstreams  # all open: try anyway
        nudge = False
        for up in healthy:
            use_model = model or (up.lead_model if lead and up.lead_model else up.model)
            per_up_tries = max(1, self.s.llm_retries) if up is healthy[0] else 1
            for attempt in range(per_up_tries):
                try:
                    msgs = with_plain_text_nudge(messages) if nudge else messages
                    out = await self._request(up, msgs, use_model, temperature, session)
                    up.record(True, self.threshold, self.cooldown)
                    route.append(f"{up.provider}:ok")
                    self.last_route = route
                    return out
                except _Permanent as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(str(e))
                    break  # no point retrying this upstream; fail over to the next
                except _Retry as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(str(e))
                    r = e.response
                    if r is not None and is_tool_parse_error(r.status_code, r.text):
                        nudge = True
                    if attempt < per_up_tries - 1:
                        await asyncio.sleep(_delay(e.response, self.s.llm_backoff, attempt))
                except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
                    # A malformed base URL fails identically every time: no retry, fail over.
                    # (InvalidURL is not an httpx.HTTPError, so before round 23 it escaped chat()
                    # as a raw exception instead of an LLMError.)
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(f"{up.provider} bad base URL {up.base_url!r}: {e}")
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(f"{up.provider} unreachable: {e}")
                except httpx.HTTPError as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(f"{up.provider} {type(e).__name__}: {e}")
            route.append(f"{up.provider}:fail")
        self.last_route = route
        raise LLMError(f"All upstreams failed ({' -> '.join(route)}): {' | '.join(errors[-4:])}")


# gpt-oss on Ollama: when the model writes its answer into the "commentary"/tool channel, Ollama
# tries to parse it as a native tool call and answers 500 ("error parsing tool call ..."). AVRONE
# never sends native tools; its actions are JSON in plain message text. Re-sending the identical
# request often fails the same way, so the retry carries an explicit plain-text instruction.
TOOL_PARSE_ERR = ("parsing tool call", "parse tool call", "tool call parsing", "tool_call",
                  "harmony", "unexpected end of json input")
PLAIN_TEXT_NUDGE = ("IMPORTANT: do NOT use native function/tool calling. Write your whole reply, "
                    "including any JSON action object, as ordinary message text in the final answer.")


def is_tool_parse_error(status: int, text: str) -> bool:
    t = (text or "").lower()
    return status >= 500 and any(k in t for k in TOOL_PARSE_ERR)


def with_plain_text_nudge(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """A copy of messages whose system prompt carries the plain-text instruction (never mutates)."""
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        if PLAIN_TEXT_NUDGE not in str(out[0].get("content", "")):
            out[0]["content"] = f"{out[0].get('content') or ''}\n\n{PLAIN_TEXT_NUDGE}".strip()
    else:
        out.insert(0, {"role": "system", "content": PLAIN_TEXT_NUDGE})
    return out


def _tool_name(raw: Any) -> str:
    name = str(raw or "").strip()
    # gpt-oss namespaces: "functions.shell", "repo_browser.shell", "assistant<|channel|>commentary to=functions.shell"
    name = name.split("to=")[-1].strip()
    name = name.rsplit(".", 1)[-1]
    name = name.split("<|", 1)[0].strip()
    return name


FINAL_TOOL_NAMES = {"final", "final_answer", "finalanswer"}
_FINAL_KEYS = ("final", "answer", "text", "content", "response", "result")
_HARMONY = re.compile(r"<\|[a-z_]{1,24}\|>")


_THINK = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)


def content_text(content: Any) -> str:
    """message.content -> text. OpenAI-style content parts ([{"type": "text", "text": ...}])
    are joined; non-text parts (images, audio) are skipped. Before round 20 a parts list was
    str()-ed into a Python repr and handed to the agent as its final answer."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, (list, tuple)):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, dict):
                    t = t.get("value")
                if isinstance(t, str) and part.get("type", "text") in ("text", "output_text", "input_text"):
                    out.append(t)
        return "".join(out)
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return str(content)


def _is_preamble(content: str, tool_calls: Any) -> bool:
    """Prose beside a native tool call is gpt-oss's commentary ("Let me list the files."),
    not a final answer. AVRONE treats prose as final, so the tool call wins unless the text
    itself carries a JSON action."""
    if not (isinstance(tool_calls, list) and tool_calls):
        return False
    text = _HARMONY.sub("", _THINK.sub("", content))
    return "{" not in text


def _parse_args(raw: Any, _depth: int = 0) -> tuple[Any, bool]:
    """Tool-call arguments -> (args, complete). complete=False means the JSON is cut off or
    unreadable. gpt-oss leaks harmony tokens (<|call|>) or trailing junk after a complete
    object; the first complete object is used then."""
    if raw is None:
        return {}, True
    if not isinstance(raw, str):
        return raw, True
    text = _HARMONY.sub("", raw).strip()
    if not text:
        return {}, True
    try:
        val = json.loads(text)
        if isinstance(val, str) and val.strip()[:1] in ("{", "[", '"') and _depth < 3:
            # double-encoded: "{\"cmd\": \"ls\"}"
            inner, ok = _parse_args(val, _depth + 1)
            if ok:
                return inner, True
        return val, True
    except ValueError:
        pass
    start = text.find("{")
    if start >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(text, start)
            if isinstance(obj, dict):
                return obj, True
        except ValueError:
            pass
    return {"_raw": raw}, False


def _final_from(args: Any) -> Any:
    if isinstance(args, dict):
        for k in _FINAL_KEYS:
            if k in args:
                return args[k]
        if len(args) == 1:
            return next(iter(args.values()))
        return args
    return args


def tool_calls_as_action(tool_calls: Any, finish_reason: Any = None) -> str:
    """OpenAI-style message.tool_calls -> AVRONE's JSON action text ("" when unusable).

    Round 19 guards: a call whose arguments were cut off by finish_reason=length is never
    dispatched (a half-written shell command must not run); a nameless call whose arguments
    are themselves an action ({"tool": ...} / {"final": ...}) is used as that action; a call
    named final/final_answer becomes {"final": ...}; exact duplicate calls run once."""
    if not isinstance(tool_calls, list):
        return ""
    truncated = finish_reason == "length"
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        args, complete = _parse_args(fn.get("arguments"))
        if not complete and truncated:
            continue  # cut off mid-arguments: never run it
        name = _tool_name(fn.get("name"))
        action: dict[str, Any]
        if not name:
            if isinstance(args, dict) and isinstance(args.get("tool"), str) and args["tool"].strip():
                action = {"tool": _tool_name(args["tool"]), "args": args.get("args", {})}
            elif isinstance(args, dict) and "final" in args:
                action = {"final": args["final"]}
            else:
                continue
        elif name.lower() in FINAL_TOOL_NAMES:
            action = {"final": _final_from(args)}
        else:
            if not isinstance(args, dict):
                args = {"_raw": args}
            action = {"tool": name, "args": args}
        key = json.dumps(action, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    if not actions:
        return ""
    finals = [a for a in actions if "final" in a]
    if finals and len(actions) > 1:
        actions = [a for a in actions if "final" not in a]  # tools first; the agent finishes later
    return json.dumps(actions[0] if len(actions) == 1 else {"tools": actions}, default=str)


_ROLES = {"system", "user", "assistant", "tool"}
_ROLE_ALIASES = {"developer": "system", "human": "user", "ai": "assistant", "model": "assistant",
                 "bot": "assistant", "function": "tool"}
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def clean_text(text: Any) -> str:
    """Non-text input reads as its str() (a None/int/list used to raise TypeError in the regex).
    Make text safe to JSON-encode as UTF-8 and to tokenize: lone surrogates (from bytes
    decoded with surrogateescape, or a cut emoji) become U+FFFD instead of raising
    UnicodeEncodeError inside httpx; NUL bytes are dropped (llama.cpp/Ollama and many proxies
    choke on them)."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if _SURROGATE.search(text):
        text = _SURROGATE.sub("\ufffd", text)
    if "\x00" in text:
        text = text.replace("\x00", "")
    return text


def sanitize_messages(messages: Any) -> list[dict[str, Any]]:
    """The outbound chat history, made acceptable to any OpenAI-compatible server.
    Before round 22 one lone surrogate in a tool result crashed the request locally (not
    retryable, every upstream the same), a None content was sent as null (Ollama 400), and a
    tool-role message without tool_call_id was rejected by strict servers."""
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, (list, tuple)):
        messages = [] if messages is None else [{"role": "user", "content": str(messages)}]
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, str):
            m = {"role": "user", "content": m}
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user").strip().lower()
        role = _ROLE_ALIASES.get(role, role)
        if role not in _ROLES:
            role = "user"
        raw = m.get("content")
        if isinstance(raw, str):
            content = raw
        elif raw is None:
            content = ""
        elif isinstance(raw, (list, tuple, dict)) and content_text(raw):
            content = content_text(raw)
        elif isinstance(raw, (dict, list, tuple)):
            content = json.dumps(raw, default=str, ensure_ascii=False)
        else:
            content = content_text(raw)
        content = clean_text(content)
        clean: dict[str, Any] = {"role": role, "content": content}
        if role == "tool":
            tcid = m.get("tool_call_id")
            # the id travels on the wire too: a lone surrogate or NUL in it (an id echoed back from a
            # junk tool call) crashed the request locally just like one in the content. Clean it
            # BEFORE the emptiness check, or "\x00\n" passes the check and goes out as "\n".
            tcid = clean_text(tcid)[:256].strip() if isinstance(tcid, str) else ""
            if tcid:
                clean["tool_call_id"] = tcid
            else:
                clean = {"role": "user", "content": content if content.startswith("TOOL RESULT")
                         else f"TOOL RESULT:\n{content}"}
        name = m.get("name")
        if isinstance(name, str) and _NAME_OK.match(name) and role in ("user", "assistant", "system"):
            clean["name"] = name
        out.append(clean)
    return out


def safe_temperature(t: Any, default: float = 0.7) -> float:
    if isinstance(t, bool):
        return default
    try:
        v = float(t)
    except (TypeError, ValueError, OverflowError):  # 10**400 raised before the request was sent
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return min(2.0, max(0.0, v))


class _UpstreamError(ValueError):
    def __init__(self, msg: str, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent


_PERMANENT_ERR = ("not found", "does not exist", "unknown model", "invalid api key", "unauthorized",
                  "invalid_api_key", "model_not_found", "no such model", "pull the model")


def _error_text(err: Any) -> str:
    if isinstance(err, dict):
        for k in ("message", "error", "detail", "msg"):
            if isinstance(err.get(k), str) and err[k].strip():
                return err[k].strip()
            if isinstance(err.get(k), dict):
                return _error_text(err[k])
        return json.dumps(err, default=str)[:300]
    return str(err).strip()[:300]


def _sse_message(text: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """A proxy that forces streaming answers with SSE ("data: {...}" lines). Join the deltas."""
    parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish = None
    seen = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except (ValueError, RecursionError):
            continue
        if isinstance(chunk, dict) and chunk.get("error") is not None:
            raise _UpstreamError(_error_text(chunk["error"]),
                                 any(w in _error_text(chunk["error"]).lower() for w in _PERMANENT_ERR))
        chs = chunk.get("choices") if isinstance(chunk, dict) else None
        if not (isinstance(chs, list) and chs and isinstance(chs[0], dict)):
            continue
        seen = True
        ch = chs[0]
        finish = ch.get("finish_reason") or finish
        d = ch.get("delta") or ch.get("message") or {}
        if not isinstance(d, dict):
            continue
        if isinstance(d.get("content"), str):
            parts.append(d["content"])
        for i, tc in enumerate(d.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", i) if isinstance(tc.get("index", i), int) else i
            slot = calls.setdefault(idx, {"function": {"name": "", "arguments": ""}})
            fn = tc.get("function") or {}
            if isinstance(fn, dict):
                if isinstance(fn.get("name"), str):
                    slot["function"]["name"] += fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["function"]["arguments"] += fn["arguments"]
                elif isinstance(fn.get("arguments"), dict):
                    slot["function"]["arguments"] = fn["arguments"]
    if not seen:
        return None
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(parts)}
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    return {"finish_reason": finish, "message": msg}, msg


def extract_choice(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A 200 body -> (choice, message). Accepts the OpenAI chat shape plus what other
    "OpenAI-compatible" servers send: legacy completions (choices[0].text), a single streamed
    chunk (choices[0].delta), Ollama's native /api/chat ({"message": ...}), an SSE stream, and a
    200 carrying {"error": ...} (surfaced instead of "bad response"; model-not-found and auth
    errors are permanent)."""
    body = text.lstrip("\ufeff").strip()
    if body.startswith("data:") or "\ndata:" in body[:2000]:
        got = _sse_message(body)
        if got is not None:
            return got
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("response is not an object")
    if data.get("error") is not None and not data.get("choices"):
        err = _error_text(data["error"])
        raise _UpstreamError(err or "error", any(w in err.lower() for w in _PERMANENT_ERR))
    choices = data.get("choices")
    if choices is None and isinstance(data.get("message"), dict):  # Ollama /api/chat
        msg = data["message"]
        return {"message": msg, "finish_reason": data.get("done_reason")}, msg
    choice = choices[0]
    if not isinstance(choice, dict):
        raise TypeError("choice is not an object")
    msg = choice.get("message")
    if msg is None and isinstance(choice.get("delta"), dict):
        msg = choice["delta"]
    if msg is None and isinstance(choice.get("text"), str):
        msg = {"role": "assistant", "content": choice["text"]}
    if not isinstance(msg, dict):
        raise TypeError("message is not an object")
    return choice, msg


MAX_RESPONSE_BYTES = 32 * 1024 * 1024


async def _bounded_post(client: httpx.AsyncClient, url: str, headers: dict[str, str], body: Any,
                        provider: str) -> httpx.Response:
    """POST and read at most MAX_RESPONSE_BYTES of the (decoded) body. Before: the whole body was
    buffered, so a 300 MB gzip stream (a few hundred KB on the wire) was inflated in memory, and a
    200 MB "content" string was handed to the agent as its reply."""
    if not hasattr(client, "stream"):  # a minimal stand-in client (tests, shims): plain post, then cap
        r = await client.post(url, headers=headers, json=body)
        if len(r.content) > MAX_RESPONSE_BYTES:
            raise _Permanent(f"{provider} {r.status_code}: response larger than {MAX_RESPONSE_BYTES} bytes")
        return r
    async with client.stream("POST", url, headers=headers, json=body) as resp:
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > MAX_RESPONSE_BYTES:
                raise _Permanent(f"{provider} {resp.status_code}: response larger than "
                                 f"{MAX_RESPONSE_BYTES} bytes; not read further")
        hdrs = [(k, v) for k, v in resp.headers.multi_items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(resp.status_code, headers=hdrs, content=bytes(buf), request=resp.request)


class _Retry(RuntimeError):
    def __init__(self, msg: str, response: httpx.Response | None = None):
        super().__init__(msg)
        self.response = response


class _Permanent(RuntimeError):
    pass


def retry_after_seconds(value: Any, now: float | None = None) -> float | None:
    """Retry-After as delay-seconds or an HTTP-date (RFC 9110). None when unusable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        secs = float(text)
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            import datetime as _dt
            when = when.replace(tzinfo=_dt.timezone.utc)
        secs = when.timestamp() - (time.time() if now is None else now)
    if secs != secs:  # NaN
        return None
    return max(0.0, secs)


def _delay(r: httpx.Response | None, base: float, attempt: int) -> float:
    delay = base * (2 ** attempt)
    if r is not None:
        ra = retry_after_seconds(r.headers.get("retry-after"))
        if ra is not None:
            delay = max(delay, min(ra, 30.0))
    return delay
