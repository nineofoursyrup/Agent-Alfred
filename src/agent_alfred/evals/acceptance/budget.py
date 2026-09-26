"""Batch authorization and per-transport-dispatch accounting, shared by judges."""

import math
import threading
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime

from agent_alfred.clock import SystemClock
from agent_alfred.model import ModelCallInterrupted, ModelError
from agent_alfred.resource_rollback import (
    ConstructionOwner,
    ResumableRollback,
    RollbackSlot,
)

from .execution_policy import bind_client, policy
from .schema import digest, judge_model, validate


def binding(batch):
    value = {
        "batch_id": batch["batch_id"],
        "phase": batch["phase"],
        "candidate_id": batch["candidate_id"],
        "cases": digest(batch["cases"]),
        "profiles": digest(batch["profiles"]),
        "judge_profile": digest(batch.get("judge_profile")),
        "rubric": digest(batch["rubric"]),
    }

    if batch["schema_version"] == 3:
        value.update(
            {
                key: digest(batch.get(key))
                for key in (
                    "semantic_rubric",
                    "review_policy",
                    "aggregation_policy",
                    "seen_families",
                )
            }
        )
    return value


def validate_authorization(auth, expected_binding):
    if auth is None:
        raise ValueError("authorization_missing")
    if auth["binding"] != expected_binding:
        raise ValueError("authorization_mismatch")
    if not auth["by"] or not auth["at"]:
        raise ValueError("authorization_identity_missing")
    from .report import instant

    instant(auth["at"])
    for key in ("max_requests", "max_output_tokens"):
        if type(auth[key]) is not int or auth[key] <= 0:
            raise ValueError("invalid_authorization_limit")
    seconds = auth["total_seconds"]
    if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("invalid_authorization_deadline")
    cost = auth["cost"]
    if not cost["source"]:
        raise ValueError("cost_source_missing")
    if cost["amount"] is None and auth["accept_unknown_cost"] is not True:
        raise ValueError("unknown_cost_not_accepted")
    if cost["amount"] is not None and (
        type(cost["amount"]) not in (float, int)
        or not math.isfinite(cost["amount"])
        or cost["amount"] < 0
    ):
        raise ValueError("invalid_cost")


