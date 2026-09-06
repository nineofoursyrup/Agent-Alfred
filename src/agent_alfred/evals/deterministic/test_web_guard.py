"""The four request-time defences, each refused on its own grounds.

ADR-0014's point is that the layers are not interchangeable: binding
loopback does not stop another origin's page, Host does not stop a
same-origin-looking write, Origin does not stop a form POST, and CSRF does
nothing for a read. Each test here removes exactly one layer's protection and
asserts the layer that is supposed to catch it still does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

import pytest

from agent_alfred.gateway.web.guard import (
    ALLOWED_HOST_NAMES,
    CSRF_HEADER,
    JSON_CONTENT_TYPE,
    MAX_BODY_BYTES,
    AuthorizedRequest,
    Rejection,
    RequestGuard,
    normalize_host,
    normalize_origin,
)

PORT = 7717
TOKEN = "t" * 40


@dataclass
class _Headers:
    """Case-insensitive enough for the guard: it must not depend on case."""

    raw: dict[str, str]

    def get(self, name: str, default=None):
        for key, value in self.raw.items():
            if key.lower() == name.lower():
                return value
        return default

    def items(self):
        return self.raw.items()


def _guard() -> RequestGuard:
    return RequestGuard(port=PORT, csrf_token=TOKEN)


def _headers(**kwargs: str) -> _Headers:
    return _Headers(dict(kwargs))


def _read(**kwargs: str):
    return _guard().check(method="GET", headers=_headers(**kwargs))


def _write(**kwargs: str):
    return _guard().check(method="POST", headers=_headers(**kwargs))


# --- host normalization ----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("localhost", "localhost"),
        ("LOCALHOST", "localhost"),
        ("localhost:7717", "localhost"),
        ("127.0.0.1:7717", "127.0.0.1"),
        # A trailing dot is the same name to a resolver and to a browser.
        ("localhost.", "localhost"),
        ("  localhost  ", "localhost"),
        ("[::1]", "[::1]"),
        ("[::1]:7717", "[::1]"),
    ],
)
def test_a_host_that_names_this_machine_normalizes(value: str, expected: str) -> None:
    assert normalize_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        # Userinfo: the classic "looks like localhost" trick.
        "evil.com@127.0.0.1",
        "127.0.0.1@evil.com",
        "localhost:7717@evil.com",
        # A path or a query is not a host.
        "localhost/path",
        "localhost?x=1",
        "localhost\\path",
        # Malformed ports and bare IPv6 without brackets.
        "localhost:http",
        "localhost:7717:9",
        "::1",
        "[::1",
        ".",
    ],
)
def test_a_host_that_does_not_name_this_machine_is_refused(value: str) -> None:
    assert normalize_host(value) is None


def test_the_allowed_names_are_exactly_the_two_we_bind() -> None:
    # No "[::1]": we bind IPv4 loopback only, and listing a name that cannot
    # reach the listener would only suggest it is supported.
    assert ALLOWED_HOST_NAMES == frozenset({"127.0.0.1", "localhost"})


# --- the Host layer --------------------------------------------------------


def test_a_request_without_a_host_is_refused() -> None:
    rejection = _read(origin=f"http://localhost:{PORT}")
    assert rejection is not None
    assert rejection.status == 400
    assert rejection.code == "missing_host"


@pytest.mark.parametrize("host", ["evil.com", "127.0.0.2", "localhost.evil.com"])
def test_a_host_that_is_not_this_machine_is_refused(host: str) -> None:
    rejection = _read(host=host)
    assert rejection is not None
    assert rejection.code == "host_not_allowed"


def test_the_host_check_stops_dns_rebinding() -> None:
    """The one thing binding loopback cannot do.

    A rebound name resolves to 127.0.0.1 at connect time, so the socket
    succeeds; the Host header is the only place the name the browser thinks
    it is talking to still shows up.
    """
    rejection = _read(host="rebound.evil.com", origin=f"http://localhost:{PORT}")
    assert rejection is not None
    assert rejection.code == "host_not_allowed"


# --- the Origin layer ------------------------------------------------------


def test_the_whitelist_is_exactly_two_entries_on_the_bound_port() -> None:
    assert _guard().allowed_origins == frozenset(
        {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:7717",
        "http://localhost:7717",
        # HTTP syntax-level surrounding whitespace is not part of the value.
        "  http://localhost:7717  ",
    ],
)
def test_an_origin_from_this_dashboard_is_allowed(origin: str) -> None:
    assert _read(host="localhost", origin=origin) is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.com",
        "https://localhost:7717",
        "http://localhost:7718",
        "http://[::1]:7717",
        "null",
        "http://localhost.evil.com:7717",
        # Nothing is normalized into a whitelist value: a trailing slash,
        # several of them, a path, userinfo, a comma list and a differently
        # cased spelling are all different origins, and each is refused.
        "http://localhost:7717/",
        "http://localhost:7717//",
        "http://localhost:7717/path",
        "http://x@localhost:7717",
        "http://localhost:7717,http://127.0.0.1:7717",
        "HTTP://LOCALHOST:7717",
    ],
)
def test_a_foreign_origin_is_refused(origin: str) -> None:
    rejection = _read(host="localhost", origin=origin)
    assert rejection is not None
    assert rejection.status == 403
    assert rejection.code == "origin_not_allowed"


def test_a_read_without_an_origin_is_allowed() -> None:
    """An EventSource on our own origin sends no Origin at all.

    Refusing the missing header would break the stream the Dashboard exists
    to provide; Host and the absent CORS header are what hold this case.
    """
    assert _read(host="localhost") is None


def test_the_origin_check_is_exact_not_prefix_matched() -> None:
    # Suffix matching here is how "http://localhost:7717.evil.com" would
    # become an allowed origin.
    assert normalize_origin("http://localhost:7717.evil.com") not in (
        _guard().allowed_origins
    )


# --- the CSRF layer --------------------------------------------------------


def _valid_write(**kwargs: str):
    base = {
        "host": "localhost",
        CSRF_HEADER: TOKEN,
        "content-type": JSON_CONTENT_TYPE,
        "content-length": "17",
    }
    base.update(kwargs)
    return base


def test_a_write_with_the_process_token_is_allowed() -> None:
    assert _write(**_valid_write()) is None


def test_an_authorized_write_carries_its_validated_body_length() -> None:
    result = _guard().authorize(method="POST", headers=_headers(**_valid_write()))
    assert result == AuthorizedRequest(body_length=17)


def test_a_write_without_a_token_is_refused() -> None:
    headers = _valid_write()
    del headers[CSRF_HEADER]
    rejection = _write(**headers)
    assert rejection is not None
    assert rejection.status == 403
    assert rejection.code == "csrf_rejected"


def test_a_write_with_the_wrong_token_is_refused() -> None:
    rejection = _write(**_valid_write(**{CSRF_HEADER: TOKEN[:-1] + "x"}))
    assert rejection is not None
    assert rejection.code == "csrf_rejected"


def test_a_read_is_never_asked_for_a_token() -> None:
    """An EventSource cannot set request headers.

    Writes are guarded by the token precisely because reads cannot be; if a
    read needed one, the stream would be impossible.
    """
    assert _read(host="localhost") is None


# --- the body layer --------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "APPLICATION/JSON"],
)
def test_a_json_write_is_allowed(content_type: str) -> None:
    assert _write(**_valid_write(**{"content-type": content_type})) is None


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        # Form posts are exactly the cross-origin write CSRF exists to stop:
        # a browser will send them without a preflight.
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "text/html",
        "",
    ],
)
def test_a_non_json_write_is_refused(content_type: str) -> None:
    rejection = _write(**_valid_write(**{"content-type": content_type}))
    assert rejection is not None
    assert rejection.status == 415
    assert rejection.code == "content_type_not_allowed"


def test_a_write_without_a_content_length_is_refused_before_reading() -> None:
    headers = _valid_write()
    del headers["content-length"]
    rejection = _write(**headers)
    assert rejection is not None
    assert rejection.status == 411
    assert rejection.code == "length_required"


def test_a_write_over_the_body_limit_is_refused_before_reading() -> None:
    rejection = _write(**_valid_write(**{"content-length": str(MAX_BODY_BYTES + 1)}))
    assert rejection is not None
    assert rejection.status == 413
    assert rejection.code == "body_too_large"


def test_the_body_limit_is_the_limit_not_a_suggestion() -> None:
    assert _write(**_valid_write(**{"content-length": str(MAX_BODY_BYTES)})) is None


@pytest.mark.parametrize(
    "int_max_digits",
    [None, "0", "10000"],
    ids=["default", "unlimited", "ten-thousand"],
)
def test_an_arbitrarily_long_ascii_byte_count_is_stably_refused(
    int_max_digits: str | None,
) -> None:
    """The protocol answer must not depend on Python's integer limit."""
    script = """
from agent_alfred.gateway.web.guard import CSRF_HEADER, RequestGuard

result = RequestGuard(port=7717, csrf_token="token").authorize(
    method="POST",
    headers={
        "host": "localhost",
        CSRF_HEADER: "token",
        "content-type": "application/json",
        "content-length": "9" * 5000,
    },
)
print(result.status, result.code)
"""
    env = os.environ.copy()
    if int_max_digits is None:
        env.pop("PYTHONINTMAXSTRDIGITS", None)
    else:
        env["PYTHONINTMAXSTRDIGITS"] = int_max_digits

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "413 body_too_large"
    assert completed.stderr == ""


