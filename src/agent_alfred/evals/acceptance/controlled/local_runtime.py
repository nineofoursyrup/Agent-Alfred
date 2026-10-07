"""Explicit installed local runtime: trusted owner/host, constrained runner.

No real configuration is shipped. Provider credentials are first read inside
send after the existing live Authority gates; all fixtures stay synthetic.
"""

import sys
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from time import get_clock_info

import httpx2 as httpx

from agent_alfred.resource_rollback import (
    ConstructionOwner,
    ResumableRollback,
    RollbackSlot,
    capture_call_result,
)

from ..candidate import verify_runtime
from ..materials import strict_json
from ..schema import digest, encode
from .contract import (
    ADVISORY_BUDGET,
    cost_units,
    group_for_model,
    input_limit,
    integer,
    is_product,
    model_group,
)
from .local_clock import ContinuityGuard, MacClock
from .local_installation import (
    _LOCAL_INSTALL,
    PROFILE,
    final_charge_path,
    inspect_local_installation,
    read_advisory_budget,
    read_billing,
    read_final_charge,
    read_readiness,
    review_request,
    verify_installation,
)
from .local_persistence import (
    _LOCAL_STORAGE,
    AppendWitnessDocuments,
    LocalExecutionStore,
    LocalExecutionWitness,
)
from .local_source import _LOCAL_SOURCE, LocalDecisionControl, LocalDecisionSource
from .local_tokens import estimate_judge_wire
from .runtime import _post_once

__all__ = [
    "LocalInstalledRuntime",
    "install_local_runtime",
    "inspect_local_installation",
]


