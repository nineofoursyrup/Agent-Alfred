"""Inert SDK modules and bodies only: never load AWS clients or credentials."""

import base64
import dis
import json
import stat
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import runtime as runtime_module
from agent_alfred.evals.acceptance.controlled.aws_persistence import AwsExecutionStore
from agent_alfred.evals.acceptance.controlled.control import ControlService
from agent_alfred.evals.acceptance.controlled.runtime import InstalledRuntime
from agent_alfred.evals.acceptance.schema import digest, encode
from agent_alfred.resource_rollback import (
    IncompleteRollback,
    ResumableRollback,
    RollbackSlot,
    capture_call_result,
)

from ._monitoring_test_helpers import (
    interrupt_instruction_once,
    interrupt_py_return_once,
)
from .test_controlled_aws_persistence import DynamoStub, S3Stub, aws_fixture
from .test_controlled_execution import prepare
from .test_controlled_persistence import control_envelope


def inert_installed_sdk(
    monkeypatch,
    *,
    principal="fixture:expected",
    construction_failure=None,
    identity_failure=None,
    close_failures=None,
):
    """Replace SDK imports before the public constructor can call any factory."""
    clients, closes = {}, []
    close_failures = close_failures or {}

    class Client:
        def __init__(self, service):
            self.service, self.close_calls = service, 0

        def close(self):
            self.close_calls += 1
            closes.append(self.service)
            failure = close_failures.get(self.service)
            if failure is not None and self.close_calls == 1:
                raise failure

        def get_caller_identity(self):
            if identity_failure is not None:
                raise identity_failure
            return {"Arn": principal}

    def client(service, **options):
        if construction_failure is not None and service == construction_failure[0]:
            raise construction_failure[1]
        result = Client(service)
        clients[service] = result
        return result

    boto3, botocore, config_module = (
        ModuleType("boto3"),
        ModuleType("botocore"),
        ModuleType("botocore.config"),
    )
    boto3.client = client
    config_module.Config = lambda **options: options
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", config_module)
    return {"region": "fixture", "principal": "fixture:expected"}, clients, closes


def test_installed_constructor_releases_clients_after_identity_mismatch(monkeypatch):
    config, clients, closes = inert_installed_sdk(
        monkeypatch, principal="fixture:wrong"
    )
    with pytest.raises(ValueError, match="installed_runtime_identity_mismatch"):
        InstalledRuntime(config, token=runtime_module._INSTALL)
    assert closes == ["sts", "secretsmanager", "lambda", "kms"]
    assert all(client.close_calls == 1 for client in clients.values())


@pytest.mark.parametrize("service", ["kms", "lambda", "secretsmanager", "sts"])
def test_installed_constructor_rolls_back_only_created_clients(monkeypatch, service):
    failure = OSError("fixture SDK construction failed")
    config, clients, closes = inert_installed_sdk(
        monkeypatch, construction_failure=(service, failure)
    )
    with pytest.raises(OSError) as caught:
        InstalledRuntime(config, token=runtime_module._INSTALL)
    assert caught.value is failure
    assert closes == list(reversed(clients))
    assert all(client.close_calls == 1 for client in clients.values())


@pytest.mark.parametrize(
    ("identity_type", "close_type"),
    [
        (OSError, OSError),
        (KeyboardInterrupt, OSError),
        (OSError, SystemExit),
        (GeneratorExit, SystemExit),
    ],
)
def test_installed_constructor_retains_first_failure_and_retry_progress(
    monkeypatch, identity_type, close_type
):
    failure = identity_type("fixture identity read failed")
    close_failure = close_type("fixture SDK close failed")
    config, clients, closes = inert_installed_sdk(
        monkeypatch,
        identity_failure=failure,
        close_failures={"secretsmanager": close_failure},
    )
    with pytest.raises(BaseException) as caught:
        InstalledRuntime(config, token=runtime_module._INSTALL)
    expected = (
        close_failure
        if identity_type is OSError and close_type is SystemExit
        else failure
    )
    assert caught.value is expected
    pending = caught.value.__cause__
    assert isinstance(pending, IncompleteRollback)
    assert pending.failure is failure and pending.errors == (close_failure,)
    assert closes == ["sts", "secretsmanager"]
    assert pending.retry() is True
    assert pending.retry() is True
    assert closes == ["sts", "secretsmanager", "secretsmanager", "lambda", "kms"]
    assert all(client.close_calls >= 1 for client in clients.values())


