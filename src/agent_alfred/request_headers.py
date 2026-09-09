"""Destination-scoped inference headers; never mutate pooled SDK defaults."""

import hashlib
import uuid
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlsplit


def opencode_headers(endpoint, route):
    url = urlsplit(endpoint.base_url)
    if (
        url.scheme != "https"
        or url.hostname != "opencode.ai"
        or url.port not in (None, 443)
        or url.path.rstrip("/") != "/zen/go/v1"
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or route.path not in ("/messages", "/chat/completions")
    ):
        return None
    try:
        release = version("agent-alfred")
    except PackageNotFoundError:
        release = "0+unknown"
    # Factory-created clients without Host provenance have their own isolated
    # conversation lifetime. Both codecs/fallback attempts share this closure.
    fallback = "client:" + uuid.uuid4().hex

    def headers(request):
        identity = request.conversation_id or fallback
        return {
            "User-Agent": "agent-alfred/" + release,
            "x-opencode-session": "alfred-"
            + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        }

    return headers
