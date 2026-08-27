"""Runtime settings loaded at host assembly. Defaults are injectable."""

from __future__ import annotations

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
DEFAULT_PERSONA = (
    "You are Alfred, a local-first private AI assistant. "
    "Answer helpfully and concisely."
)


@dataclass(frozen=True)
class Settings:
    max_steps: int = 8
    max_tokens: int | None = None
    overall_deadline_s: float | None = None
    per_attempt_timeout_s: float = 60.0
    stream_fallback: bool = True
    working_memory_rounds: int = 20
    prompt_preview_max_chars: int = PROMPT_PREVIEW_MAX_CHARS
    persona: str = DEFAULT_PERSONA
    endpoint_id: str = DEFAULT_ENDPOINT_ID
    model_id: str = DEFAULT_MODEL_ID
    wire_style: str = DEFAULT_WIRE_STYLE
    api_key_env: str = OPENCODE_API_KEY_ENV


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