def test_installed_runtime_close_is_terminal_and_cleanup_is_body_only(monkeypatch):
    config, clients, closes = inert_installed_sdk(monkeypatch)
    runtime = InstalledRuntime(config, token=runtime_module._INSTALL)
    assert runtime.retry_cleanup() is True and closes == []
    runtime.close()
    runtime.close()
    assert closes == ["sts", "secretsmanager", "lambda", "kms"]
    assert all(client.close_calls == 1 for client in clients.values())
    with pytest.raises(ValueError, match="installed_runtime_closed"):
        runtime.observe("fixture", {})
    with pytest.raises(ValueError, match="installed_runtime_closed"):
        runtime.send({}, preflight=lambda stage: pytest.fail(stage), timeout=1)


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_installed_runtime_keeps_failed_shutdown_separate_from_body_retry(
    monkeypatch, failure_type
):
    failure = failure_type("fixture runtime close interrupted")
    config, clients, closes = inert_installed_sdk(
        monkeypatch, close_failures={"secretsmanager": failure}
    )
    runtime = InstalledRuntime(config, token=runtime_module._INSTALL)
    with pytest.raises(failure_type) as caught:
        runtime.close()
    assert caught.value is failure
    assert isinstance(caught.value.__cause__, IncompleteRollback)
    assert closes == ["sts", "secretsmanager"]
    assert runtime.retry_cleanup() is True
    assert closes == ["sts", "secretsmanager"]
    with pytest.raises(ValueError, match="installed_runtime_closed"):
        runtime.send({}, preflight=lambda stage: pytest.fail(stage), timeout=1)
    # Dropping the exception does not lose this runtime's shutdown progress.
    runtime.close()
    runtime.close()
    assert closes == ["sts", "secretsmanager", "secretsmanager", "lambda", "kms"]
    assert all(client.close_calls >= 1 for client in clients.values())


@pytest.mark.parametrize("client_number", [1, 2, 3, 4])
def test_installed_factory_result_is_owned_before_next_caller_instruction(
    monkeypatch, client_number
):
    config, clients, closes = inert_installed_sdk(monkeypatch)
    control = KeyboardInterrupt("fixture SDK caller return interrupted")
    code = capture_call_result.__code__
    target = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname in {"RETURN_VALUE", "RETURN_CONST"}
    )
    with interrupt_instruction_once(
        code, target, control, occurrence=client_number
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as caught:
            InstalledRuntime(config, token=runtime_module._INSTALL)
    assert armed == [False] and caught.value is control
    assert len(clients) == client_number
    assert closes == list(reversed(clients))


def protected_fixture_config(tmp_path, monkeypatch, config):
    """Synthetic protection facts; the only opened file is this local fixture."""
    path = tmp_path / "deployment.json"
    path.write_text(
        json.dumps(
            {
                **config,
                "version": 1,
                "deployment_id": "fixture",
                "source_function_arn": "arn:aws:lambda:fixture",
                "signing_key_arn": "arn:aws:kms:fixture",
                "secret_arn": "arn:aws:secretsmanager:fixture",
            }
        )
    )
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_uid=0)
    monkeypatch.setattr(Path, "lstat", lambda path: info)
    monkeypatch.setattr(runtime_module.os, "fstat", lambda fd: info)
    return path


