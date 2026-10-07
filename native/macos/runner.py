"""Fixed bundled entrypoint; no environment, argv or working-directory imports."""

from agent_alfred.evals.acceptance.controlled.native_probe import dispatch

dispatch()
