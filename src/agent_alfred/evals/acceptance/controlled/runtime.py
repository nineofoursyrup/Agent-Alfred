"""Explicit installation of authenticated services, separate from worker input.

The real adapter loads only a root-owned immutable deployment file, pins an A
signing key and service ARN, authenticates its B role, and challenges fresh signed
readbacks. No environment switch, user source/factory, mock proof or local report
can select its real transport. Cloud calls here are for a separately authorized
installation; merely importing this module does no IO.
"""

import base64
import hashlib
import os
import stat
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from secrets import token_hex
from threading import Event, Thread
from time import monotonic

import httpx2 as httpx

from agent_alfred.resource_rollback import (
    ConstructionOwner,
    OwnedResource,
    ResumableRollback,
    RollbackSlot,
    capture_call_result,
)

from ..candidate import verify_runtime
from ..materials import strict_json
from ..schema import digest, encode, hash_value
from ..simulation_authority import SimulationAuthority
from ..supplement_decisions import instant
from .contract import URL, cost_units, exact, text
from .response_resources import close_body, owned_response
from .serialized_close import SerializedClose

_INSTALL = object()


class TransportFailure(ValueError):
    """Only inert diagnostics cross Dispatch; HTTP errors and owners stay here."""

    def __init__(self, *, diagnostics, response=None):
        graph = diagnostics["transport"] or diagnostics["cleanup"]
        first = graph["errors"][0]
        self.original_type, self.safe_message = first["type"], first["message"]
        super().__init__(self.safe_message)
        self.diagnostics = deepcopy(diagnostics)
        self.response = deepcopy(response)

    def record(self):
        return {
            "type": self.original_type,
            "message": self.safe_message,
            "diagnostics": deepcopy(self.diagnostics),
        }


def _failure_graph(error, key):
    """Keep causal shape, including cycles/groups, without exception references."""
    pending, indexes, records = [], {}, []

    def link(value):
        if value is None:
            return None
        identity = id(value)
        if identity not in indexes:
            indexes[identity] = len(pending)
            pending.append(value)
        return indexes[identity]

    link(error)
    for value in pending:
        try:
            message = str(value).replace(key, "[credential-redacted]")
        except BaseException:
            message = "unprintable_transport_error"
        records.append(
            {
                "type": type(value).__name__,
                "message": message,
                "cause": link(value.__cause__),
                "context": link(value.__context__),
                "suppress_context": value.__suppress_context__,
                "members": [link(e) for e in getattr(value, "exceptions", ())],
            }
        )
    return {"root": 0, "errors": records}