@pytest.mark.parametrize("entrypoint", ["constructor", "installer"])
def test_installed_runtime_keeps_caller_owner_across_return_and_store(
    tmp_path, monkeypatch, entrypoint
):
    config, clients, closes = inert_installed_sdk(monkeypatch)
    owner = ResumableRollback()
    if entrypoint == "constructor":
        code = InstalledRuntime.__init__.__code__
    else:
        path = protected_fixture_config(tmp_path, monkeypatch, config)
        code = runtime_module.install_runtime.__code__
    control = SystemExit("fixture runtime returned before caller stored it")
    with interrupt_py_return_once("installed-runtime-handoff", code, control) as armed:
        with pytest.raises(SystemExit) as caught:
            if entrypoint == "constructor":
                InstalledRuntime(config, token=runtime_module._INSTALL, _rollback=owner)
            else:
                runtime_module.install_runtime(path, _rollback=owner)
    assert armed == [False] and caught.value is control
    assert closes == []
    owner.close()
    owner.close()
    assert closes == ["sts", "secretsmanager", "lambda", "kms"]
    assert all(client.close_calls == 1 for client in clients.values())


def test_installer_cleans_up_interrupted_constructor_return(tmp_path, monkeypatch):
    config, clients, closes = inert_installed_sdk(monkeypatch)
    path = protected_fixture_config(tmp_path, monkeypatch, config)
    control = GeneratorExit("fixture constructor return interrupted")
    with interrupt_py_return_once(
        "installed-constructor-handoff", InstalledRuntime.__init__.__code__, control
    ) as armed:
        with pytest.raises(GeneratorExit) as caught:
            runtime_module.install_runtime(path)
    assert armed == [False] and caught.value is control
    assert closes == ["sts", "secretsmanager", "lambda", "kms"]
    assert all(client.close_calls == 1 for client in clients.values())


class ResponseBody:
    def __init__(self, raw, *, read_failure=None, close_failure=None):
        self.raw = raw
        self.read_failure, self.close_failure = read_failure, close_failure
        self.read_calls = self.close_calls = 0

    def read(self, *args):
        self.read_calls += 1
        if self.read_failure is not None:
            raise self.read_failure
        return self.raw.read(*args)

    def close(self):
        self.close_calls += 1
        if self.close_failure is not None and self.close_calls == 1:
            raise self.close_failure
        self.raw.close()


@pytest.mark.parametrize("failure_type", [None, OSError, KeyboardInterrupt])
def test_s3_cleanup_retains_first_failure_and_retries_only_body(
    monkeypatch, failure_type
):
    s3 = S3Stub()
    store = AwsExecutionStore(
        dynamodb=DynamoStub(),
        s3=s3,
        table="fixture",
        bucket="fixture",
        retention_days=1,
    )
    identity = store.put_object({"fixture": "retained-value"})
    read_failure = None if failure_type is None else failure_type("fixture read failed")
    close_failure = OSError("fixture body close failed")
    original_get, bodies = s3.get_object, []

    def get(**args):
        result = original_get(**args)
        body = ResponseBody(
            result["Body"], read_failure=read_failure, close_failure=close_failure
        )
        bodies.append(body)
        result["Body"] = body
        return result

    monkeypatch.setattr(s3, "get_object", get)
    try:
        with pytest.raises(BaseException) as caught:
            store.get_object(identity)
        failure = caught.value
        assert failure is (read_failure or close_failure)
        pending = failure.__cause__
        assert isinstance(pending, IncompleteRollback)
        assert pending.failure is failure and pending.errors == (close_failure,)
        assert len(bodies) == 1
        body = bodies[0]
        assert (body.read_calls, body.close_calls, body.raw.closed) == (1, 1, False)
        calls_before_retry = len(s3.calls)
        assert pending.retry() is True
        assert pending.retry() is True
        assert (body.read_calls, body.close_calls, body.raw.closed) == (1, 2, True)
        assert len(s3.calls) == calls_before_retry
    finally:
        for body in bodies:
            body.raw.close()