@pytest.mark.parametrize("length", ["²", "١"])
def test_a_non_ascii_digit_is_not_an_http_byte_count(length: str) -> None:
    rejection = _write(**_valid_write(**{"content-length": length}))
    assert rejection is not None
    assert rejection.status == 400
    assert rejection.code == "bad_content_length"


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        ("0", 0),
        (str(MAX_BODY_BYTES - 1), MAX_BODY_BYTES - 1),
        (str(MAX_BODY_BYTES), MAX_BODY_BYTES),
        (str(MAX_BODY_BYTES + 1), "body_too_large"),
        ("0" * 5000, 0),
        ("0" * 5000 + str(MAX_BODY_BYTES), MAX_BODY_BYTES),
        ("0" * 5000 + str(MAX_BODY_BYTES + 1), "body_too_large"),
    ],
    ids=[
        "zero",
        "max-minus-one",
        "max",
        "max-plus-one",
        "zero-padded-zero",
        "zero-padded-max",
        "zero-padded-max-plus-one",
    ],
)
def test_content_length_comparison_preserves_boundaries_and_leading_zeroes(
    length: str, expected: int | str
) -> None:
    result = _guard().authorize(
        method="POST",
        headers=_headers(**_valid_write(**{"content-length": length})),
    )
    if isinstance(expected, int):
        assert result == AuthorizedRequest(body_length=expected)
    else:
        assert isinstance(result, Rejection)
        assert result.code == expected


