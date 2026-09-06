"""Shared construction seams for dashboard startup and CLI tests."""

from pathlib import Path, PurePath

from agent_alfred.model import ScriptedModel, ScriptedModelFactory


def scripted_factory(script=None):
    return ScriptedModelFactory(ScriptedModel(script or ["pong"]))


def managed_process_lock(directory):
    """Create a real ProcessLock whose root and file are centrally leased."""
    from agent_alfred.gateway.web.lifecycle import LOCK_NAME, ProcessLock
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(directory)
    try:
        file = state.open_regular(
            PurePath(LOCK_NAME),
            access="read_write",
            create=True,
            role="process lock",
        )
    except BaseException:
        state.close()
        raise
    return ProcessLock(file, owned_state=state)


def record_managed_state_acquires(monkeypatch) -> list[Path]:
    """Record public root acquisitions; each call owns a fresh result list."""
    from agent_alfred.managed_state import ManagedStateDirectory

    paths: list[Path] = []
    real = ManagedStateDirectory.acquire.__func__

    def recording(cls, path, _rollback=None):
        paths.append(Path(path))
        return real(cls, path, _rollback=_rollback)

    monkeypatch.setattr(
        ManagedStateDirectory, "acquire", classmethod(recording)
    )
    return paths