class AuthorizedBatch:
    def __init__(self, batch, *, clock=None, session=None, mock_transport=None):
        self.source = deepcopy(batch)
        validate(batch)
        auth = batch["authorization"]
        validate_authorization(auth, binding(batch))
        self.session = session
        self.mock_transport = mock_transport
        self._clients = RollbackSlot()
        if session is not None:
            state = self.require_session()
            batch = {
                **batch,
                **{
                    k: state[k]
                    for k in (
                        "budget_started_at",
                        "requests",
                        "stop_reason",
                    )
                },
            }
            clock = session.authority.clock
        seconds = auth["total_seconds"]
        if batch.get("budget_scope") and batch["budget_scope"] != batch["batch_id"]:
            raise ValueError("new_regrade_budget_required")
        self.clock = clock or SystemClock()
        if (
            batch["schema_version"] == 3
            and datetime.fromisoformat(auth["at"]) > self.clock.wall_utc()
        ):
            raise ValueError("authorization_after_execution")
        self.started_at = batch.get(
            "budget_started_at", self.clock.wall_utc().isoformat()
        )
        started = datetime.fromisoformat(self.started_at)
        if started.tzinfo is None:
            raise ValueError("invalid_budget_time")
        elapsed = (self.clock.wall_utc() - started).total_seconds()
        if elapsed < 0:
            raise ValueError("invalid_budget_time")
        self.deadline = (
            state["started_monotonic"] + state["proposal"]["limits"]["total_seconds"]
            if session is not None
            else self.clock.monotonic() + seconds - elapsed
        )
        self.max_requests = auth["max_requests"]
        self.max_output_tokens = auth["max_output_tokens"]
        self.requests = deepcopy(batch.get("requests", []))
        self.journal = None
        self.trial = batch["phase"] == "trial"
        self.single_attempt = batch["schema_version"] == 3
        self.execution_policy = policy(batch)
        self.infrastructure_stop_reason = (
            "trial_infrastructure_failure"
            if self.trial
            else "execution_infrastructure_failure"
        )
        self.stop_reason = batch.get("stop_reason")
        self._lock = threading.Lock()
        self._ids = {r["attempt_id"] for r in self.requests}
        self._models = {
            "product": {
                (m["endpoint_id"], m["model_id"])
                for p in batch["profiles"]
                for m in p["product_models"]
            },
            "judge": {
                (
                    judge_model(batch, p)["endpoint_id"],
                    judge_model(batch, p)["model_id"],
                )
                for p in batch["profiles"]
            },
        }
        self.thinking = {}
        self.wire_styles = {}
        for profile in batch["profiles"]:
            for role, models in (
                ("product", profile["product_models"]),
                ("judge", [judge_model(batch, profile)]),
            ):
                for model in models:
                    key = (role, model["endpoint_id"], model["model_id"])
                    mode = model.get("thinking")
                    if key in self.thinking and self.thinking[key] != mode:
                        raise ValueError("conflicting_model_options")
                    self.thinking[key] = mode
                    style = model["wire_style"]
                    if key in self.wire_styles and self.wire_styles[key] != style:
                        raise ValueError("conflicting_model_options")
                    self.wire_styles[key] = style

    def check(self):
        if self.session is not None:
            self.session.check()
        if self.stop_reason:
            raise ValueError(self.stop_reason)
        if self.clock.monotonic() >= self.deadline:
            raise ValueError("batch_deadline")
        if len(self.requests) >= self.max_requests:
            raise ValueError("request_limit")

    def require_session(self):
        from .admission import require_source
        from .simulation_authority import SimulationSession

        require_source(self.source)
        if type(self.session) is not SimulationSession:
            raise ValueError("simulation_session_and_mock_required")
        return self.session.validate_binding(self.source)

    def start(
        self,
        attempt_id,
        role,
        model,
        *,
        max_tokens=None,
        request_digest=None,
        profile_id=None,
        _dispatch=False,
    ):
        self.require_session()
        with self._lock:
            self.check()
            if (
                role not in self._models
                or (model.endpoint_id, model.model_id) not in self._models[role]
            ):
                raise ValueError("unauthorized_model")
            if attempt_id in self._ids:
                raise ValueError("duplicate_attempt")
            row = {
                "attempt_id": attempt_id,
                "role": role,
                "model": asdict(model),
                "started_at": self.clock.wall_utc().isoformat(),
                "usage": None,
                "outcome": "unknown",
                "request_digest": request_digest,
            }
            if profile_id is not None:
                row["profile_id"] = profile_id
            reserved_tokens = max_tokens
            if reserved_tokens is None:
                reserved_tokens = self.max_output_tokens
                if role == "product":
                    reserved_tokens = min(
                        reserved_tokens,
                        self.source["profiles"][0]["parameters"].get(
                            "max_tokens", reserved_tokens
                        ),
                    )
            descriptor = {
                "row": row,
                "batch": self.source,
                "max_tokens": reserved_tokens,
            }
            if _dispatch:
                status = self.session.invoke(role, attempt_id, descriptor)
                if status["already_recorded"]:
                    raise ValueError("duplicate_attempt")
            else:
                # Synthetic fault fixtures may stop after a durable reservation.
                self.session.reserve(**descriptor)
            try:
                self._ids.add(attempt_id)
                self.requests = self.session.snapshot()["requests"]
                if self.journal:
                    self.journal(
                        "budget",
                        {
                            "requests": self.requests,
                            "budget_started_at": self.started_at,
                        },
                    )
            except BaseException as failure:
                if _dispatch:
                    try:
                        self.session.suspend_unresolved(attempt_id)
                    except BaseException as suspension_failure:
                        raise suspension_failure from failure
                raise

    def complete(self, result):
        with self._lock:
            error = result.final_error
            stop_on_error = self.execution_policy["stop_on_infrastructure_error"]
            if (
                stop_on_error
                and error
                and (
                    # Schema3 stops on any failed model call, including SDK
                    # transport failures with no status/code. Tool business
                    # refusals and judge-format errors are outside this seam.
                    self.single_attempt
                    or error.status_code in (400, 401, 403, 404, 422)
                    or error.code in ("invalid_response", "model_identity_mismatch")
                )
            ):
                self.stop_reason = self.stop_reason or self.infrastructure_stop_reason
            settled = []
            for attempt in result.attempts:
                for row in self.requests:
                    if row["attempt_id"] == attempt.attempt_id:
                        usage = asdict(attempt.usage)
                        cost = usage["endpoint_reported_cost_usd"]
                        if cost is not None:
                            usage["endpoint_reported_cost_usd"] = str(cost)
                        row.update(usage=usage, outcome=attempt.outcome)
                        if result.response and attempt == result.attempts[-1]:
                            actual = result.response.provider_model_id
                            row["provider_model_id"] = actual
                            if (
                                stop_on_error
                                and actual is not None
                                and actual != row["model"]["model_id"]
                            ):
                                self.stop_reason = self.infrastructure_stop_reason
                        settled.append(row)
                        break
                else:
                    raise ValueError("unobserved_attempt")

            if self.session is not None:
                self.session.record(settled, self.stop_reason)
                state = self.session.snapshot()
                self.requests = state["requests"]
                self.stop_reason = state["stop_reason"]
            if self.journal:
                self.journal(
                    "budget",
                    {
                        "requests": self.requests,
                        "budget_started_at": self.started_at,
                        "stop_reason": self.stop_reason,
                    },
                )

    def client(self, snapshot, *, role, mock_transport=None):
        self.require_session()
        transport = self.mock_transport if mock_transport is None else mock_transport
        owner = ResumableRollback()
        self._clients.begin(owner)
        return self.session.open_client(
            snapshot, self, role, transport, _rollback=owner
        )

    def close(self):
        self._clients.close()