@pytest.mark.parametrize("length", ["-1", "lots", "1.5", ""])
def test_a_content_length_that_is_not_a_byte_count_is_refused(length: str) -> None:
    rejection = _write(**_valid_write(**{"content-length": length}))
    assert rejection is not None
    assert rejection.status == 400
    assert rejection.code == "bad_content_length"


def test_headers_are_matched_without_regard_to_case() -> None:
    # HTTP header names are case-insensitive; a guard that compared them
    # literally would reject legitimate requests from some clients and,
    # worse, accept a differently-cased look-alike from others.
    mixed = {key.upper(): value for key, value in _valid_write().items()}
    assert _guard().check(method="POST", headers=_Headers(mixed)) is None


class _RepeatedHeaders(_Headers):
    def __init__(self, raw: dict[str, str], repeated: dict[str, list[str]]):
        super().__init__(raw)
        self.repeated = repeated

    def get_all(self, name: str):
        return self.repeated.get(name.lower())


@pytest.mark.parametrize("method", ("GET", "OPTIONS", "TRACE"))
def test_nonwrite_authorization_reports_a_declared_unconsumed_body(method) -> None:
    result = _guard().authorize(
        method=method,
        headers=_headers(host="localhost", **{"content-length": "12"}),
    )
    assert isinstance(result, AuthorizedRequest)
    assert result.body_length is None
    assert result.body_declared is True


def test_transfer_encoding_and_repeated_lengths_fail_closed() -> None:
    transfer = _guard().authorize(
        method="GET",
        headers=_headers(host="localhost", **{"transfer-encoding": "chunked"}),
    )
    repeated = _guard().authorize(
        method="GET",
        headers=_RepeatedHeaders(
            {"host": "localhost", "content-length": "2"},
            {"content-length": ["2", "2"]},
        ),
    )
    assert isinstance(transfer, Rejection)
    assert transfer.code == "unsupported_transfer_encoding"
    assert transfer.body_declared is True
    assert isinstance(repeated, Rejection)
    assert repeated.code == "conflicting_content_length"
    assert repeated.body_declared is True
