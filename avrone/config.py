"""AVRONE configuration, read from environment variables (or a .env file).

Settings() reads the environment when it is constructed, so tests and the CLI
can change env vars and build a fresh Settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    for line in text.splitlines():
        kv = parse_dotenv_line(line)
        if kv:
            os.environ.setdefault(*kv)


_load_dotenv(Path(os.getenv("AVRONE_ENV_FILE", ".env")))


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
        f = float(v)
    except ValueError:
        import sys
        print(f"AVRONE: ignoring bad number {raw!r}; using {default}", file=sys.stderr)
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


def _bool(name: str, default: bool):
    return lambda: os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: str):
    return lambda: Path(os.getenv(name, default)).resolve()


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
    allow_network: bool = field(default_factory=_bool("AVRONE_ALLOW_NETWORK", True))
    command_timeout: int = field(default_factory=_int("AVRONE_COMMAND_TIMEOUT", 120))

    # --- Access control ---
    admin_token: str = field(default_factory=_env("AVRONE_ADMIN_TOKEN"))
    agents_can_shell: bool = field(default_factory=_bool("AVRONE_AGENTS_CAN_SHELL", True))

    # --- Eternal CodeX (persistent logs and memory) ---
    data_dir: Path = field(default_factory=_path("AVRONE_DATA_DIR", "./data"))

    def __post_init__(self) -> None:
        self.provider, self.llm_base_url, self.llm_model, self.llm_api_key = resolve_provider(
            self.provider, self.llm_base_url, self.llm_model, self.llm_api_key)


settings = Settings()
