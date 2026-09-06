"""The request-time defences: Host, Origin, CSRF, and body shape.

ADR-0014 separates four layers, and each of them defends against something
the others cannot see:

- binding only the loopback address limits **who can connect**, not which
  page can make the browser connect;
- the **Host** check stops DNS rebinding, which is precisely an attack on the
  assumption "they can only connect from localhost";
- the **Origin** check stops a cross-origin page from *reading*, which is
  what an ``EventSource`` or a ``fetch`` in someone else's tab would do;
- the **CSRF token** stops a cross-origin page from *writing*, which is the
  one thing the other three cannot prevent, because a simple POST needs no
  permission from us to be sent.

They are kept in one module because they are one decision seen from four
sides, and because the temptation to "simplify" by dropping one layer is
exactly how the whole set stops being a defence.

The one header that must never appear is ``Access-Control-Allow-Origin``.
A cross-origin ``EventSource`` gets no data precisely because that header is
absent; adding it, even with ``*``, removes the Origin layer silently. This
module never sets it and there is no code path that adds it.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Mapping

# A chat message larger than this is not a chat message. The limit is checked
# from Content-Length *before* the body is read: reading first and rejecting
# second would let any caller pin the memory with one request.
MAX_BODY_BYTES = 1024 * 1024

# Every method that changes state. A GET is never asked for a token, because
# an EventSource cannot set request headers -- reads are held by Host and
# Origin instead, and that is the whole reason those two layers exist.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CSRF_HEADER = "x-agent-alfred-csrf"
JSON_CONTENT_TYPE = "application/json"

# The Host names that mean "this machine". Exactly two, because we bind IPv4
# loopback only: a name that cannot reach the listener has no business being
# in a whitelist, and listing it would only suggest it is supported.
ALLOWED_HOST_NAMES = frozenset({"127.0.0.1", "localhost"})

__all__ = [
    "ALLOWED_HOST_NAMES",
    "CSRF_HEADER",
    "JSON_CONTENT_TYPE",
    "MAX_BODY_BYTES",
    "WRITE_METHODS",
    "AuthorizedRequest",
    "Rejection",
    "RequestGuard",
    "normalize_host",
    "normalize_origin",
]


def normalize_host(value: str) -> str | None:
    """Reduce a Host header to the bare name it names, or None if it lies.

    Normalization is what makes the comparison mean anything. A browser
    treats ``LOCALHOST:7717``, ``localhost.`` and ``localhost`` as the same
    origin, so a check that compared raw strings would either reject
    legitimate requests or accept a look-alike -- and a look-alike is all a
    rebinding attack needs.

    ``None`` means the value is not a host:port pair at all, which covers
    userinfo (``evil.com@127.0.0.1``), paths, and malformed ports. Each of
    those is a refusal rather than a best-effort parse.
    """
    text = value.strip().lower()
    if not text:
        return None
    if "@" in text or "/" in text or "\\" in text or "?" in text:
        return None
    if text.startswith("["):
        # An IPv6 literal: [::1] or [::1]:7717. No name inside the brackets
        # is ever compared, because none of them is a loopback name we bind.
        end = text.find("]")
        if end < 0:
            return None
        host = text[: end + 1]
        rest = text[end + 1 :]
        if rest and not rest.startswith(":"):
            return None
        return host
    if text.count(":") > 1:
        # More than one colon and no brackets is not a host:port pair.
        return None
    host, _, port = text.partition(":")
    if port and not port.isdigit():
        return None
    host = host.rstrip(".")
    if not host:
        return None
    return host


def normalize_origin(value: str) -> str | None:
    """Reduce an Origin to the value compared against the whitelist.

    Only the HTTP syntax's surrounding whitespace is removed -- optional
    whitespace around a header's value is not part of it. Nothing else is
    normalized: a trailing slash, a path, userinfo, a comma list or a
    different case each name a *different* origin, and folding any of them
    into a whitelist value is how one check comes to accept two origins
    while believing it accepts one.
    """
    text = value.strip()
    if not text:
        return None
    return text


@dataclass(frozen=True)
class Rejection:
    """Why a request is refused. ``code`` is the machine-readable reason."""

    status: int
    code: str
    detail: str
    body_declared: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class AuthorizedRequest:
    """Values the guard validated for the request that may proceed."""

    body_length: int | None
    body_declared: bool = field(default=False, compare=False)


def _rejection(
    status: int, code: str, detail: str, *, body_declared: bool
) -> Rejection:
    return Rejection(status, code, detail, body_declared=body_declared)


class RequestGuard:
    """Decides whether one request may proceed. Pure: no IO, no state."""

    def __init__(self, *, port: int, csrf_token: str):
        self._port = port
        self._csrf_token = csrf_token
        # Exactly two entries: the port this instance actually bound. No
        # "[::1]" -- we do not listen on IPv6, so an entry for it would only
        # advertise a way in that does not exist.
        self._origins = frozenset(
            {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            }
        )

    @property
    def csrf_token(self) -> str:
        return self._csrf_token

    @property
    def allowed_origins(self) -> frozenset[str]:
        return self._origins

    def check(self, *, method: str, headers: Mapping[str, str]) -> Rejection | None:
        """None means allowed. The first failing layer names the refusal."""
        result = self.authorize(method=method, headers=headers)
        return result if isinstance(result, Rejection) else None

    def authorize(
        self, *, method: str, headers: Mapping[str, str]
    ) -> AuthorizedRequest | Rejection:
        """Validate once and return the values downstream request IO may use."""
        body_length, framing_error, body_declared = _request_framing(headers)

        host = _header(headers, "host")
        if host is None:
            return _rejection(
                400,
                "missing_host",
                "a Host header is required on every request",
                body_declared=body_declared,
            )
        name = normalize_host(host)
        if name is None or name not in ALLOWED_HOST_NAMES:
            return _rejection(
                400,
                "host_not_allowed",
                "the Host header does not name this machine",
                body_declared=body_declared,
            )
        origin = _header(headers, "origin")
        if origin is not None and normalize_origin(origin) not in self._origins:
            # 403 rather than 400: the request was well formed, it came from
            # somewhere we do not answer. The body says nothing about what
            # the whitelist contains, so a probe learns nothing.
            return _rejection(
                403,
                "origin_not_allowed",
                "the Origin is not this Dashboard",
                body_declared=body_declared,
            )
        if method.upper() in WRITE_METHODS:
            return self._check_write(
                headers,
                body_length=body_length,
                framing_error=framing_error,
                body_declared=body_declared,
            )
        if framing_error is not None:
            return framing_error
        return AuthorizedRequest(body_length=None, body_declared=body_declared)

    def _check_write(
        self,
        headers: Mapping[str, str],
        *,
        body_length: int | None,
        framing_error: Rejection | None,
        body_declared: bool,
    ) -> AuthorizedRequest | Rejection:
        token = _header(headers, CSRF_HEADER)
        try:
            token_matches = token is not None and hmac.compare_digest(
                token, self._csrf_token
            )
        except TypeError:
            # Unsupported token text is a refusal, not a handler failure.
            token_matches = False
        if not token_matches:
            return _rejection(
                403,
                "csrf_rejected",
                "a valid CSRF token is required to write",
                body_declared=body_declared,
            )
        content_type = (_header(headers, "content-type") or "").split(";")[0]
        if content_type.strip().lower() != JSON_CONTENT_TYPE:
            return _rejection(
                415,
                "content_type_not_allowed",
                f"writes must be {JSON_CONTENT_TYPE}",
                body_declared=body_declared,
            )
        if framing_error is not None:
            return framing_error
        if body_length is None:
            return _rejection(
                411,
                "length_required",
                "a Content-Length is required to write",
                body_declared=body_declared,
            )
        return AuthorizedRequest(
            body_length=body_length, body_declared=body_declared
        )


def _request_framing(
    headers: Mapping[str, str],
) -> tuple[int | None, Rejection | None, bool]:
    """Return the one authoritative HTTP body framing decision."""
    transfer_encodings = _header_values(headers, "transfer-encoding")
    lengths = _header_values(headers, "content-length")
    if transfer_encodings:
        return (
            None,
            Rejection(
                400,
                "unsupported_transfer_encoding",
                "Transfer-Encoding is not supported",
                body_declared=True,
            ),
            True,
        )
    if len(lengths) > 1:
        return (
            None,
            Rejection(
                400,
                "conflicting_content_length",
                "exactly one Content-Length is allowed",
                body_declared=True,
            ),
            True,
        )
    if not lengths:
        return None, None, False
    result = _authorize_body_length(lengths[0])
    if isinstance(result, Rejection):
        return (
            None,
            Rejection(
                result.status,
                result.code,
                result.detail,
                body_declared=True,
            ),
            True,
        )
    return result.body_length, None, True


def _authorize_body_length(raw: str) -> AuthorizedRequest | Rejection:
    """Parse one HTTP decimal without constructing an unbounded integer."""
    text = raw.strip()
    if not text.isascii() or not text.isdecimal():
        return Rejection(
            400, "bad_content_length", "Content-Length is not a byte count"
        )

    significant = text.lstrip("0") or "0"
    maximum = str(MAX_BODY_BYTES)
    if len(significant) > len(maximum) or (
        len(significant) == len(maximum) and significant > maximum
    ):
        return Rejection(
            413,
            "body_too_large",
            f"the body may be at most {MAX_BODY_BYTES} bytes",
        )
    return AuthorizedRequest(body_length=int(significant))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """One header, case-insensitively.

    Called with a mapping rather than ``email.message.Message`` so the guard
    can be tested with a plain dict: the defence is the decision, not the
    parsing of the wire.
    """
    value = headers.get(name)
    if value is None:
        value = headers.get(name.title())
    if value is None:
        target = name.lower()
        for key, candidate in headers.items():
            if key.lower() == target:
                return candidate
    return value


def _header_values(headers: Mapping[str, str], name: str) -> list[str]:
    get_all = getattr(headers, "get_all", None)
    if get_all is not None:
        values = get_all(name)
        if values is not None:
            return list(values)
    value = _header(headers, name)
    return [] if value is None else [value]