class LocalInstalledRuntime:
    trust_profile = PROFILE

    def __init__(self, config_path, *, token=None, _rollback=None):
        if token is not _LOCAL_INSTALL or sys.platform != "darwin":
            raise ValueError("trusted_local_installation_required")
        self._path = config_path
        (
            self._directory,
            self._config,
            self._bundle_manifest,
            self._bundle_verifier,
            self._host_verifier,
        ) = verify_installation(config_path)
        self._closing = False
        self._last_readiness = None
        self._bound_candidate_id = None
        if "mach_absolute_time" not in get_clock_info("monotonic").implementation:
            raise ValueError("local_awake_clock_unverifiable")
        self._continuity = ContinuityGuard()
        self._clock = MacClock()
        self._cleanup = RollbackSlot()
        self._resources = ResumableRollback()
        self._shutdown = ResumableRollback()
        self._shutdown.own(self._resources)
        self._shutdown.own(self._cleanup)
        owner = ConstructionOwner(_rollback)
        owner.rollback.own(self._shutdown)
        try:
            for attribute, cls, path_key in (
                ("store", LocalExecutionStore, "ledger_path"),
                ("anchor", LocalExecutionWitness, "witness_path"),
                ("_decisions", AppendWitnessDocuments, "decisions_path"),
            ):
                setattr(self, attribute, None)
                self._resources.own(attribute, partial(self._close_resource, attribute))
                capture_call_result(
                    self,
                    attribute,
                    partial(
                        cls,
                        self._directory,
                        self._config[path_key],
                        token=_LOCAL_STORAGE,
                        _rollback=owner.rollback,
                    ),
                )
                owner.rollback.transfer(getattr(self, attribute))
            self._decision_source = LocalDecisionSource(self, token=_LOCAL_SOURCE)
            self.decision_control = LocalDecisionControl(self, token=_LOCAL_SOURCE)
            owner.publish(self, parts=(self._shutdown,))
        except BaseException as error:
            owner.fail(error)

    @property
    def config(self):
        return deepcopy(self._config)

    def _close_resource(self, attribute):
        resource = getattr(self, attribute)
        if resource is not None:
            return resource.close()

    @property
    def bundle_manifest(self):
        return deepcopy(self._bundle_manifest)

    @property
    def bundle_verifier(self):
        return self._bundle_verifier

    @property
    def provenance(self):
        """Report the last verified observation; this is never a fresh grant."""
        return {
            "contract": "V1-LOCAL-RUNTIME-PROVENANCE",
            "version": 1,
            "trust_profile": PROFILE,
            "installation_id": self._config["installation_id"],
            "candidate_id": self._config["candidate_id"],
            "bundle_manifest": deepcopy(self._config["bundle_manifest"]),
            "host_environment": deepcopy(self._config["host_environment"]),
            "last_validated_readiness": deepcopy(self._last_readiness),
            "limitations": [
                "trusted_owner_and_host",
                "no_administrator_rollback_protection",
                "no_compromised_host_egress_guarantee",
                "no_independent_administrator_anchor",
            ],
            "online_executable": False,
        }

    @property
    def decision_source(self):
        return self._decision_source

    def verify_installation(self):
        if self._closing:
            raise ValueError("installed_runtime_closed")
        self._continuity.sample(self._clock.sample())
        verify_installation(
            self._path,
            self._config,
            verifier=self._bundle_verifier,
            host_verifier=self._host_verifier,
            with_manifest=False,
        )

    def continuity_checkpoint(self):
        self.verify_installation()
        value = self._clock.sample()
        self._continuity.sample(value)
        return value

    def resume_continuity(self, checkpoint):
        from .contract import exact

        exact(checkpoint, "wall awake sleep_interval")
        guard = ContinuityGuard()
        guard.sample(checkpoint)
        sample = self._clock.sample()
        try:
            guard.sample(sample)
            self._continuity.sample(sample)
        except ValueError:
            self._continuity.failed = True
            raise

    def check(self, plan, binding, batch, now):
        self.verify_installation()
        if (
            plan["execution_mode"] != "authorized"
            or binding["simulation"]
            or batch["simulation"]
            or plan["initial_phase"] != "diagnostic"
            or plan["runtime_candidate"] is None
            or not verify_runtime(plan["runtime_candidate"])
            or digest(plan["runtime_candidate"]) != plan["candidate_id"]
            or plan["candidate_id"] != self._config["candidate_id"]
        ):
            raise ValueError("runtime_candidate_mismatch")
        self._verify_packaged_candidate(plan["candidate_id"], plan["runtime_candidate"])
        products = [row for row in plan["operations"] if row["kind"] == "product"]
        if (
            len(products) != 30
            or len({row["object_id"] for row in products}) != 30
            or len(self._bundle_manifest["slots"]) != 30
        ):
            raise ValueError("local_thirty_case_installation_required")
        policy = plan.get("budget_policy")
        readiness = read_readiness(
            self._directory,
            self._config,
            now=now,
            budget_policy=policy,
            model_policy=plan.get("model_policy"),
        )
        if (
            readiness["plan_sha256"] != digest(plan)
            or readiness["binding_sha256"] != digest(binding)
            or readiness["worker"] != plan["worker"]
            or readiness["controller"] != plan["controller"]
        ):
            raise ValueError("runtime_readback_unverifiable")
        self.decision_source.verify_owner_record(
            readiness["approval_ref"],
            review_request("local_readiness", self._config, readiness),
        )
        billing = self._billing(
            now,
            budget_policy=policy,
            **(
                {"model_policy": plan["model_policy"]} if "model_policy" in plan else {}
            ),
        )
        if plan["pricing"] != billing["pricing"]:
            raise ValueError("billing_or_token_proof_mismatch")
        if (
            policy == ADVISORY_BUDGET
            and readiness["evidence"]["advisory_budget"] != billing["provider_evidence"]
        ):
            raise ValueError("local_advisory_evidence_mismatch")
        self._last_readiness = {
            "at": now.isoformat(),
            "budget_policy": policy or "final_liability_hard_cap",
            "final_bill_ceiling_proven": policy != ADVISORY_BUDGET,
            "readiness_sha256": digest(readiness),
            "readiness_source_ref": readiness["approval_ref"],
            "billing_sha256": digest(billing),
            "billing_source_ref": billing["approval_ref"],
            "evidence": deepcopy(readiness["evidence"]),
        }
        return {
            "contract": (
                "V3-LOCAL-FLASH-RUNTIME-READBACK"
                if plan.get("version") == 3
                else "V2-LOCAL-RUNTIME-READBACK"
                if policy == ADVISORY_BUDGET
                else "V1-LOCAL-RUNTIME-READBACK"
            ),
            "version": 3
            if plan.get("version") == 3
            else 2
            if policy == ADVISORY_BUDGET
            else 1,
            "trust_profile": PROFILE,
            "active": True,
            "candidate_id": plan["candidate_id"],
            "plan_sha256": digest(plan),
            "worker": plan["worker"],
            "controller": plan["controller"],
            "prerequisites": {
                key: value["sha256"] for key, value in readiness["evidence"].items()
            },
            "identities": deepcopy(readiness["identities"]),
            "independent_administrator_anchor": False,
            "administrator_rollback_protection": False,
        }

    def _verify_packaged_candidate(self, candidate_id, candidate):
        """Bind every copied Alfred package, once per immutable installation."""
        if self._bound_candidate_id == candidate_id:
            return
        prefix = "src/agent_alfred/"
        expected = {
            path.removeprefix(prefix): row["sha256"]
            for path, row in candidate["files"].items()
            if path.startswith(prefix) and row.get("kind") == "file"
        }
        manifest = self._bundle_manifest
        packages = {row["path"]: {} for row in [*manifest["slots"], manifest["probe"]]}
        marker = "/Contents/Resources/packages/agent_alfred/"
        for row in manifest["files"]:
            app, separator, name = row["path"].partition(marker)
            if separator and app in packages:
                packages[app][name] = row["sha256"]
        if not expected or any(observed != expected for observed in packages.values()):
            raise ValueError("local_packaged_candidate_mismatch")
        # Later checks still verify the original bundle's immutable file pins.
        # This cache never updates the manifest or accepts a changed package.
        self._bound_candidate_id = candidate_id

    def _billing(self, now, *, budget_policy=None, model_policy=None):
        self.verify_installation()
        if budget_policy == ADVISORY_BUDGET:
            billing = read_advisory_budget(
                self._directory, self._config, now=now, model_policy=model_policy
            )
            scope = "local_advisory_budget"
        elif budget_policy is None:
            billing = read_billing(self._directory, self._config, now=now)
            scope = "local_billing_bound"
        else:
            raise ValueError("local_budget_policy_invalid")
        self.decision_source.verify_owner_record(
            billing["approval_ref"],
            review_request(scope, self._config, billing),
        )
        return billing

    def visible_final_charge(self, row):
        """A newly deposited receipt must be reconciled before another dispatch."""
        path = final_charge_path(self._directory, row)
        return path.exists() or path.is_symlink()

    def quote(self, plan, operation, payload, now):
        policy = plan.get("budget_policy")
        billing = self._billing(
            now,
            budget_policy=policy,
            **(
                {"model_policy": plan["model_policy"]} if "model_policy" in plan else {}
            ),
        )
        group = model_group(plan, operation)
        if billing["pricing"] != plan["pricing"]:
            raise ValueError("billing_or_token_proof_mismatch")
        measurement = billing["input_measurement"]
        if (
            "input_policy" in plan
            and billing.get("input_policy") != plan["input_policy"]
        ):
            raise ValueError("controlled_input_policy_invalid")
        if "input_policy" in plan and not is_product(operation):
            count = estimate_judge_wire(payload)
        else:
            numerator = len(encode(payload)) * measurement["numerator"]
            count = (numerator + measurement["denominator"] - 1) // measurement[
                "denominator"
            ] + measurement["overhead"]
        if count > input_limit(plan, operation):
            raise ValueError("local_input_preflight_exceeded")
        integer(count)
        readiness = read_readiness(
            self._directory,
            self._config,
            now=now,
            budget_policy=policy,
            model_policy=plan.get("model_policy"),
        )
        if policy == ADVISORY_BUDGET:
            if readiness["evidence"]["advisory_budget"] != billing["provider_evidence"]:
                raise ValueError("local_advisory_evidence_mismatch")
            return {
                "budget_policy": ADVISORY_BUDGET,
                "payload_sha256": digest(payload),
                "input_estimate": count,
                "terms": deepcopy(billing["pricing"][group]),
                "token_evidence": self.store.put_object(
                    {
                        "advisory_budget": billing,
                        "complete_payload_sha256": digest(payload),
                        "complete_wire_bytes": len(encode(payload)),
                        "input_estimate": count,
                    }
                ),
                "identity_evidence": readiness["identities"]["evidence"],
            }
        return {
            "payload_sha256": digest(payload),
            "input_tokens": count,
            "terms": deepcopy(billing["pricing"][group]),
            "token_evidence": self.store.put_object(
                {
                    "billing_bound": billing,
                    "complete_payload_sha256": digest(payload),
                    "input_upper_bound": count,
                }
            ),
            "identity_evidence": readiness["identities"]["evidence"],
        }

    def settlement(self, row, response, quote):
        self.verify_installation()
        original = self.store.get_object(quote["token_evidence"])
        if original["complete_payload_sha256"] != row["payload_sha256"]:
            raise ValueError("settlement_unverifiable")
        advisory = quote.get("budget_policy") == ADVISORY_BUDGET
        billing = original["advisory_budget"] if advisory else original["billing_bound"]
        final = read_final_charge(
            self._directory,
            self._config,
            row,
            response,
            quote,
            billing,
            now=datetime.now(UTC),
        )
        if final is not None:
            self.decision_source.verify_owner_record(
                final["approval_ref"],
                review_request("local_final_charge", self._config, final),
            )
            return {
                "payload_sha256": row["payload_sha256"],
                "response_sha256": digest(response),
                "actual_units": (
                    final["normalized_usd_units"] if advisory else final["actual_units"]
                ),
                "evidence": self.store.put_object(final),
            }
        # Only the contract admitted for this exact Attempt may derive a charge.
        # A later account/price/mode approval cannot replace its original proof.
        # A final provider receipt above has its own fresh, exact review and does
        # not require an old send-time pricing window to remain open today.
        if advisory:
            self.decision_source.verify_owner_record(
                billing["approval_ref"],
                review_request("local_advisory_budget", self._config, billing),
            )
            raw = strict_json(response["raw"].encode(), limit=4 * 1024 * 1024)
            if raw.get("model") != row["model"]:
                raise ValueError("response_model_mismatch")
            usage = raw.get("usage", {})
            integer(usage.get("prompt_tokens"))
            integer(usage.get("completion_tokens"), maximum=row["output_bound"])
            group = group_for_model(row["model"])
            if billing["pricing"][group] != quote["terms"]:
                raise ValueError("settlement_unverifiable")
            return {
                "contract": "V2-LOCAL-PLAN-READBACK",
                "version": 2,
                "payload_sha256": row["payload_sha256"],
                "response_sha256": digest(response),
                "liability_state": "RESERVED",
                "actual_units": None,
                "evidence": digest(
                    {
                        "advisory_budget": billing,
                        "provider_response": response,
                        "basis": "planned_reservation_until_verified_final_charge",
                    }
                ),
            }
        self.decision_source.verify_owner_record(
            billing["approval_ref"],
            review_request("local_billing_bound", self._config, billing),
        )
        raw = strict_json(response["raw"].encode(), limit=4 * 1024 * 1024)
        if raw.get("model") != row["model"]:
            raise ValueError("response_model_mismatch")
        usage = raw.get("usage", {})
        used_input = integer(usage.get("prompt_tokens"), maximum=row["input_bound"])
        used_output = integer(
            usage.get("completion_tokens"), maximum=row["output_bound"]
        )
        group = group_for_model(row["model"])
        if billing["pricing"][group] != quote["terms"] or billing[
            "liability_mode"
        ] not in ("bounded_final_liability_function", "exact_final_liability_function"):
            raise ValueError("settlement_unverifiable")
        if billing["liability_mode"] == "bounded_final_liability_function":
            return {
                "contract": "V1-LOCAL-LIABILITY-READBACK",
                "version": 2,
                "payload_sha256": row["payload_sha256"],
                "response_sha256": digest(response),
                "liability_state": "RESERVED",
                "actual_units": None,
                "evidence": digest(
                    {
                        "billing_bound": billing,
                        "provider_response": response,
                        "basis": "full_reservation_until_verified_final_charge",
                    }
                ),
            }
        # This path exists only for a provider/account contract proving the
        # complete exact final liability function. An estimate/balance/alert or
        # public tariff cannot populate the required billing evidence package.
        return {
            "payload_sha256": row["payload_sha256"],
            "response_sha256": digest(response),
            "actual_units": cost_units(quote["terms"], used_input, used_output),
            "evidence": digest(
                {
                    "billing_bound": billing,
                    "provider_response": response,
                    "basis": "response_usage_and_exact_final_liability_contract",
                }
            ),
        }

    def send(self, payload, *, preflight, timeout):
        self.verify_installation()
        preflight("credentials")
        raw = self._directory.read(self._config["credential_path"], limit=16384)
        try:
            key = raw.decode().strip()
        except UnicodeError:
            raise ValueError("model_credential_unavailable") from None
        if not key or "\n" in key or "\r" in key:
            raise ValueError("model_credential_unavailable")
        timeout = min(timeout, preflight("client"))
        return _post_once(
            httpx.HTTPTransport(retries=0),
            payload,
            key,
            preflight=preflight,
            timeout=timeout,
            cleanup=self._cleanup,
        )

    def retry_cleanup(self):
        return self._cleanup.retry()

    def close(self):
        self._closing = True
        self._shutdown.close()


def install_local_runtime(config_path, *, _rollback=None):
    """Explicit host operation. Never takes a source/credential/client callable."""
    owner = ConstructionOwner(_rollback)
    try:
        runtime = LocalInstalledRuntime(
            config_path, token=_LOCAL_INSTALL, _rollback=owner.rollback
        )
        owner.publish(runtime)
        return runtime
    except BaseException as failure:
        owner.fail(failure)