def _post_once(transport, payload, key, *, timeout, preflight, cleanup):
    """Own construction, the single send and retryable cleanup in trusted code."""
    done, owner = Event(), ResumableRollback()
    cleanup.begin(owner)
    client = None
    worker_active = False
    dispatch, cancel_ticket = {}, object()
    deadline = monotonic() + timeout
    cleanup_ready = True
    leaf = OwnedResource()
    leaf.publish(transport)
    outcome = {"response": None, "transport": None, "cleanup": None}

    def release_http():
        if client is None or client.is_closed:
            return leaf.close()
        return client.close()

    closing = SerializedClose(release_http, completed=leaf.close_completed)

    def close_http():
        if not cleanup_ready:
            return False  # The daemon still owns an in-flight request.
        return closing.close()

    owner.own(transport, close_http)

    def finish_cleanup():
        complete = owner.retry()
        if not complete:
            outcome["cleanup"] = _failure_graph(
                owner.errors[0]
                if owner.errors
                else RuntimeError("controlled_cleanup_incomplete"),
                key,
            )
        if complete:
            cleanup.complete(owner)
        done.set()

    def start_cleanup(*, wait):
        # Cancellation must return at the Attempt boundary even when close is
        # blocked. The same trusted owner remains reachable for later release.
        try:
            Thread(
                target=finish_cleanup, daemon=True, name="controlled-attempt-cleanup"
            ).start()
            if wait and not done.wait(max(0, deadline - monotonic())):
                outcome["cleanup"] = _failure_graph(
                    TimeoutError("controlled_cleanup_deadline"), key
                )
        except BaseException as error:
            outcome["cleanup"] = _failure_graph(error, key)

    def result():
        if outcome["transport"] or outcome["cleanup"]:
            # Raise after leaving every original except block. `from None`
            # alone would still export the credential-bearing __context__.
            raise TransportFailure(
                diagnostics={k: outcome[k] for k in ("transport", "cleanup")},
                response=outcome["response"],
            )
        return outcome["response"]

    class SendBoundary(httpx.BaseTransport):
        def handle_request(self, request):
            if dispatch:
                raise ValueError("controlled_send_cancelled")
            # Check live permission after scheduling and request construction.
            remaining = min(preflight("send"), deadline - monotonic())
            if remaining <= 0:
                raise ValueError("controlled_send_cancelled")
            request.extensions["timeout"] = httpx.Timeout(remaining).as_dict()
            ticket = object()
            # Cancellation and sending compete for exactly one built-in claim.
            # A rejected claim never enters the underlying transport.
            if dispatch.setdefault("winner", ticket) is not ticket:
                raise ValueError("controlled_send_cancelled")
            return transport.handle_request(request)

        def close(self):
            return leaf.close()

    try:
        client = httpx.Client(
            transport=SendBoundary(),
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
        )
        if deadline <= monotonic():
            raise ValueError("attempt_deadline")
    except BaseException as error:
        outcome["transport"] = _failure_graph(error, key)
    if outcome["transport"]:
        start_cleanup(wait=True)
        return result()

    def send():
        nonlocal worker_active, cleanup_ready
        try:
            if dispatch.get("winner") is cancel_ticket:
                return
            worker_active = True
            response = client.post(
                URL,
                content=encode(payload),
                timeout=max(0.001, deadline - monotonic()),
                headers={
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                },
            )
            outcome["response"] = {
                "status_code": response.status_code,
                "raw": response.text.replace(key, "[credential-redacted]"),
            }
        except BaseException as error:
            outcome["transport"] = _failure_graph(error, key)
        finally:
            cleanup_ready = True
            finish_cleanup()

    start_failure = None
    try:
        cleanup_ready = False
        Thread(target=send, daemon=True, name="controlled-single-attempt").start()
    except BaseException as error:
        start_failure = _failure_graph(error, key)
    if start_failure:
        dispatch.setdefault("winner", cancel_ticket)
        can_close = not worker_active
        if can_close:
            cleanup_ready = True
        if can_close:
            outcome["transport"] = start_failure
            start_cleanup(wait=True)
            return result()
        # Start may be interrupted after the daemon takes ownership. It keeps
        # the connection; the caller cannot close it or repeat the send.
        raise TransportFailure(
            diagnostics={"transport": start_failure, "cleanup": None}
        )
    wait_failure = None
    try:
        completed = done.wait(max(0, deadline - monotonic()))
    except BaseException as error:
        wait_failure = _failure_graph(error, key)
        completed = False
    if not completed:
        dispatch.setdefault("winner", cancel_ticket)
        can_close = not worker_active
        if can_close:
            cleanup_ready = True
        if can_close:
            start_cleanup(wait=False)
        if outcome["response"] is not None or outcome["transport"] is not None:
            # HTTP finished, but closing can itself exceed the Attempt window.
            # Preserve its first response/error while the trusted owner remains.
            raise TransportFailure(
                diagnostics={
                    "transport": outcome["transport"],
                    "cleanup": outcome["cleanup"]
                    or wait_failure
                    or _failure_graph(TimeoutError("controlled_cleanup_deadline"), key),
                },
                response=outcome["response"],
            )
        wait_failure = wait_failure or _failure_graph(
            TimeoutError("controlled_attempt_deadline_may_have_sent"), key
        )
        raise TransportFailure(
            diagnostics={"transport": wait_failure, "cleanup": outcome["cleanup"]}
        )
    return result()


class SyntheticCredentials:
    def __init__(self):
        self.reads = 0

    def read(self):
        self.reads += 1
        return "synthetic-controlled-credential"


