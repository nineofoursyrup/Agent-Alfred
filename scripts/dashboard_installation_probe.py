"""Installed HTTP closure checks, copied beside the isolated smoke runner."""

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


class DocumentResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.paths.add(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet":
            self.paths.add(values["href"])


def closure(package: Path):
    """Discover required files from HTML and actual module/CSS references."""
    static = package / "ops" / "static"
    document = DocumentResources()
    document.feed((static / "index.html").read_text())
    pending, found = set(document.paths), {}
    while pending:
        url = pending.pop()
        assert url.startswith("/assets/"), url
        path = static / url.removeprefix("/assets/")
        assert path.resolve().is_relative_to(static.resolve()), url
        data = path.read_bytes()
        found[url] = data
        code = data.decode()
        references = []
        if path.suffix == ".js":
            references.extend(
                re.findall(r"""(?:from\s*|import\s*(?:\(\s*)?)["']([^"']+)["']""", code)
            )
        elif path.suffix == ".css":
            references.extend(
                re.findall(r"""(?:url\(\s*|@import\s*)["']?([^\s"')]+)""", code)
            )
        for reference in references:
            parsed = urlsplit(reference)
            assert not parsed.scheme and not parsed.netloc, reference
            target = urljoin(url, reference)
            if target not in found:
                pending.add(target)
    return found


def verify_dashboard_http(origin: str, package: Path):
    from agent_alfred.gateway.web.assets import ASSETS, PAGE_POLICY

    resources = closure(package)
    assert resources.keys() == ASSETS.keys(), {
        "unregistered": sorted(resources.keys() - ASSETS.keys()),
        "unreachable": sorted(ASSETS.keys() - resources.keys()),
    }
    html = (package / "ops/static/index.html").read_bytes()
    routes = [
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
        "/?range=today",
        "/overview?range=7d",
        "/runs?filter=all",
        "/runs/opaque%20%2F%E8%BF%90%E8%A1%8C%3F?filter=system",
        "/ops?range=today&timezone=Asia%2FShanghai",
    ]
    proof = []
    for path, content in [(route, html) for route in routes] + sorted(
        resources.items()
    ):
        mime = (
            "text/html"
            if path in routes
            else "text/javascript"
            if path.endswith(".js")
            else "text/css"
        )
        if path in resources:
            assert ASSETS[path] == (path.removeprefix("/assets/"), mime)
        for method in ("GET", "HEAD"):
            with urlopen(Request(origin + path, method=method), timeout=8) as response:
                body = response.read()
                assert response.status == 200, (method, path, response.status)
                assert body == (content if method == "GET" else b""), (method, path)
                headers = dict(response.headers.items())
                assert response.headers["Content-Type"] == mime + "; charset=utf-8", (
                    path
                )
                assert response.headers["Cache-Control"] == "no-store", path
                assert response.headers["X-Content-Type-Options"] == "nosniff", path
                assert response.headers["Content-Security-Policy"] == PAGE_POLICY, path
                assert int(response.headers["Content-Length"]) == len(content), path
                proof.append(
                    {
                        "method": method,
                        "path": path,
                        "status": response.status,
                        "length": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "headers": headers,
                    }
                )
    return {
        "routes": routes,
        "closure": {
            path: hashlib.sha256(data).hexdigest()
            for path, data in sorted(resources.items())
        },
        "http": proof,
    }