def readback_fixture(mode, *, close_failure=None, read_failure=None):
    class LambdaStub:
        def __init__(self):
            self.calls, self.bodies = [], []

        def invoke(self, **args):
            self.calls.append(args)
            challenge = json.loads(args["Payload"])
            now = datetime.now(UTC)
            raw = encode(
                {
                    "body": {
                        "challenge_sha256": digest(challenge),
                        "at": (now - timedelta(seconds=1)).isoformat(),
                        "expires_at": (now + timedelta(seconds=20)).isoformat(),
                        "payload": {"fixture": "readback"},
                    },
                    "signature": base64.b64encode(b"fixture-signature").decode(),
                }
            )
            body = ResponseBody(
                BytesIO(b"not-json" if mode == "malformed_json" else raw),
                read_failure=read_failure,
                close_failure=close_failure,
            )
            self.bodies.append(body)
            return {
                "StatusCode": 200,
                "Payload": body,
                **({"FunctionError": "Unhandled"} if mode == "function_error" else {}),
            }

    class KmsStub:
        def __init__(self):
            self.calls = []

        def verify(self, **args):
            self.calls.append(args)
            return {"SignatureValid": True, "KeyId": "fixture-key"}

    # The installed constructor would access AWS; allocate an inert object and
    # inject only these pure stubs at the public observe() readback boundary.
    runtime = object.__new__(InstalledRuntime)
    runtime._config = {
        "deployment_id": "fixture",
        "source_function_arn": "fixture-function",
        "signing_key_arn": "fixture-key",
    }
    source, kms = LambdaStub(), KmsStub()
    runtime._source, runtime._kms = source, kms
    runtime._cleanup = RollbackSlot()
    runtime._closing = False
    return runtime, source, kms


