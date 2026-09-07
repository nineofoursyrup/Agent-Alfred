"""Execute a declared credential probe. Never follows redirects."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from agent_alfred.clock import format_instant
from agent_alfred.endpoints import AuthProbe, ModelEndpoint

AUTH_HEADER_NAMES = frozenset({"authorization", "x-api-key"})


class AuthProbeRefused(Exception):
    """The probe must not send a request."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def resolve_probe_url(base_url: str, path: str) -> str | None:
    if path.startswith("//") or not path:
        return None
    if path.startswith("https://"):
        candidate = path
    elif path.startswith("/"):
        candidate = base_url.rstrip("/") + path
    else:
        return None
    target = _origin(candidate)
    base = _origin(base_url)
    if target is None or base is None or target != base:
        return None
    return candidate


def _origin(url: str) -> tuple[str, str, int] | None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443
    return (parts.scheme, host, port)


def probe_headers(
    probe: AuthProbe, api_key: str
) -> dict[str, str] | None:
    if any(api_key and api_key in value for value in probe.static_headers.values()):
        return None
    if probe.auth_scheme == "bearer":
        headers = {"Authorization": f"Bearer {api_key}"}
    elif probe.auth_scheme == "x-api-key":
        headers = {"x-api-key": api_key}
    else:
        return None
    for name, value in probe.static_headers.items():
        if name.lower() in AUTH_HEADER_NAMES:
            continue
        headers[name] = value
    return headers


def observation_for_status(status: int, probe: AuthProbe, checked_at: str) -> dict:
    if 200 <= status < 300 and status in probe.success_statuses:
        return {
            "state": "connected",
            "checked_at": checked_at,
            "checked_via": "auth_probe",
            "reason": None,
        }
    reason = probe.error_mapping.get(status) or f"http_{status}"
    return {
        "state": "error",
        "checked_at": checked_at,
        "checked_via": "auth_probe",
        "reason": reason,
    }


def run_auth_probe(
    endpoint: ModelEndpoint,
    *,
    api_key: str,
    transport,
    clock,
) -> dict:
    probe = endpoint.auth_probe
    if probe is None:
        raise AuthProbeRefused("auth_probe_unavailable", 400)
    url = resolve_probe_url(endpoint.base_url, probe.path)
    if url is None:
        raise AuthProbeRefused("auth_probe_target_rejected", 400)
    headers = probe_headers(probe, api_key)
    if headers is None:
        raise AuthProbeRefused("auth_probe_unavailable", 400)
    checked_at = format_instant(clock.wall_utc())
    try:
        result = transport.request(
            probe.method, url, headers=headers, timeout=probe.timeout_s
        )
    except TimeoutError:
        return {
            "state": "error",
            "checked_at": checked_at,
            "checked_via": "auth_probe",
            "reason": "TimeoutError",
        }
    except Exception as exc:
        return {
            "state": "error",
            "checked_at": checked_at,
            "checked_via": "auth_probe",
            "reason": type(exc).__name__,
        }
    status = result["status"]
    return observation_for_status(status, probe, checked_at)


class UrllibAuthProbeTransport:
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], timeout: float
    ):
        import urllib.error
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_301(self, req, fp, code, msg, hdrs):
                raise urllib.error.HTTPError(req.full_url, code, msg, hdrs, fp)

            http_error_302 = http_error_303 = http_error_307 = http_error_308 = (
                http_error_301
            )

        request = urllib.request.Request(
            url, data=None, headers=dict(headers), method=method
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                response.read()
                return {"status": response.status}
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            finally:
                exc.close()
            return {"status": exc.code}
        except TimeoutError:
            raise
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise TimeoutError from exc
            raise
