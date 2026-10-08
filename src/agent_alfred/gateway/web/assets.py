"""Packaged, explicitly named Dashboard assets; no filesystem URL mapping."""

from importlib.resources import files

PAGE_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)

ASSETS = {
    "/assets/models.js": ("models.js", "text/javascript"),
    "/assets/connections.js": ("connections.js", "text/javascript"),
    "/assets/settings.css": ("settings.css", "text/css"),
    "/assets/overview.js": ("overview.js", "text/javascript"),
    "/assets/overview.css": ("overview.css", "text/css"),
    "/assets/shell.js": ("shell.js", "text/javascript"),
    "/assets/routing-statistics.js": ("routing-statistics.js", "text/javascript"),

    "/assets/trace_export.js": ("trace_export.js", "text/javascript"),
    "/assets/topology.js": ("topology.js", "text/javascript"),
    "/assets/aggregation.js": ("aggregation.js", "text/javascript"),
    "/assets/tools.js": ("tools.js", "text/javascript"),
    "/assets/accounting.js": ("accounting.js", "text/javascript"),
    "/assets/app.js": ("app.js", "text/javascript"),
    "/assets/app.css": ("app.css", "text/css"),
    "/assets/stream.js": ("stream.js", "text/javascript"),
    "/assets/dom.js": ("dom.js", "text/javascript"),
    "/assets/progress.js": ("progress.js", "text/javascript"),
    "/assets/notices.js": ("notices.js", "text/javascript"),
    "/assets/pages.js": ("pages.js", "text/javascript"),
    "/assets/runs.js": ("runs.js", "text/javascript"),
    "/assets/memory.js": ("memory.js", "text/javascript"),
    "/assets/database.js": ("database.js", "text/javascript"),
}


def page_asset(path: str) -> tuple[bytes, str] | None:
    if path in {
        "/",
        "/overview",
        "/inbox",
        "/runs",
        "/memory",
        "/tools",
        "/ops",
        "/models",
        "/connections",
        "/behaviour",
        "/database",
    } or path.startswith("/runs/"):
        name, content_type = "index.html", "text/html"
    elif path in ASSETS:
        name, content_type = ASSETS[path]
    else:
        return None
    return (
        files("agent_alfred.ops").joinpath("static", name).read_bytes(),
        content_type + "; charset=utf-8",
    )
