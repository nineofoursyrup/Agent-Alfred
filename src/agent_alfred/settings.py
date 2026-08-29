"""Runtime settings loaded at host assembly. Defaults are injectable.

The production read path is :func:`load_settings`: environment variables (the
``AGENT_ALFRED_*`` family, see ``.env.example``) form the base layer, and the
CLI flags of ``agent-alfred`` override them. Parsing is fail-fast -- an illegal
value is a startup error, never a silent fallback to the default. Everything
resolves **before** Host assembly and is injected as the frozen
:class:`Settings`; nothing inside the Run loop re-reads the environment.

Persona priority (highest first):
1. ``--persona-file`` CLI flag,
2. ``AGENT_ALFRED_PERSONA_FILE`` environment variable,
3. the built-in ``DEFAULT_PERSONA`` when neither is set.

The persona file must be UTF-8 and non-empty; the system prompt is composed
from it plus the current local time at each request, and is never written to
the session record or the run transcript.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENDPOINT_ID = "opencode-go"
DEFAULT_MODEL_ID = "deepseek-v4-flash"
DEFAULT_WIRE_STYLE = "openai"
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_API_KEY_ENV = "OPENCODE_API_KEY"
STATE_DIR_ENV = "AGENT_ALFRED_HOME"
DEFAULT_STATE_DIRNAME = ".agent_alfred"
PROMPT_PREVIEW_MAX_CHARS = 240
LOOP_NODE_ID = "loop"
MAX_STEPS_REACHED_TEXT = (
    "Reached the configured max_steps limit; no further model request was sent."
)
CONTROLLED_FAILURE_TEXT = (
    "The model request failed; no assistant reply was produced."
)
OVERALL_DEADLINE_TEXT = (
    "The run overall deadline elapsed; no further model request was sent."
)
DEFAULT_PERSONA = (
    "You are Alfred, a local-first private AI assistant. "
    "Answer helpfully and concisely."
)

ENV_MAX_STEPS = "AGENT_ALFRED_MAX_STEPS"
ENV_MAX_TOKENS = "AGENT_ALFRED_MAX_TOKENS"
ENV_OVERALL_DEADLINE_S = "AGENT_ALFRED_OVERALL_DEADLINE_S"
ENV_PER_ATTEMPT_TIMEOUT_S = "AGENT_ALFRED_PER_ATTEMPT_TIMEOUT_S"
ENV_STREAM = "AGENT_ALFRED_STREAM"
ENV_STREAM_FALLBACK = "AGENT_ALFRED_STREAM_FALLBACK"
ENV_WORKING_MEMORY_ROUNDS = "AGENT_ALFRED_WORKING_MEMORY_ROUNDS"
ENV_PERSONA_FILE = "AGENT_ALFRED_PERSONA_FILE"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class Settings:
    max_steps: int = 8
    max_tokens: int | None = None
    overall_deadline_s: float | None = None
    per_attempt_timeout_s: float = 60.0
    stream: bool = False
    stream_fallback: bool = True
    working_memory_rounds: int = 20
    prompt_preview_max_chars: int = PROMPT_PREVIEW_MAX_CHARS
    persona: str = DEFAULT_PERSONA
    endpoint_id: str = DEFAULT_ENDPOINT_ID
    model_id: str = DEFAULT_MODEL_ID
    wire_style: str = DEFAULT_WIRE_STYLE
    api_key_env: str = OPENCODE_API_KEY_ENV


class SettingsError(ValueError):
    """A configuration value is illegal. Startup fails fast; no fallback."""


def _env_int(
    env: Mapping[str, str] | None,
    name: str,
    *,
    minimum: int | None,
    allow_zero: bool,
) -> int | None:
    raw = (env or {}).get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from None
    if value < 0 or (value == 0 and not allow_zero):
        boundary = ">= 0" if allow_zero else ">= 1"
        raise SettingsError(f"{name} must be {boundary}, got {value}")
    if minimum is not None and value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(env: Mapping[str, str] | None, name: str) -> float | None:
    raw = (env or {}).get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(f"{name} must be > 0, got {value}")
    return value


def _env_bool(env: Mapping[str, str] | None, name: str) -> bool | None:
    raw = (env or {}).get(name)
    if raw is None or not raw.strip():
        return None
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    allowed = ", ".join(sorted(_TRUE_WORDS | _FALSE_WORDS))
    raise SettingsError(f"{name} must be one of {allowed}, got {raw!r}")


def _read_persona_file(path_text: str) -> str:
    path = Path(path_text).expanduser()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SettingsError(
            f"persona file not found: {path_text}"
        ) from None
    except OSError as exc:
        raise SettingsError(
            f"persona file cannot be read: {path_text} ({type(exc).__name__})"
        ) from None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SettingsError(
            f"persona file is not valid UTF-8: {path_text}"
        ) from None
    if not content.strip():
        raise SettingsError(f"persona file is empty: {path_text}")
    return content


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    max_steps: int | None = None,
    max_tokens: int | None = None,
    overall_deadline_s: float | None = None,
    per_attempt_timeout_s: float | None = None,
    stream: bool | None = None,
    stream_fallback: bool | None = None,
    working_memory_rounds: int | None = None,
    persona_file: str | None = None,
) -> Settings:
    """Resolve the immutable Settings for host assembly. Fail-fast.

    Priority per knob: explicit argument (the CLI flag layer) > environment
    variable > the Settings default. There is no silent fallback: an illegal
    value raises :class:`SettingsError` naming the offending variable.
    ``environ=None`` means the real process environment -- the production
    read path.
    """
    import os

    env = os.environ if environ is None else environ
    env_max_steps = _env_int(env, ENV_MAX_STEPS, minimum=None, allow_zero=True)
    env_max_tokens = _env_int(env, ENV_MAX_TOKENS, minimum=1, allow_zero=False)
    env_overall = _env_float(env, ENV_OVERALL_DEADLINE_S)
    env_per_attempt = _env_float(env, ENV_PER_ATTEMPT_TIMEOUT_S)
    env_stream = _env_bool(env, ENV_STREAM)
    env_fallback = _env_bool(env, ENV_STREAM_FALLBACK)
    env_rounds = _env_int(
        env, ENV_WORKING_MEMORY_ROUNDS, minimum=None, allow_zero=True
    )

    resolved_max_steps = (
        max_steps if max_steps is not None else env_max_steps
    )
    if resolved_max_steps is not None and resolved_max_steps < 0:
        raise SettingsError(f"{ENV_MAX_STEPS} must be >= 0, got {resolved_max_steps}")

    resolved_max_tokens = (
        max_tokens if max_tokens is not None else env_max_tokens
    )
    if resolved_max_tokens is not None and resolved_max_tokens < 1:
        raise SettingsError(
            f"{ENV_MAX_TOKENS} must be >= 1, got {resolved_max_tokens}"
        )

    resolved_overall = (
        overall_deadline_s if overall_deadline_s is not None else env_overall
    )
    if resolved_overall is not None and resolved_overall <= 0:
        raise SettingsError(
            f"{ENV_OVERALL_DEADLINE_S} must be > 0, got {resolved_overall}"
        )

    resolved_per_attempt = (
        per_attempt_timeout_s
        if per_attempt_timeout_s is not None
        else env_per_attempt
    )
    if resolved_per_attempt is not None and resolved_per_attempt <= 0:
        raise SettingsError(
            f"{ENV_PER_ATTEMPT_TIMEOUT_S} must be > 0, got {resolved_per_attempt}"
        )

    resolved_rounds = (
        working_memory_rounds
        if working_memory_rounds is not None
        else env_rounds
    )
    if resolved_rounds is not None and resolved_rounds < 0:
        raise SettingsError(
            f"{ENV_WORKING_MEMORY_ROUNDS} must be >= 0, got {resolved_rounds}"
        )

    persona_path = (
        persona_file if persona_file is not None else env.get(ENV_PERSONA_FILE)
    )
    persona = DEFAULT_PERSONA
    if persona_path is not None and persona_path.strip():
        persona = _read_persona_file(persona_path)

    if stream is not None:
        resolved_stream = stream
    elif env_stream is not None:
        resolved_stream = env_stream
    else:
        resolved_stream = False
    if stream_fallback is not None:
        resolved_fallback = stream_fallback
    elif env_fallback is not None:
        resolved_fallback = env_fallback
    else:
        resolved_fallback = True

    return Settings(
        max_steps=8 if resolved_max_steps is None else resolved_max_steps,
        max_tokens=resolved_max_tokens,
        overall_deadline_s=resolved_overall,
        per_attempt_timeout_s=(
            60.0 if resolved_per_attempt is None else resolved_per_attempt
        ),
        stream=resolved_stream,
        stream_fallback=resolved_fallback,
        working_memory_rounds=(
            20 if resolved_rounds is None else resolved_rounds
        ),
        persona=persona,
    )


def resolve_state_dir(environ: dict[str, str] | None = None) -> Path:
    import os

    env = os.environ if environ is None else environ
    override = env.get(STATE_DIR_ENV)
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError(f"{STATE_DIR_ENV} must be an absolute path")
        return path
    return Path.home() / DEFAULT_STATE_DIRNAME