class _SimulationDispatchClient:
    """Raw synthetic SDK client owned only by SimulationAuthority."""

    def __init__(self, snapshot, budget, role, mock_transport=None, *, _rollback=None):
        import httpx2 as httpx

        from agent_alfred.endpoint_factory import EndpointClientFactory
        from agent_alfred.model import ClientSnapshot

        budget.require_session()
        budget.check()
        if (
            type(snapshot) is not ClientSnapshot
            or type(mock_transport) is not httpx.MockTransport
        ):
            raise ValueError("simulation_mock_transport_required")
        if (
            role not in budget._models
            or (snapshot.endpoint_id, snapshot.model_id) not in budget._models[role]
        ):
            raise ValueError("unauthorized_model")
        if (
            snapshot.wire_style
            != budget.wire_styles[(role, snapshot.endpoint_id, snapshot.model_id)]
        ):
            raise ValueError("approval_profile_mismatch")
        if budget.single_attempt and (
            snapshot.stream is not budget.execution_policy["stream"]
            or snapshot.stream_fallback
            is not budget.execution_policy["stream_fallback"]
        ):
            raise ValueError("approval_profile_mismatch")
        snapshot, model, tokens, profile_id = bind_client(budget.source, snapshot, role)
        self.budget, self.role = budget, role
        self._model = model
        self._profile_id = profile_id
        self._profile_token_limit = tokens
        self._deadline = budget.clock.monotonic() + snapshot.overall_deadline_s
        self.http = None
        self.factory = None
        self.mock_transport = mock_transport
        self._close_owner = ResumableRollback()
        owner = ConstructionOwner(_rollback)
        owner.rollback.own(self)
        try:
            # The supplied transport is the only external resource of this
            # MockTransport-only HTTP client. Own it before constructing HTTP.
            self._close_owner.own(mock_transport, self._close_http)
            self.http = httpx.Client(transport=mock_transport, trust_env=False)
            self._close_owner.own(self, self._close_factory)
            self.factory = EndpointClientFactory(
                clock=budget.clock,
                http_client=self.http,
                max_retries=budget.execution_policy["max_retries"],
            )
            self.inner = self.factory.create(
                replace(snapshot, api_key="offline-synthetic-credential-admission")
            )
            owner.publish(self)
        except BaseException as failure:
            owner.fail(failure)

    def _close_http(self):
        # httpx marks itself closed before closing its transport. That flag
        # cannot retire a failed leaf; retry the supplied idempotent mock.
        if self.http is None or self.http.is_closed:
            self.mock_transport.close()
        else:
            self.http.close()

    def _close_factory(self):
        if self.factory is not None:
            self.factory.close()

    def close(self):
        self._close_owner.close()

    def respond(self, request, *, events=None, deadline=None):
        self.budget.require_session()
        self.budget.check()
        if (
            request.model.endpoint_id,
            request.model.model_id,
        ) not in self.budget._models[self.role]:
            raise ValueError("unauthorized_model")
        if (request.model.endpoint_id, request.model.model_id) != (
            self._model["endpoint_id"],
            self._model["model_id"],
        ):
            raise ValueError("approval_profile_mismatch")
        if (
            type(request.max_tokens) is not int
            or request.max_tokens <= 0
            or request.max_tokens > self.budget.max_output_tokens
        ):
            raise ValueError("output_token_limit")
        if request.max_tokens > self._profile_token_limit:
            raise ValueError("approval_profile_mismatch")
        from agent_alfred.model import NamedToolChoice, tool_schema_jsonable
        from agent_alfred.openai_compatible import _to_wire_messages

        from .schema import digest

        original = request.on_attempt_preflight
        signed_request = replace(
            request,
            thinking=self._model.get("thinking"),
            response_format=self._model.get("response_format"),
        )
        choice = signed_request.tool_choice
        request_digest = digest(
            {
                "model": asdict(signed_request.model),
                "messages": _to_wire_messages(signed_request),
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool_schema_jsonable(tool.input_schema),
                    }
                    for tool in signed_request.tools
                ],
                "max_tokens": signed_request.max_tokens,
                "tool_choice": asdict(choice)
                if isinstance(choice, NamedToolChoice)
                else choice,
                "thinking": signed_request.thinking,
                "response_format": signed_request.response_format,
                "conversation_id": signed_request.conversation_id,
            }
        )
        dispatched = False
        dispatched_ids = []

        def preflight(attempt_id, effective_deadline):
            nonlocal dispatched
            if dispatched and self.budget.single_attempt:
                self.budget.stop_reason = "execution_policy_violation"
                raise ValueError("execution_retry_forbidden")
            if original:
                original(attempt_id, effective_deadline)
            self.budget.start(
                attempt_id,
                self.role,
                request.model,
                max_tokens=request.max_tokens,
                request_digest=request_digest,
                profile_id=self._profile_id,
                _dispatch=True,
            )
            dispatched = True
            dispatched_ids.append(attempt_id)

        capped = min(self._deadline, self.budget.deadline)
        if deadline is not None:
            capped = min(deadline, capped)
        try:
            try:
                result = self.inner.respond(
                    replace(
                        signed_request,
                        on_attempt_preflight=preflight,
                    ),
                    events=events,
                    deadline=capped,
                )
            except ModelCallInterrupted as error:
                self.budget.stop_reason = self.budget.stop_reason or (
                    "judge_interrupted"
                    if self.role == "judge"
                    else self.budget.infrastructure_stop_reason
                )
                self.budget.complete(error.result)
                raise
            self.budget.complete(result)
        except BaseException as failure:
            for attempt_id in dispatched_ids:
                try:
                    self.budget.session.suspend_unresolved(attempt_id)
                except BaseException as suspension_failure:
                    raise suspension_failure from failure
            raise
        if (
            self.budget.execution_policy["stop_on_infrastructure_error"]
            and result.response
        ):
            actual = result.response.provider_model_id
            if actual is not None and actual != request.model.model_id:
                result = replace(
                    result,
                    response=None,
                    final_error=ModelError(
                        False,
                        None,
                        None,
                        result.attempts[-1].attempt_id,
                        code="model_identity_mismatch",
                    ),
                )
        return result


