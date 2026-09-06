"""Public-entry assembly helpers for managed-state ownership tests."""

from __future__ import annotations

from pathlib import Path

from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.wiring import build_default_host


def build_standalone_host(state_dir: Path) -> RuntimeHost:
    """Assemble the real standalone public entry with a deterministic model."""
    return build_default_host(
        state_dir=state_dir,
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