class SyntheticRuntime:
    """Only a MockTransport can consume these visibly synthetic attestations."""

    def __init__(self, *, source, transport, credentials, input_tokens=100):
        if (
            type(source) is not SimulationAuthority
            or type(transport) is not httpx.MockTransport
            or type(credentials) is not SyntheticCredentials
        ):
            raise ValueError("synthetic_controlled_runtime_required")
        self.source, self.transport, self.credentials = source, transport, credentials
        self.input_tokens = input_tokens
        self.tokens_by_payload = {}
        self.pricing_override = None
        self.available = True
        self.clients_created = 0
        self.before_credentials = None
        self._cleanup = RollbackSlot()

    @property
    def decision_source(self):
        return self.source

    def check(self, plan, binding, batch, now):
        if (not binding["simulation"] or not batch["simulation"]) and plan[
            "execution_mode"
        ] != "synthetic_replay":
            raise ValueError("synthetic_controlled_runtime_required")
        if not self.available:
            raise ValueError("runtime_readback_unverifiable")
        if (
            plan["binding_sha256"] != digest(binding)
            or plan["material_candidate_id"] != batch["candidate_id"]
        ):
            raise ValueError("runtime_candidate_mismatch")
        return {"mode": "SYNTHETIC_ONLY", "online_executable": False}

    def quote(self, plan, operation, payload, now):
        group = "flash" if operation["kind"] in ("product", "auxiliary") else "pro"
        return {
            "payload_sha256": digest(payload),
            "input_tokens": self.tokens_by_payload.get(
                digest(payload), self.input_tokens
            ),
            "terms": deepcopy(self.pricing_override or plan["pricing"][group]),
            "token_evidence": "synthetic:complete-wire-token-proof",
            "identity_evidence": "synthetic:distinct-resolved-models",
        }

    def send(self, payload, *, preflight, timeout):
        # Three checks bracket model credential reading, client construction and
        # the exact request send. The final check consumes a one-shot send latch.
        if self.before_credentials:
            self.before_credentials()
        preflight("credentials")
        key = self.credentials.read()
        timeout = min(timeout, preflight("client"))
        self.clients_created += 1
        return _post_once(
            self.transport,
            payload,
            key,
            preflight=preflight,
            timeout=timeout,
            cleanup=self._cleanup,
        )

    def retry_cleanup(self):
        """Trusted-side resource recovery; never retries a model invocation."""
        return self._cleanup.retry()

    def settlement(self, row, response, quote):
        raw = strict_json(response["raw"].encode(), limit=4 * 1024 * 1024)
        usage = raw["usage"]
        return {
            "payload_sha256": row["payload_sha256"],
            "response_sha256": digest(response),
            "actual_units": cost_units(
                quote["terms"], usage["prompt_tokens"], usage["completion_tokens"]
            ),
            "evidence": "synthetic:verified-settlement",
        }


class InstalledDecisionSource:
    """Only the installed A service can supply current source events."""

    def __init__(self, runtime, token=None):
        if token is not _INSTALL or type(runtime) is not InstalledRuntime:
            raise ValueError("trusted_installation_required")
        self._runtime = runtime

    def authorize(self, binding, subject):
        if binding["simulation"] or not subject:
            raise ValueError("approval_source_unverifiable")

    def read_decision(self, source_ref):
        result = self._runtime.observe("decision", {"source_ref": source_ref})
        if result.get("revoked") is not False or result.get("source_ref") != source_ref:
            raise ValueError("approval_source_unverifiable")
        event = result.get("event")
        if type(event) is not dict or event.get("source_ref") != source_ref:
            raise ValueError("approval_source_unverifiable")
        return event

    def read_scoped_decision(self, request):
        """Read an actual latest event without rewriting an approval-era batch."""
        result = self._runtime.observe("scoped_decision", {"request": request})
        if (
            result.get("request_sha256") != digest(request)
            or result.get("revoked") is not False
        ):
            raise ValueError("approval_source_unverifiable")
        event = result.get("event")
        if type(event) is not dict:
            raise ValueError("approval_source_unverifiable")
        return event


