"""TLS lifetime at the real factory/SDK boundary; no external network or model."""

import gc
import json
import os
import socket
import ssl
import sys
import weakref
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import truststore
from httpcore2._backends.sync import TLSinTLSStream

from agent_alfred import attempt_io
from agent_alfred.bounded_connect import BoundedConnector
from agent_alfred.clock import FakeClock
from agent_alfred.endpoint_factory import EndpointClientFactory
from agent_alfred.model import (
    ClientSnapshot,
    ModelAssignment,
    ModelCallInterrupted,
    ModelRef,
    ModelRequest,
)


class Resolver:
    def resolve(self, host, port, budget):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("127.0.0.1", port),
            )
        ]


@contextmanager
def tls_factory(style, *, close_failures=0, tls_error=None, close_control=None):
    model = "deepseek-v4-flash" if style == "openai" else "qwen3.7-max"
    state = {"closes": 0, "tls": 0, "refs": []}
    payload = (
        {
            "id": "m",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {},
        }
        if style == "anthropic"
        else {
            "id": "c",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
    )
    body = json.dumps(payload).encode()
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    read_fd, write_fd = os.pipe()

    class Socket:
        def __init__(self, *args):
            state["refs"].append(weakref.ref(self))

        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            pass

        def fileno(self):
            return read_fd

        def send(self, data):
            return len(data)

        def close(self):
            state["closes"] += 1
            if state["closes"] == 1 and close_control:
                raise close_control("cleanup control")
            if state["closes"] <= close_failures:
                raise OSError("injected close failure")

    class TLSObject:
        def __init__(self):
            self.response = response

        def do_handshake(self):
            state["tls"] += 1
            if tls_error:
                raise tls_error("TLS failure") from ValueError("original TLS cause")

        def selected_alpn_protocol(self):
            return "http/1.1"

        def read(self, count):
            result, self.response = self.response[:count], self.response[count:]
            return result

        def write(self, data):
            return len(data)

    def wrap_bio(*args, **kwargs):
        return TLSObject()

    state["return_codes"] = {
        "bio": wrap_bio.__code__,
        "ssl_object": TLSObject.__init__.__code__,
        "tls_stream": TLSinTLSStream.__init__.__code__,
        "upgrade": attempt_io.DeadlineNetworkStream.start_tls.__code__,
        "connector": BoundedConnector.connect_tcp.__code__,
        "backend_inner": attempt_io.DeadlineNetworkBackend._connect_tcp.__code__,
        "backend": attempt_io.DeadlineNetworkBackend.connect_tcp.__code__,
    }
    config = ClientSnapshot(
        "v1",
        ModelAssignment("opencode-go", model, style),
        None,
        "fixture",
        False,
        False,
        5,
        5,
    )
    factory = EndpointClientFactory(clock=FakeClock())
    try:
        with (
            patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}),
            patch.object(
                attempt_io,
                "BoundedConnector",
                lambda: BoundedConnector(resolver=Resolver(), socket_factory=Socket),
            ),
            patch.object(ssl.SSLContext, "wrap_bio", wrap_bio),
            patch.object(truststore.SSLContext, "wrap_bio", wrap_bio),
        ):
            yield (
                factory,
                config,
                ModelRequest(ModelRef("opencode-go", model), None, ()),
                state,
            )
    finally:
        factory.close()
        os.close(read_fd)
        os.close(write_fd)


def finish_cleanup(factory, state, close_failures):
    for expected in range(state["closes"] + 1, close_failures + 1):
        with pytest.raises(OSError):
            factory.invalidate_all()
        gc.collect()
        assert state["closes"] == expected
        assert state["refs"][0]() is not None
    factory.close()
    gc.collect()
    assert state["closes"] == close_failures + 1
    assert all(ref() is None for ref in state["refs"])
    factory.close()
    assert state["closes"] == close_failures + 1


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("close_failures", [0, 2])
def test_tls_failure_retains_cleanup_after_sdk_conversion(style, close_failures):
    with tls_factory(style, close_failures=close_failures, tls_error=OSError) as (
        factory,
        config,
        request,
        state,
    ):
        client = factory.create(config)
        result = client.respond(request, deadline=5)
        assert result.attempts[0].outcome == "aborted"
        assert state["tls"] == 1 and state["closes"] == 1
        del result, client
        gc.collect()
        if close_failures:
            assert state["refs"][0]() is not None, "TLS failure discarded cleanup owner"
        finish_cleanup(factory, state, close_failures)


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("close_failures", [0, 2])
def test_successful_tls_keeps_the_same_owned_socket(style, close_failures):
    with tls_factory(style, close_failures=close_failures) as (
        factory,
        config,
        request,
        state,
    ):
        client = factory.create(config)
        result = client.respond(request, deadline=5)
        assert result.response is not None
        assert state["tls"] == 1 and state["closes"] == 0
        assert len(state["refs"]) == 1, "TLS must not mint an unowned second socket"
        del result, client
        finish_cleanup(factory, state, close_failures)


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("control_phase", ["tls", "cleanup"])
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_tls_control_and_original_failure_remain_traceable(
    style, control_phase, control_type
):
    with tls_factory(
        style,
        close_failures=2,
        tls_error=control_type if control_phase == "tls" else OSError,
        close_control=control_type if control_phase == "cleanup" else None,
    ) as (factory, config, request, state):
        client = factory.create(config)
        with pytest.raises(ModelCallInterrupted) as caught:
            client.respond(request, deadline=5)
        messages, seen, active = [], set(), set()

        def visit(failure):
            if failure is None:
                return
            assert id(failure) not in active, "exception graph must not cycle"
            if id(failure) in seen:
                return
            active.add(id(failure))
            seen.add(id(failure))
            messages.append(str(failure))
            visit(failure.__cause__)
            visit(failure.__context__)
            active.remove(id(failure))

        visit(caught.value)
        assert isinstance(caught.value.cause, control_type)
        assert "original TLS cause" in messages and "TLS failure" in messages
        if control_phase == "cleanup":
            assert "cleanup control" in messages
        del caught, client
        gc.collect()
        assert state["refs"][0]() is not None
        finish_cleanup(factory, state, 2)


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("close_failures", [0, 2])
@pytest.mark.parametrize(
    "edge",
    ["ssl_object", "bio", "tls_stream", "upgrade",
     "connector", "backend_inner", "backend"],
)
def test_tls_construction_return_interruption_keeps_owner(style, close_failures, edge):
    with tls_factory(style, close_failures=close_failures) as (
        factory,
        config,
        request,
        state,
    ):
        client = factory.create(config)
        interrupted = []

        def profile(frame, event, arg):
            if event == "return" and frame.f_code is state["return_codes"][edge]:
                sys.setprofile(None)
                interrupted.append(True)
                raise KeyboardInterrupt("TLS return interruption")

        try:
            sys.setprofile(profile)
            with pytest.raises(ModelCallInterrupted):
                client.respond(request, deadline=5)
        finally:
            sys.setprofile(None)
        assert interrupted == [True]
        del client
        gc.collect()
        if close_failures:
            assert state["refs"][0]() is not None
        finish_cleanup(factory, state, close_failures)