def validate_material_approval_timing(batch, *, started_at=None):
    """Check case and semantic approval against this batch's first execution fact."""
    if (
        batch["schema_version"] != 3
        or batch["phase"] not in ("calibration", "formal")
        or batch["simulation"]
    ):
        return None
    from .report import instant
    from .schema import scoring_rubric

    times = [instant(result["sampled_at"]) for result in batch["results"]]
    if batch.get("budget_started_at") is not None:
        times.append(instant(batch["budget_started_at"]))
    if started_at is not None:
        times.append(instant(started_at))
    if not times:
        return None
    cutoff = min(times)
    for name, proof in (
        ("case", batch.get("case_set_approval")),
        ("semantic", scoring_rubric(batch)["approval"]),
    ):
        if proof and instant(proof["at"]) > cutoff:
            raise ValueError(name + "_approval_after_execution")
    return cutoff


def validate_formal_approval_timing(batch, source, *, started_at=None):
    """Apply formal-only approval chronology after material timing is checked."""
    if (
        batch["schema_version"] != 3
        or batch["phase"] != "formal"
        or batch["simulation"]
    ):
        return
    from .report import instant

    cutoff = validate_material_approval_timing(batch, started_at=started_at)
    if cutoff is None:
        return
    calibrated_at = instant(source["calibration_approval"]["at"])
    if calibrated_at > cutoff:
        raise ValueError("calibration_approval_after_execution")
    aggregation = batch.get("aggregation_policy")
    if aggregation and aggregation["approval"]["kind"] == "approved":
        approved_at = instant(aggregation["approval"]["at"])
        if approved_at > cutoff:
            raise ValueError("aggregation_approval_after_execution")
        if approved_at < calibrated_at:
            raise ValueError("aggregation_approval_before_calibration")


