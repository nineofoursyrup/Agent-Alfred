"""Shared construction seams for dashboard startup and CLI tests."""

from agent_alfred.model import ScriptedModel, ScriptedModelFactory


def file_database(directory):
    """Open the production database in the supplied state directory."""
    from agent_alfred.wiring import open_database

    return open_database(directory)


def scripted_factory(script=None):
    return ScriptedModelFactory(ScriptedModel(script or ["pong"]))
