"""Durable routing evidence; semantic identities are independent of graph shape."""

METRICS_VERSION = "routing-statistics-1"
POLICY_VERSION = "message-routing-1"
ROUTES = ("quick", "full", "fallback", "no_action", "context_failure")


def admission(settings):
    enabled = settings.get("enabled")
    return dict(
        schema_version=1,
        enablement=(
            ("enabled" if enabled else "disabled")
            if settings.get("status") == "ok" and type(enabled) is bool
            else "unknown"
        ),
        metrics_version=METRICS_VERSION,
        policy_version=POLICY_VERSION,
    )


def initial_result():
    # Created with admission and persisted only by the existing finalizer.
    # An interrupted process never persists these negative observations.
    return dict(
        schema_version=1,
        route=None,
        decision_complete=True,
        graph_entered=False,
        fallback=False,
        bypass=False,
        recovered=False,
        blocked=None,
    )