def validate_execution_materials(batch, store, *, started_at=None):
    """Resolve approved materials before any client can spend the batch budget."""
    store._validate_links(batch, ())
    if batch["simulation"]:
        return
    from .review_policy import approval_valid
    from .schema import scoring_rubric
    from .semantic_rules import case_approval_valid

    if not case_approval_valid(batch):
        raise ValueError("case_set_approval_missing")
    if batch["schema_version"] == 3 and not approval_valid(
        scoring_rubric(batch)["approval"], batch
    ):
        raise ValueError("approved_rubric_missing")
    if batch["schema_version"] == 3:
        from .report import instant

        if started_at is None:
            raise ValueError("execution_start_required")
        cutoff = validate_material_approval_timing(batch, started_at=started_at)
    if batch["phase"] == "formal":
        if not batch["calibration"]:
            raise ValueError("calibration_missing")
        rubric = scoring_rubric(batch)
        if not rubric or not approval_valid(rubric["approval"], batch):
            raise ValueError("approved_rubric_missing")
        if batch["schema_version"] == 3:
            policy = batch.get("aggregation_policy")
            if not policy or policy["approval"]["kind"] != "approved":
                raise ValueError("approved_aggregation_missing")
            if instant(policy["approval"]["at"]) > cutoff:
                raise ValueError("aggregation_approval_after_execution")
            source = store.read(batch["calibration"]["batch_id"])
            validate_formal_approval_timing(batch, source, started_at=started_at)
        from .calibration import validate_source

        validate_source(store.read(batch["calibration"]["batch_id"]), store)
