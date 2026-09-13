"""Bounded, single-attempt urllib transport. No global opener or retry thread."""

import json
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


@dataclass(frozen=True)
class HTTPResult:
    status: int | None = None
    data: object = None
    retry_after: str | None = None
    error: str | None = None
    not_sent: bool = False


def decode_number(text):
    try:
        return Decimal(text)
    except InvalidOperation:
        # Explicit #20 contract supplement: an unrepresentable optional
        # number is unknown; valid source data still survives decoding.
        return Decimal("NaN")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TavilyHTTP:
    def __init__(self, *, base_url="https://api.tavily.com", proxy_handler=None):
        # Injection is exclusively a transport test seam, never a user setting.
        self.base_url = base_url
        self.proxy_handler = proxy_handler

    def request(self, path, key, payload, *, deadline, monotonic):
        limit = 1024 * 1024 if path == "/search" else 64 * 1024
        response = None
        attempted = False
        status = None
        retry_after = None

        def remaining():
            value = deadline - monotonic()
            if value <= 0:
                raise TimeoutError()
            return value

        try:
            remaining()
            proxies = self.proxy_handler or ProxyHandler()
            for scheme, proxy in proxies.proxies.items():
                if scheme == "no":
                    continue
                parsed = urlsplit(proxy if "://" in proxy else "http://" + proxy)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    return HTTPResult(error="proxy_invalid", not_sent=True)
                _ = parsed.port
            opener = build_opener(proxies, NoRedirect())
            request = Request(
                self.base_url + path,
                data=None
                if payload is None
                else json.dumps(payload, ensure_ascii=False).encode(),
                headers={
                    "Authorization": "Bearer " + key,
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                },
                method="GET" if payload is None else "POST",
            )
            timeout = remaining()
            attempted = True
            try:
                response = opener.open(request, timeout=timeout)
            except HTTPError as exc:
                response = exc
            status = response.code
            retry_after = response.headers.get("Retry-After")
            if (
                response.headers.get("Content-Encoding", "identity").lower()
                != "identity"
            ):
                return HTTPResult(
                    status, retry_after=retry_after, error="unsupported_encoding"
                )
            chunks = []
            count = 0
            while True:
                timeout = remaining()
                # urllib's acquired HTTPResponse retains the owning socket here.
                # read1 performs at most one raw IO, so slow chunks cannot renew
                # the total budget; DNS remains a cooperative system boundary.
                raw_response = (
                    response.fp if isinstance(response, HTTPError) else response
                )
                fp = getattr(raw_response, "fp", None)
                sock = getattr(getattr(fp, "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(timeout)
                chunk = response.read1(min(65536, limit + 1 - count))
                remaining()
                if not chunk:
                    if getattr(raw_response, "length", 0):
                        return HTTPResult(
                            status, retry_after=retry_after, error="protocol_error"
                        )
                    break
                count += len(chunk)
                if count > limit:
                    return HTTPResult(
                        status, retry_after=retry_after, error="response_too_large"
                    )
                chunks.append(chunk)
            try:
                data = json.loads(
                    b"".join(chunks),
                    parse_int=decode_number,
                    parse_float=decode_number,
                    parse_constant=decode_number,
                )
            except ValueError, UnicodeError:
                return HTTPResult(
                    status, retry_after=retry_after, error="protocol_error"
                )
            return HTTPResult(status, data, retry_after)
        except (TimeoutError, URLError, OSError, ValueError, HTTPException) as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            not_sent = not attempted or isinstance(
                cause, (socket.gaierror, ConnectionRefusedError)
            )
            return HTTPResult(
                status,
                retry_after=retry_after,
                error="timeout" if isinstance(cause, TimeoutError) else "network_error",
                not_sent=not_sent,
            )
        finally:
            if response is not None:
                response.close()