@pytest.fixture(scope="module")
def local_certificate(tmp_path_factory):
    import subprocess

    directory = tmp_path_factory.mktemp("tls-certificate")
    key, cert = directory / "key.pem", directory / "cert.pem"
    root_key, root_cert = directory / "root.key", directory / "root.pem"
    request, extensions = directory / "server.csr", directory / "server.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost\n"
    )
    commands = [
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(root_key), "-out", str(root_cert),
         "-subj", "/CN=Issue17 Test Root", "-days", "1",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        ["req", "-new", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(request), "-subj", "/CN=localhost"],
        ["x509", "-req", "-in", str(request), "-CA", str(root_cert),
         "-CAkey", str(root_key), "-CAcreateserial", "-out", str(cert),
         "-days", "1", "-extfile", str(extensions)],
    ]
    for command in commands:
        subprocess.run(["openssl", *command], check=True, capture_output=True)
    return key, cert, root_cert


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("verification", ["trusted", "untrusted", "wrong_hostname"])
@pytest.mark.parametrize("system_trust", [False, True])
def test_real_local_tls_preserves_certificate_verification(
    local_certificate, style, verification, system_trust
):
    import threading

    import httpx2

    from agent_alfred.endpoints import ModelEndpoint, ModelRoute

    key, cert, root_cert = local_certificate
    trusted = verification != "untrusted"
    accepted = verification == "trusted"
    hostname = "127.0.0.1" if verification == "wrong_hostname" else "localhost"
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    if system_trust:
        client_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if trusted:
            client_context.load_verify_locations(str(root_cert))
    else:
        client_context = ssl.create_default_context(
            cafile=str(root_cert) if trusted else None
        )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    finished, errors = threading.Event(), []
    body = json.dumps(
        {
            "id": "m",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {},
        }
        if style == "anthropic"
        else {
            "id": "c",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
    ).encode()

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = tls.recv(8192)
                        if not chunk:
                            raise AssertionError("request ended before headers")
                        request += chunk
                    headers, received = request.split(b"\r\n\r\n", 1)
                    length = next(
                        int(line.split(b":", 1)[1])
                        for line in headers.lower().split(b"\r\n")
                        if line.startswith(b"content-length:")
                    )
                    while len(received) < length:
                        chunk = tls.recv(8192)
                        if not chunk:
                            raise AssertionError("request ended before body")
                        received += chunk
                    tls.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                        + body
                    )
        except ssl.SSLError as error:
            errors.append(error)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with httpx2.Client(verify=client_context, trust_env=False) as http:
            factory = EndpointClientFactory(
                clock=FakeClock(),
                http_client=http,
                endpoints=(
                    ModelEndpoint(
                        "local",
                        f"https://{hostname}:{port}",
                        "UNUSED",
                        {
                            "m": ModelRoute(
                                style,
                                "/messages"
                                if style == "anthropic"
                                else "/chat/completions",
                            )
                        },
                    ),
                ),
            )
            config = ClientSnapshot(
                "v1",
                ModelAssignment("local", "m", style),
                None,
                "fixture",
                False,
                False,
                5,
                5,
            )
            result = factory.create(config).respond(
                ModelRequest(ModelRef("local", "m"), None, ()), deadline=5
            )
            assert (result.response is not None) is accepted
            assert len(result.attempts) == 1
            factory.close()
        assert finished.wait(5)
        thread.join(5)
        assert not thread.is_alive()
        if accepted:
            assert errors == []
        else:
            assert len(errors) == 1 and isinstance(errors[0], ssl.SSLError)
    finally:
        listener.close()
        thread.join(5)