class InstalledRuntime:
    """Installed clients live until close(); response cleanup is independent."""

    def __init__(self, config, *, token=None, _rollback=None):
        if token is not _INSTALL:
            raise ValueError("trusted_installation_required")
        import boto3
        from botocore.config import Config

        self._config = config
        self._cleanup = RollbackSlot()
        self._closing = False
        self._clients = ResumableRollback()
        self._shutdown = ResumableRollback()
        self._shutdown.own(self._clients)
        self._shutdown.own(self._cleanup)
        owner = ConstructionOwner(_rollback)
        owner.rollback.own(self._shutdown)
        try:
            # Retry-free signed readbacks; any loss is a blocking observation.
            options = Config(
                retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15
            )
            for attribute, service in (
                ("_kms", "kms"),
                ("_source", "lambda"),
                ("_secrets", "secretsmanager"),
                ("_sts", "sts"),
            ):
                setattr(self, attribute, None)
                self._clients.own(attribute, partial(self._close_client, attribute))
                # Register first, then capture the SDK return before the next
                # caller instruction. Third-party factory internals remain the
                # SDK's responsibility; they cannot publish into our owner.
                capture_call_result(
                    self,
                    attribute,
                    partial(
                        boto3.client,
                        service,
                        region_name=config["region"],
                        config=options,
                    ),
                )
            identity = self._sts.get_caller_identity()
            if identity.get("Arn") != config["principal"]:
                raise ValueError("installed_runtime_identity_mismatch")
            self._decision_source = InstalledDecisionSource(self, _INSTALL)
            owner.publish(self, parts=(self._shutdown,))
        except BaseException as failure:
            owner.fail(failure)

    def _close_client(self, attribute):
        client = getattr(self, attribute)
        if client is not None:
            # SDK client close is repeatable if its own return is interrupted.
            return client.close()

    def close(self):
        """Terminate this runtime, draining bodies before the SDK clients."""
        self._closing = True
        self._shutdown.close()

    @property
    def decision_source(self):
        return self._decision_source

    def observe(self, purpose, request):
        if self._closing:
            raise ValueError("installed_runtime_closed")
        nonce = token_hex(32)
        challenge = {
            "contract": "V1-CONTROLLED-READBACK",
            "version": 1,
            "deployment_id": self._config["deployment_id"],
            "purpose": purpose,
            "nonce": nonce,
            "request": deepcopy(request),
        }
        with owned_response(
            self._cleanup,
            partial(
                self._source.invoke,
                FunctionName=self._config["source_function_arn"],
                InvocationType="RequestResponse",
                Payload=encode(challenge),
            ),
            close=partial(close_body, field="Payload"),
        ) as result:
            if result.get("FunctionError") or result.get("StatusCode") != 200:
                raise ValueError("trusted_readback_unavailable")
            envelope = strict_json(result["Payload"].read(512 * 1024 + 1))
        exact(envelope, "body signature", "trusted_readback_invalid")
        body = envelope["body"]
        exact(
            body, "challenge_sha256 at expires_at payload", "trusted_readback_invalid"
        )
        if body["challenge_sha256"] != digest(challenge):
            raise ValueError("trusted_readback_replay")
        now = datetime.now(UTC)
        if not instant(body["at"]) <= now < instant(body["expires_at"]):
            raise ValueError("trusted_readback_expired")
        if (instant(body["expires_at"]) - instant(body["at"])).total_seconds() > 60:
            raise ValueError("trusted_readback_invalid")
        try:
            signature = base64.b64decode(envelope["signature"], validate=True)
        except ValueError, TypeError:
            raise ValueError("trusted_readback_invalid") from None
        verified = self._kms.verify(
            KeyId=self._config["signing_key_arn"],
            Message=hashlib.sha256(encode(body)).digest(),
            MessageType="DIGEST",
            Signature=signature,
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if (
            verified.get("SignatureValid") is not True
            or verified.get("KeyId") != self._config["signing_key_arn"]
        ):
            raise ValueError("trusted_readback_signature_invalid")
        return body["payload"]

    def check(self, plan, binding, batch, now):
        if (
            plan["execution_mode"] != "authorized"
            or binding["simulation"]
            or batch["simulation"]
            or plan["initial_phase"] != "diagnostic"
            or plan["runtime_candidate"] is None
            or not verify_runtime(plan["runtime_candidate"])
        ):
            raise ValueError("runtime_candidate_mismatch")
        proof = self.observe(
            "runtime", {"plan_sha256": digest(plan), "binding_sha256": digest(binding)}
        )
        exact(
            proof,
            "active candidate_id plan_sha256 worker controller "
            "prerequisites identities",
        )
        if (
            proof["active"] is not True
            or proof["candidate_id"] != plan["candidate_id"]
            or proof["plan_sha256"] != digest(plan)
            or proof["worker"] != plan["worker"]
            or proof["controller"] != plan["controller"]
        ):
            raise ValueError("runtime_readback_unverifiable")
        required = {
            "source",
            "isolation",
            "hard_cap",
            "deployment",
            "capacity",
            "phases",
            "billing",
            "model_identity",
        }
        if (
            type(proof["prerequisites"]) is not dict
            or set(proof["prerequisites"]) != required
        ):
            raise ValueError("real_execution_prerequisites_unverified")
        for value in proof["prerequisites"].values():
            hash_value(value)
        identities = exact(proof["identities"], "flash pro evidence valid_until")
        if (
            not identities["flash"]
            or not identities["pro"]
            or identities["flash"] == identities["pro"]
        ):
            raise ValueError("actual_model_identity_unverifiable")
        hash_value(identities["evidence"])
        if now >= instant(identities["valid_until"]):
            raise ValueError("actual_model_identity_expired")
        return proof

    def quote(self, plan, operation, payload, now):
        return self.observe(
            "complete_payload_quote",
            {
                "plan_sha256": digest(plan),
                "operation_id": operation["id"],
                "payload": payload,
            },
        )

    def settlement(self, row, response, quote):
        return self.observe(
            "settlement", {"attempt": row, "response": response, "quote": quote}
        )

    def send(self, payload, *, preflight, timeout):
        if self._closing:
            raise ValueError("installed_runtime_closed")
        preflight("credentials")
        key = self._secrets.get_secret_value(SecretId=self._config["secret_arn"])[
            "SecretString"
        ]
        if type(key) is not str or not key:
            raise ValueError("model_credential_unavailable")
        timeout = min(timeout, preflight("client"))
        # No SDK layer, no retries, proxies, redirects, stream or fallback.
        return _post_once(
            httpx.HTTPTransport(retries=0),
            payload,
            key,
            preflight=preflight,
            timeout=timeout,
            cleanup=self._cleanup,
        )

    def retry_cleanup(self):
        """Recover HTTP and SDK bodies only; close() terminates the runtime."""
        return self._cleanup.retry()


def install_runtime(config_path, *, _rollback=None):
    """Privileged explicit installation; never accepts a source or client factory."""
    path = Path(config_path).absolute()
    for node in (path, *path.parents):
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("installation_not_administrator_protected")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("installation_not_administrator_protected")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            config = strict_json(stream.read(16385), limit=16384)
    finally:
        os.close(descriptor)
    exact(
        config,
        "version region principal deployment_id source_function_arn "
        "signing_key_arn secret_arn",
    )
    if type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("installation_version_invalid")
    for value in config.values():
        if type(value) is not int:
            text(value)
    if (
        not config["source_function_arn"].startswith("arn:aws:lambda:")
        or not config["signing_key_arn"].startswith("arn:aws:kms:")
        or not config["secret_arn"].startswith("arn:aws:secretsmanager:")
    ):
        raise ValueError("installation_target_invalid")
    owner = ConstructionOwner(_rollback)
    try:
        runtime = InstalledRuntime(config, token=_INSTALL, _rollback=owner.rollback)
        owner.publish(runtime)
        return runtime
    except BaseException as failure:
        owner.fail(failure)


def validate_runtime(runtime):
    if type(runtime) is not SyntheticRuntime and not is_installed_runtime(runtime):
        raise ValueError("approval_source_unverifiable")
    return runtime


def is_installed_runtime(runtime):
    """Closed real adapters; no structural protocol or user factory admission."""
    from .local_runtime import LocalInstalledRuntime

    return type(runtime) in (InstalledRuntime, LocalInstalledRuntime)


def is_installed_source(source):
    from .local_source import LocalDecisionSource

    return type(source) in (InstalledDecisionSource, LocalDecisionSource)
