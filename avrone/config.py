"""AVRONE configuration, read from environment variables (or a .env file).

Settings() reads the environment when it is constructed, so tests and the CLI
can change env vars and build a fresh Settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_dotenv_line(line: str) -> tuple[str, str] | None:
    """One .env line -> (key, value), or None. Handles `export KEY=v`, a BOM, CRLF, matching
    quotes (a ' #' inside quotes is kept), and trailing ' # comments' on unquoted values.
    Before round 23 `export KEY=v` set a variable literally named 'export KEY', and
    KEY="a #1" lost everything after the '#'."""
    line = line.lstrip("\ufeff").strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if "\x00" in line:
        # round 78: os.environ refuses a NUL ("embedded null byte"), and the ValueError was raised
        # at import time by _load_dotenv - one bad .env line kept the whole server from starting
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if not key or any(c.isspace() for c in key):
        return None
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end > 0:
            return key, value[1:end]
    value = value.split(" #", 1)[0].split("\t#", 1)[0]
    return key, _unquote(value)


DOTENV_MAX_BYTES = 4 * 1024 * 1024


def _load_dotenv(path: Path) -> None:
    from .safeio import open_read
    try:
        if not path.is_file():
            return
        # round 107: is_file() then read_text() blocked startup on a FIFO swapped in between and
        # read a multi-GB file whole; a symlinked .env is still followed
        with open_read(path, follow=True) as f:
            raw = f.read(DOTENV_MAX_BYTES + 1)
        if len(raw) > DOTENV_MAX_BYTES:
            return
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return
    # round 78: splitlines() also broke at U+2028, \x0c, \x1c-\x1e, \x85 ..., so a value holding
    # one was cut and its tail parsed as a line of its own; lines end at \n only (CRLF handled)
    for line in text.split("\n"):
        kv = parse_dotenv_line(line.rstrip("\r"))
        if kv:
            os.environ.setdefault(*kv)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8710


def _env_value(name: str, env: Any = None) -> str:
    raw = (os.environ if env is None else env).get(name)
    return raw.strip().strip("'\"").strip() if isinstance(raw, str) else ""


def bind_host(env: Any = None) -> str:
    """The address the HTTP server binds, as server.main, doctor and exposure_check all read it.

    Round 81: server.main passed AVRONE_HOST="" straight to uvicorn, and asyncio binds an empty
    host on EVERY interface (0.0.0.0 and ::), while doctor and exposure_check read the same blank
    value as 127.0.0.1 and reported the node as loopback-only. Blank, quoted-blank, non-ASCII or a
    value with spaces/control characters now means 127.0.0.1 for all three (fail closed)."""
    v = _env_value("AVRONE_HOST", env)
    if not v:
        return DEFAULT_HOST
    if not v.isascii() or not v.isprintable() or any(c.isspace() for c in v):
        import sys
        print(f"AVRONE: ignoring unusable AVRONE_HOST {v[:40]!r}; binding {DEFAULT_HOST}", file=sys.stderr)
        return DEFAULT_HOST
    return v


def bind_port(env: Any = None) -> int:
    """AVRONE_PORT as 1..65535. Round 81: "" raised ValueError out of server.main, "0" bound a
    random port nobody could find, and int() took fullwidth digits ("\uff18\uff17\uff11\uff10")."""
    v = _env_value("AVRONE_PORT", env)
    if not v:
        return DEFAULT_PORT
    if v.isascii() and v.isdigit() and 1 <= int(v) <= 65535:
        return int(v)
    import sys
    print(f"AVRONE: ignoring bad AVRONE_PORT {v[:40]!r}; using {DEFAULT_PORT}", file=sys.stderr)
    return DEFAULT_PORT


def env_file() -> Path:
    """AVRONE_ENV_FILE, or .env. Round 78: a blank value was Path("") (the cwd itself, never a
    file), so AVRONE_ENV_FILE= silently loaded no .env at all."""
    raw = os.getenv("AVRONE_ENV_FILE")
    if raw is None or not raw.strip():
        raw = ".env"
    return Path(_unquote(raw)).expanduser()


_load_dotenv(env_file())


# ------------------------------------------------------------ provider presets
# name -> (base_url, default model, env var holding that provider's key)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "openclaw": ("http://127.0.0.1:18789/v1", "openclaw", "OPENCLAW_GATEWAY_TOKEN"),
    "xai": ("https://api.x.ai/v1", "grok-4", "XAI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4.1", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "x-ai/grok-4", "OPENROUTER_API_KEY"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:3b", ""),
    "custom": ("", "default", ""),
}


def resolve_provider(provider: str | None, base_url: str | None = None, model: str | None = None,
                     api_key: str | None = None, env: dict[str, str] | None = None) -> tuple[str, str, str, str]:
    """Return (provider, base_url, model, api_key). Explicit values override the preset.

    An empty api key is allowed (a proxy may inject auth, or the backend needs none).
    """
    env = os.environ if env is None else env
    name = (provider or "openclaw").strip().lower()
    if name not in PROVIDERS:
        raise ValueError(f"Unknown AVRONE_PROVIDER {provider!r}. Choose one of: {', '.join(PROVIDERS)}")
    p_url, p_model, key_env = PROVIDERS[name]
    url = normalize_base_url(base_url or "", name) or p_url
    if not url:
        raise ValueError("AVRONE_PROVIDER=custom needs AVRONE_LLM_BASE_URL")
    key = clean_api_key(api_key if api_key else (env.get(key_env, "") if key_env else ""))
    model = _unquote(model or "") or p_model
    return name, url, model, key


_LOCAL_HOSTS = ("localhost", "127.", "0.0.0.0", "[::1]", "::1", "host.docker.internal", "10.", "192.168.")


def normalize_base_url(url: str, provider: str = "") -> str:
    """The OpenAI-compatible base URL, from what people actually paste:
    - quotes/whitespace, trailing slashes, and a full '.../chat/completions' endpoint are stripped
      (before round 23 that became '.../chat/completions/chat/completions', a 404);
    - a missing scheme gets http:// for local hosts and https:// otherwise (httpx refused it);
    - for Ollama, a bare 'http://host:11434' or the native '/api' or '/api/chat' path becomes '/v1'
      (the server answers 404 'page not found' on /chat/completions without it)."""
    u = _unquote(url or "")
    if not u:
        return ""
    if "://" not in u:
        host = u.split("/", 1)[0].lower()
        local = host.startswith(_LOCAL_HOSTS) or host.split(":")[0] in ("localhost",) or ":11434" in host
        u = ("http://" if local else "https://") + u
    u = u.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if u.lower().endswith(suffix):
            u = u[: -len(suffix)].rstrip("/")
            break
    scheme, _, rest = u.partition("://")
    host, slash, path = rest.partition("/")
    path = "/" + path if slash else ""
    is_ollama = provider == "ollama" or host.endswith(":11434")
    if is_ollama and path.lower() in ("", "/api", "/api/chat", "/api/generate"):
        path = "/v1"
    return f"{scheme.lower()}://{host}{path}"


def clean_api_key(key: str) -> str:
    """A pasted key with quotes, whitespace, newlines, or a leading 'Bearer ' (which made the
    header 'Bearer Bearer sk-...', a 401) is reduced to the bare token."""
    k = _unquote(key or "")
    if k[:7].lower() == "bearer " or k[:7].lower() == "bearer\t":
        k = k[7:]
    return "".join(k.split())  # no whitespace is valid inside a token; a newline breaks the header


def _env(name: str, default: str = ""):
    return lambda: os.getenv(name, default)


def _num(raw: str | None, default: float, cast: type) -> float:
    v = (raw or "").strip().strip("'\"").replace("_", "")
    if not v:
        return default
    try:
        if not v.isascii():  # round 78: float() took "\uff12" and Arabic-Indic digits
            raise ValueError
        f = float(v)
    except ValueError:
        import sys
        shown = repr(raw[:40] + ("..." if len(raw) > 40 else "")) if isinstance(raw, str) else repr(raw)
        print(f"AVRONE: ignoring bad number {shown}; using {default}", file=sys.stderr)
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return cast(f)


def _int(name: str, default: int):
    # Before round 23 AVRONE_LLM_TIMEOUT=30s or an empty AVRONE_LLM_RETRIES= raised ValueError at
    # import time (settings = Settings() below), so the whole server failed to start.
    return lambda: int(_num(os.getenv(name), default, int))


def _float(name: str, default: float):
    return lambda: float(_num(os.getenv(name), default, float))


TRUE_WORDS = frozenset({"1", "true", "yes", "on", "y", "enable", "enabled"})
FALSE_WORDS = frozenset({"0", "false", "no", "off", "n", "disable", "disabled", "none"})


def parse_bool(raw: str | None, default: bool, blank: bool | None = None) -> bool:
    """An on/off env value. Round 78: "TRUE " (trailing space), " 1", '"true"' were False, and a
    blank value (AVRONE_GOVERNOR=) turned a default-on setting off. Values are trimmed, unquoted
    and case-folded; blank means the default; an unknown word stays False (as before) but is
    reported, since an on/off switch must never be on by accident. ``blank`` overrides what a
    blank value means: permission switches (network, shell) pass blank=False, so an empty line
    keeps them off exactly as before round 78."""
    if raw is None:
        return default
    v = _unquote(raw).strip().lower()
    if not v:
        return default if blank is None else blank
    if v in TRUE_WORDS:
        return True
    if v not in FALSE_WORDS:
        import sys
        print(f"AVRONE: unknown on/off value {raw[:40]!r}; treating it as off", file=sys.stderr)
    return False


def _bool(name: str, default: bool, blank: bool | None = None):
    return lambda: parse_bool(os.getenv(name), default, blank)


def _path(name: str, default: str):
    """A directory setting. Round 77: AVRONE_DATA_DIR= (blank, as an .env line with no value
    leaves it) was Path("").resolve() - the process cwd - so data and the agent workspace landed in
    whatever directory the server was started from (for the workspace: the repo itself). Blank or
    whitespace now means the default, and a leading ~ is expanded instead of making a "~" folder."""
    def make() -> Path:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            raw = default
        return Path(raw).expanduser().resolve()
    return make


@dataclass
class Settings:
    # --- Model backend (OpenAI-compatible). Default: a local OpenClaw Gateway. ---
    # openclaw | xai | openai | openrouter | ollama | custom
    provider: str = field(default_factory=_env("AVRONE_PROVIDER", "openclaw"))
    # Empty = take from the provider preset. Explicit env values override the preset.
    llm_base_url: str = field(default_factory=_env("AVRONE_LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=_env("AVRONE_LLM_API_KEY"))
    llm_model: str = field(default_factory=_env("AVRONE_LLM_MODEL"))
    # Optional: a different model for the Lead (synthesizer) agents, e.g. a stronger one.
    lead_model: str = field(default_factory=_env("AVRONE_LEAD_MODEL"))
    # "mock" runs without any model, for demos and tests.
    llm_mode: str = field(default_factory=_env("AVRONE_LLM_MODE", "live"))
    llm_timeout: float = field(default_factory=_float("AVRONE_LLM_TIMEOUT", 180))
    llm_retries: int = field(default_factory=_int("AVRONE_LLM_RETRIES", 3))  # total tries on 429/5xx
    llm_backoff: float = field(default_factory=_float("AVRONE_LLM_BACKOFF", 1.0))  # seconds, doubles per try
    # --- Gateway: extra providers tried in order when the primary fails ---
    llm_fallbacks: str = field(default_factory=_env("AVRONE_LLM_FALLBACKS"))  # e.g. "openrouter:x-ai/grok-4,ollama:llama3.1"
    breaker_threshold: int = field(default_factory=_int("AVRONE_BREAKER_THRESHOLD", 3))
    breaker_cooldown: float = field(default_factory=_float("AVRONE_BREAKER_COOLDOWN", 30))

    # --- Orchestration (Grok Heavy style squad) ---
    squad_size: int = field(default_factory=_int("AVRONE_SQUAD_SIZE", 4))
    deep_squad_size: int = field(default_factory=_int("AVRONE_DEEP_SQUAD_SIZE", 4))
    max_parallel: int = field(default_factory=_int("AVRONE_MAX_PARALLEL", 6))  # concurrent LLM calls
    max_steps: int = field(default_factory=_int("AVRONE_MAX_STEPS", 6))
    max_spawn_depth: int = field(default_factory=_int("AVRONE_MAX_SPAWN_DEPTH", 2))
    # Wall-clock cap (seconds) on one agent's whole run; 0 = no cap. A worker that hits it is
    # reported as timed out and the squad merges the others instead of waiting forever.
    agent_timeout: float = field(default_factory=_float("AVRONE_AGENT_TIMEOUT", 0))
    # --- Health / thermal governor (avrone/governor.py): fewer parallel workers on a hot,
    # loaded or low-memory node. Advisory: never below min_parallel (>= 1).
    governor: bool = field(default_factory=_bool("AVRONE_GOVERNOR", True))
    min_parallel: int = field(default_factory=_int("AVRONE_MIN_PARALLEL", 1))
    thermal_max_c: float = field(default_factory=_float("AVRONE_THERMAL_MAX_C", 85))

    # --- Environment (Sovereign OS layer) ---
    workspace: Path = field(default_factory=_path("AVRONE_WORKSPACE", "./workspace"))
    runner: str = field(default_factory=_env("AVRONE_RUNNER", "auto"))  # auto | docker | local
    docker_image: str = field(default_factory=_env("AVRONE_DOCKER_IMAGE", "python:3.12-slim"))
    allow_network: bool = field(default_factory=_bool("AVRONE_ALLOW_NETWORK", True, blank=False))
    command_timeout: int = field(default_factory=_int("AVRONE_COMMAND_TIMEOUT", 120))

    # --- Access control ---
    admin_token: str = field(default_factory=_env("AVRONE_ADMIN_TOKEN"))
    # Rotation grace for /api/blackboard only. Kept verbatim: os.getenv does not strip.
    admin_token_previous: str = field(default_factory=_env("AVRONE_ADMIN_TOKEN_PREVIOUS"))
    agents_can_shell: bool = field(default_factory=_bool("AVRONE_AGENTS_CAN_SHELL", True, blank=False))

    # --- Eternal CodeX (persistent logs and memory) ---
    data_dir: Path = field(default_factory=_path("AVRONE_DATA_DIR", "./data"))

    def __post_init__(self) -> None:
        self.provider, self.llm_base_url, self.llm_model, self.llm_api_key = resolve_provider(
            self.provider, self.llm_base_url, self.llm_model, self.llm_api_key)
        bound_settings(self)


# round 78/79: field -> (lowest allowed, highest allowed, value used below the lowest).
# Before round 79 nothing bounded these: AVRONE_MAX_STEPS=0 gave every agent zero turns,
# AVRONE_LLM_TIMEOUT=0 or -1 failed every model call at once, AVRONE_LLM_BACKOFF=1e300 made the
# first retry sleep forever, AVRONE_BREAKER_COOLDOWN=1e300 opened a breaker for good.
SETTING_BOUNDS: dict[str, tuple[float, float, float]] = {
    "llm_timeout": (1, 3600, 180),
    "llm_retries": (1, 20, 1),
    "llm_backoff": (0, 60, 0),
    "breaker_threshold": (1, 1000, 1),
    "breaker_cooldown": (0, 3600, 0),
    "squad_size": (1, 64, 1),  # trinity still runs at most 8
    "deep_squad_size": (0, 64, 0),
    "max_parallel": (1, 4096, 1),
    "max_steps": (1, 1000, 1),
    "max_spawn_depth": (0, 16, 0),
    "agent_timeout": (0, 86400, 0),
    "min_parallel": (1, 4096, 1),
    "thermal_max_c": (1, 150, 85),
    "command_timeout": (1, 86400, 120),
}


def bound_setting(name: str, value: Any, default: Any = None) -> Any:
    """``value`` inside SETTING_BOUNDS[name]; a non-number, NaN or bool becomes the low fallback
    (or ``default`` when given), a value below the floor the fallback, above the ceiling the
    ceiling. Float fields come back as float, the rest as int."""
    lo, hi, below = SETTING_BOUNDS[name]
    fallback = below if default is None else default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        out = fallback
    elif value < lo:
        out = fallback
    elif value > hi:
        out = hi
    else:
        out = value
    return float(out) if name in _FLOAT_FIELDS else int(out)


_FLOAT_FIELDS = frozenset({"llm_timeout", "llm_backoff", "breaker_cooldown", "agent_timeout",
                           "thermal_max_c"})


def bound_settings(s: Any) -> None:
    for name in SETTING_BOUNDS:
        if hasattr(s, name):
            setattr(s, name, bound_setting(name, getattr(s, name)))


settings = Settings()