@pytest.mark.parametrize("mode", ["function_error", "malformed_json", "success"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_readback_releases_payload_on_success_and_validation_failure(mode, close_fails):
    close_failure = OSError("fixture payload close failed") if close_fails else None
    runtime, source, kms = readback_fixture(mode, close_failure=close_failure)
    pending = None
    try:
        if mode == "success" and not close_fails:
            assert runtime.observe("fixture", {"safe": "request"}) == {
                "fixture": "readback"
            }
        else:
            with pytest.raises(Exception) as caught:
                runtime.observe("fixture", {"safe": "request"})
            failure = caught.value
            if mode == "success":
                assert failure is close_failure
            else:
                expected = (
                    "trusted_readback_unavailable"
                    if mode == "function_error"
                    else "invalid_material_json"
                )
                assert isinstance(failure, ValueError) and str(failure) == expected
            if close_fails:
                pending = failure.__cause__
                assert isinstance(pending, IncompleteRollback)
                assert pending.failure is failure and pending.errors == (close_failure,)
        assert len(source.bodies) == 1
        body = source.bodies[0]
        assert body.close_calls == 1 and body.raw.closed is not close_fails
        assert len(source.calls) == 1
        calls_before_retry = len(kms.calls)
        if pending is not None:
            assert pending.retry() is True
            assert pending.retry() is True
            assert body.close_calls == 2 and body.raw.closed is True
            assert len(source.calls) == 1 and len(kms.calls) == calls_before_retry
    finally:
        for body in source.bodies:
            body.raw.close()


def test_control_receipt_does_not_discard_sdk_body_cleanup(tmp_path, monkeypatch):
    f, b, c, s3 = aws_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    _, prepared = prepare(f)
    original_get, bodies = s3.get_object, []

    def get(**args):
        result = original_get(**args)
        if not bodies:
            body = ResponseBody(
                result["Body"], close_failure=OSError("fixture SDK close unavailable")
            )
            bodies.append(body)
            result["Body"] = body
        return result

    monkeypatch.setattr(s3, "get_object", get)
    try:
        receipt = ControlService(auth).call(
            control_envelope(f, prepared), principal="simulation:worker"
        )
        assert receipt["state"] == "COMPLETED" and f["sends"] == []
        body = bodies[0]
        assert body.close_calls == 1 and body.raw.closed is False
        calls_before_retry = (
            len(s3.calls),
            len(b.calls),
            len(c.calls),
            body.read_calls,
        )
        # No exception escapes the public control API: the trusted backend must
        # retain ownership after its error has become a plain persisted receipt.
        assert auth.store.backend.retry_cleanup() is True
        assert auth.anchor.backend.retry_cleanup() is True
        assert auth.store.backend.retry_cleanup() is True
        assert auth.anchor.backend.retry_cleanup() is True
        assert body.close_calls == 2 and body.raw.closed is True
        assert (len(s3.calls), len(b.calls), len(c.calls), body.read_calls) == (
            calls_before_retry
        )
        assert f["sends"] == []
    finally:
        for body in bodies:
            body.raw.close()


def test_runtime_keeps_readback_cleanup_after_caller_discards_exception():
    runtime, source, kms = readback_fixture(
        "function_error", close_failure=OSError("fixture payload close unavailable")
    )

    def caller():
        try:
            runtime.observe("fixture", {"safe": "request"})
        except ValueError as error:
            return {"error": str(error)}
        raise AssertionError("fixture readback failure expected")

    try:
        assert caller() == {"error": "trusted_readback_unavailable"}
        body = source.bodies[0]
        assert body.close_calls == 1 and body.raw.closed is False
        calls_before_retry = (len(source.calls), len(kms.calls), body.read_calls)
        assert runtime.retry_cleanup() is True
        assert runtime.retry_cleanup() is True
        assert body.close_calls == 2 and body.raw.closed is True
        assert (
            len(source.calls),
            len(kms.calls),
            body.read_calls,
        ) == calls_before_retry
    finally:
        for body in source.bodies:
            body.raw.close()


@pytest.mark.parametrize("recover_body_first", [False, True])
def test_installed_shutdown_drains_readback_bodies_before_long_lived_clients(
    monkeypatch, recover_body_first
):
    config, clients, closes = inert_installed_sdk(monkeypatch)
    runtime = InstalledRuntime(config, token=runtime_module._INSTALL)
    fixture, source, kms = readback_fixture(
        "function_error", close_failure=OSError("fixture payload close unavailable")
    )
    config.update(fixture._config)
    clients["lambda"].invoke = source.invoke
    clients["kms"].verify = kms.verify
    try:
        with pytest.raises(ValueError, match="trusted_readback_unavailable"):
            runtime.observe("fixture", {})
        body = source.bodies[0]
        assert body.close_calls == 1 and closes == []
        for client in clients.values():
            close = client.close

            def close_after_body(close=close):
                assert body.raw.closed
                return close()

            monkeypatch.setattr(client, "close", close_after_body)
        if recover_body_first:
            assert runtime.retry_cleanup() is True
            assert runtime.retry_cleanup() is True
            assert body.close_calls == 2 and closes == []
        runtime.close()
        runtime.close()
        assert body.close_calls == 2 and body.raw.closed
        assert len(source.calls) == 1 and kms.calls == []
        assert closes == ["sts", "secretsmanager", "lambda", "kms"]
    finally:
        for body in source.bodies:
            body.raw.close()


@pytest.mark.parametrize(
    ("read_type", "close_type"),
    [(OSError, OSError), (KeyboardInterrupt, OSError), (OSError, KeyboardInterrupt)],
)
def test_readback_retains_read_failure_and_dominant_cancellation(read_type, close_type):
    read_failure = read_type("fixture payload read failed")
    close_failure = close_type("fixture payload close failed")
    runtime, source, kms = readback_fixture(
        "success", read_failure=read_failure, close_failure=close_failure
    )
    try:
        with pytest.raises(BaseException) as caught:
            runtime.observe("fixture", {"safe": "request"})
        expected = close_failure if close_type is KeyboardInterrupt else read_failure
        assert caught.value is expected
        pending = caught.value.__cause__
        assert isinstance(pending, IncompleteRollback)
        assert pending.failure is read_failure and pending.errors == (close_failure,)
        body = source.bodies[0]
        assert (body.read_calls, body.close_calls, body.raw.closed) == (1, 1, False)
        assert pending.retry() is True
        assert pending.retry() is True
        assert (body.read_calls, body.close_calls, body.raw.closed) == (1, 2, True)
        assert len(source.calls) == 1 and kms.calls == []
    finally:
        for body in source.bodies:
            body.raw.close()
