"""Closed owner-trusted installation and evidence contracts, without credentials.

Inspection never installs, authenticates an owner, initializes storage or sends a
request. Evidence bytes and their provenance remain distinct from a local review
receipt; a local hash or human approval cannot establish provider billing facts.
"""

import hashlib
import os
import sys
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from ..materials import strict_json
from ..schema import digest, hash_value
from ..supplement_decisions import instant
from .contract import exact, integer, text
from .local_files import OwnerDirectory

CONTRACT = "V1-LOCAL-INSTALLATION"
READINESS = "V1-LOCAL-READINESS"
ADVISORY_READINESS = "V2-LOCAL-READINESS"
ADVISORY_BILLING = "V2-LOCAL-ADVISORY-BUDGET"
FLASH_READINESS = "V3-LOCAL-FLASH-READINESS"
FLASH_BILLING = "V3-LOCAL-FLASH-ADVISORY-BUDGET"
PROFILE = "owner_trusted_local"
MAX_INSTALLATION_INVENTORY_BYTES = 64 * 1024 * 1024
_LOCAL_INSTALL = object()
REQUIRED_EVIDENCE = frozenset(
    (
        "source",
        "isolation",
        "deployment",
        "capacity",
        "phases",
        "billing",
        "model_identity",
        "hard_cap",
    )
)
ADVISORY_REQUIRED_EVIDENCE = (REQUIRED_EVIDENCE - {"billing", "hard_cap"}) | {
    "advisory_budget"
}
EVIDENCE_CHECKS = {
    "source": {
        "owner_authentication",
        "exact_object_and_scope",
        "revocation_readback",
        "runner_cannot_issue",
    },
    "isolation": {
        "credential_read_denied",
        "owner_files_read_denied",
        "decision_write_denied",
        "ledger_write_denied",
        "witness_write_denied",
        "candidate_write_denied",
        "cross_case_read_denied",
        "inherited_fds_absent",
        "child_same_boundary",
        "privilege_escalation_denied",
        "ipv4_denied",
        "ipv6_denied",
        "tcp_denied",
        "udp_denied",
        "dns_denied",
        "loopback_denied",
        "proxy_bypass_denied",
        "system_delegation_denied",
    },
    "deployment": {
        "bundle_signatures",
        "os_boundary_policy",
        "loaded_candidate",
        "host_environment",
        "single_use_slots",
    },
    "capacity": {
        "full_material_intake",
        "worst_case_576_attempts",
        "complete_event_inventory",
        "disk_space",
    },
    "phases": {
        "diagnostic36_checkpoint",
        "product30_first_results",
        "review48_blind_seal",
        "clock_budget_and_stop",
    },
    "billing": {
        "account_applicability",
        "complete_fee_coverage",
        "wire_input_metering",
        "output_bound",
        "tax_fx_rounding",
        "failed_inflight_liability",
        "settlement_semantics",
    },
    "model_identity": {
        "flash_resolved_identity",
        "pro_resolved_identity",
        "distinct_effective_models",
        "account_route",
    },
    "hard_cap": {
        "atomic_shared_reservation",
        "runner_no_billing_bypass",
        "unknown_no_refund",
        "no_retry_or_redirect",
        "account_other_consumers",
        "final_liability_upper_bound",
    },
    "advisory_budget": {
        "official_price_snapshot",
        "account_currency_and_attribution",
        "complete_wire_preflight",
        "dispatch_reservation_and_stop",
        "unknown_send_and_charge_retained",
        "non_guaranteed_bill_and_tokens_disclosed",
    },
}


def load_installation(config_path):
    path = Path(config_path).absolute()
    directory = OwnerDirectory(path.parent, os.geteuid())
    config = directory.json(path, limit=64 * 1024)
    exact(
        config,
        "contract version trust_profile owner_uid installation_id candidate_id "
        "worker controller subject protected_root bundle_root bundle_manifest "
        "owner_helper host_python_sha256 host_environment readiness_path "
        "billing_path decisions_path "
        "ledger_path witness_path credential_path",
        "local_installation_contract_invalid",
    )
    if (
        config["contract"] != CONTRACT
        or type(config["version"]) is not int
        or config["version"] != 1
        or config["trust_profile"] != PROFILE
        or config["owner_uid"] != os.geteuid()
        or Path(config["protected_root"]) != directory.path
        or config["worker"] == config["controller"]
    ):
        raise ValueError("local_installation_contract_invalid")
    for field in ("installation_id", "candidate_id", "worker", "controller", "subject"):
        text(config[field])
    hash_value(config["candidate_id"])
    hash_value(config["host_python_sha256"])
    exact(config["bundle_manifest"], "path sha256")
    hash_value(config["bundle_manifest"]["sha256"])
    exact(config["host_environment"], "path sha256")
    hash_value(config["host_environment"]["sha256"])
    paths = []
    for name in (
        "readiness_path",
        "billing_path",
        "decisions_path",
        "ledger_path",
        "witness_path",
        "credential_path",
    ):
        target = Path(config[name])
        if not target.is_absolute() or target.parent != directory.path:
            raise ValueError("local_path_outside_protected_directory")
        paths.append(target)
    if len(set(paths)) != len(paths) or path in paths:
        raise ValueError("local_storage_paths_not_distinct")
    return directory, config


@lru_cache(maxsize=1)
def _bundle_metadata(raw):
    # Cache only immutable fields derived from these exact, bounded JSON bytes.
    # Callers still reread the protected file and verify its digest on every use.
    manifest = strict_json(raw, limit=MAX_INSTALLATION_INVENTORY_BYTES)
    if type(manifest) is not dict:
        raise ValueError("local_bundle_source_candidate_mismatch")
    return manifest.get("source_id"), manifest["entrypoints"]["owner_helper"]


def verify_installation(
    config_path, expected=None, *, verifier=None, host_verifier=None, with_manifest=True
):
    """Verify live installation pins; omit the large return value when unused."""
    directory, config = load_installation(config_path)
    if expected is not None and config != expected:
        raise ValueError("local_installation_changed")
    manifest_ref = config["bundle_manifest"]
    raw = directory.read(manifest_ref["path"], limit=MAX_INSTALLATION_INVENTORY_BYTES)
    if hashlib.sha256(raw).hexdigest() != manifest_ref["sha256"]:
        raise ValueError("local_bundle_manifest_changed")
    if verifier is None or with_manifest:
        manifest = strict_json(raw, limit=MAX_INSTALLATION_INVENTORY_BYTES)
        if type(manifest) is not dict:
            raise ValueError("local_bundle_source_candidate_mismatch")
        source_id = manifest.get("source_id")
    else:
        manifest = None
        source_id, helper_entrypoint = _bundle_metadata(raw)
    if source_id != config["candidate_id"]:
        raise ValueError("local_bundle_source_candidate_mismatch")
    from .native import NativeBundleVerifier

    if verifier is None:
        verifier = NativeBundleVerifier(config["bundle_root"], manifest)
    elif type(verifier) is not NativeBundleVerifier:
        raise ValueError("local_bundle_verifier_required")
    bundle_report = verifier.verify()
    if bundle_report.get("slot_count") != 30:
        raise ValueError("local_thirty_single_use_slots_required")
    if manifest is not None:
        helper_entrypoint = manifest["entrypoints"]["owner_helper"]
    helper = Path(config["bundle_root"]) / helper_entrypoint
    if helper != Path(config["owner_helper"]):
        raise ValueError("local_owner_helper_mismatch")
    # The runner's Python is in the bundle inventory; this separately pins the
    # trusted host interpreter. verify_runtime pins the loaded Alfred package.
    executable = Path(sys.executable).resolve()
    if (
        hashlib.sha256(executable.read_bytes()).hexdigest()
        != config["host_python_sha256"]
    ):
        raise ValueError("local_host_interpreter_changed")
    host_ref = config["host_environment"]
    raw = directory.read(host_ref["path"], limit=MAX_INSTALLATION_INVENTORY_BYTES)
    if hashlib.sha256(raw).hexdigest() != host_ref["sha256"]:
        raise ValueError("local_host_environment_manifest_changed")
    from .local_host import HostEnvironmentVerifier

    if host_verifier is None:
        host_verifier = HostEnvironmentVerifier(
            strict_json(raw, limit=MAX_INSTALLATION_INVENTORY_BYTES)
        )
    elif type(host_verifier) is not HostEnvironmentVerifier:
        raise ValueError("local_host_verifier_required")
    host_verifier.verify()
    return (
        directory,
        config,
        manifest if with_manifest else None,
        verifier,
        host_verifier,
    )


def read_evidence(directory, reference, *, kind, config, now, model_policy=None):
    exact(reference, "path sha256")
    hash_value(reference["sha256"])
    raw = directory.read(reference["path"])
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("local_evidence_changed")
    value = directory.json(reference["path"])
    exact(
        value,
        "contract version kind installation_id candidate_id observed_at valid_until "
        "synthetic evidence_class origin observations",
        "local_evidence_invalid",
    )
    if (
        value["contract"] != "V1-LOCAL-VERIFICATION-EVIDENCE"
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["kind"] != kind
        or value["installation_id"] != config["installation_id"]
        or value["candidate_id"] != config["candidate_id"]
        or (
            kind == "phases"
            and (
                value["synthetic"] is not True
                or value["evidence_class"] != "offline_engineering"
            )
        )
        or (
            kind != "phases"
            and (
                value["synthetic"] is not False
                or value["evidence_class"] != "actual_environment_or_account"
            )
        )
        or not instant(value["observed_at"]) <= now < instant(value["valid_until"])
        or type(value["observations"]) is not list
        or not value["observations"]
    ):
        raise ValueError("real_execution_prerequisites_unverified")
    text(value["origin"])
    checks = [row.get("check") for row in value["observations"] if type(row) is dict]
    required_checks = EVIDENCE_CHECKS[kind]
    if model_policy == "flash_only" and kind == "model_identity":
        required_checks = {"flash_resolved_identity", "account_route"}
    if set(checks) != required_checks or len(checks) != len(set(checks)):
        raise ValueError("local_evidence_checks_incomplete")
    for observation in value["observations"]:
        exact(observation, "check expected actual raw_evidence")
        text(observation["check"])
        required_result = (
            "denied" if observation["check"].endswith("_denied") else "verified"
        )
        if (
            observation["expected"] != required_result
            or observation["actual"] != required_result
        ):
            raise ValueError("local_observation_failed")
        ref = exact(observation["raw_evidence"], "path sha256")
        if hashlib.sha256(directory.read(ref["path"])).hexdigest() != ref["sha256"]:
            raise ValueError("local_raw_evidence_changed")
        raw = directory.json(ref["path"])
        exact(
            raw,
            "contract version check installation_id candidate_id observed_at "
            "observer subject method result artifacts",
        )
        if (
            raw["contract"] != "V1-LOCAL-RAW-OBSERVATION"
            or type(raw["version"]) is not int
            or raw["version"] != 1
            or raw["check"] != observation["check"]
            or any(
                raw[key] != config[key] for key in ("installation_id", "candidate_id")
            )
            or not instant(value["observed_at"]) <= instant(raw["observed_at"]) <= now
            or raw["result"] != observation["actual"]
            or type(raw["artifacts"]) is not list
            or not raw["artifacts"]
        ):
            raise ValueError("local_raw_observation_unverifiable")
        for key in ("observer", "subject", "method"):
            text(raw[key])
        methods = {
            "source": {"native_owner_auth_and_readback"},
            "isolation": {"macos_process_probe_and_owner_readback"},
            "deployment": {"installed_artifact_inventory_and_signature_readback"},
            "capacity": {"actual_local_storage_synthetic_workload"},
            "phases": {"synthetic_offline_mechanism_validation"},
            "billing": {"provider_account_document_review"},
            "model_identity": {"provider_account_identity_readback"},
            "hard_cap": {"owner_verified_billing_bound_and_dispatch_probe"},
            "advisory_budget": {"owner_reviewed_account_and_dispatch_evidence"},
        }
        if raw["method"] not in methods[kind]:
            raise ValueError("local_observation_method_unverifiable")
        for artifact in raw["artifacts"]:
            exact(artifact, "path sha256")
            if (
                hashlib.sha256(directory.read(artifact["path"])).hexdigest()
                != artifact["sha256"]
            ):
                raise ValueError("local_raw_evidence_changed")
    return value


def read_readiness(directory, config, *, now, budget_policy=None, model_policy=None):
    from .contract import ADVISORY_BUDGET, FLASH_POLICY

    value = directory.json(config["readiness_path"])
    exact(
        value,
        "contract version trust_profile installation_id candidate_id plan_sha256 "
        "binding_sha256 worker controller valid_from valid_until evidence identities "
        "approval_ref",
        "local_readiness_invalid",
    )
    advisory = budget_policy == ADVISORY_BUDGET
    flash = model_policy == FLASH_POLICY
    if budget_policy not in (None, ADVISORY_BUDGET):
        raise ValueError("local_readiness_invalid")
    if model_policy not in (None, FLASH_POLICY) or (flash and not advisory):
        raise ValueError("local_readiness_invalid")
    if (
        value["contract"]
        != (FLASH_READINESS if flash else ADVISORY_READINESS if advisory else READINESS)
        or type(value["version"]) is not int
        or value["version"] != (3 if flash else 2 if advisory else 1)
        or value["trust_profile"] != PROFILE
        or any(
            value[key] != config[key]
            for key in ("installation_id", "candidate_id", "worker", "controller")
        )
        or not instant(value["valid_from"]) <= now < instant(value["valid_until"])
        or type(value["evidence"]) is not dict
        or set(value["evidence"])
        != (ADVISORY_REQUIRED_EVIDENCE if advisory else REQUIRED_EVIDENCE)
    ):
        raise ValueError("real_execution_prerequisites_unverified")
    for key in ("plan_sha256", "binding_sha256"):
        hash_value(value[key])
    for kind, reference in value["evidence"].items():
        read_evidence(
            directory,
            reference,
            kind=kind,
            config=config,
            now=now,
            model_policy=model_policy,
        )
    identities = exact(
        value["identities"],
        ("flash evidence valid_until" if flash else "flash pro evidence valid_until"),
    )
    if (
        not identities["flash"]
        or (
            not flash
            and (not identities["pro"] or identities["flash"] == identities["pro"])
        )
        or now >= instant(identities["valid_until"])
        or identities["evidence"] != value["evidence"]["model_identity"]["sha256"]
    ):
        raise ValueError("actual_model_identity_unverifiable")
    text(value["approval_ref"])
    return value


def review_request(scope, config, value):
    return {
        "scope": scope,
        "installation_id": config["installation_id"],
        "object_sha256": digest(
            {key: row for key, row in value.items() if key != "approval_ref"}
        ),
    }


def read_billing(directory, config, *, now):
    from .contract import COVERAGE, validate_terms

    value = directory.json(config["billing_path"])
    exact(
        value,
        "contract version installation_id candidate_id account_scope provider_origin "
        "provider_evidence verification liability_mode input_measurement pricing "
        "valid_from valid_until approval_ref",
        "local_billing_bound_unverifiable",
    )
    if (
        value["contract"] != "V1-LOCAL-BILLING-BOUND"
        or type(value["version"]) is not int
        or value["version"] != 1
        or any(value[key] != config[key] for key in ("installation_id", "candidate_id"))
        or value["liability_mode"]
        not in ("exact_final_liability_function", "bounded_final_liability_function")
        or not instant(value["valid_from"]) <= now < instant(value["valid_until"])
    ):
        raise ValueError("local_billing_bound_unverifiable")
    text(value["account_scope"])
    origin = urlparse(value["provider_origin"])
    if origin.scheme != "https" or origin.hostname not in (
        "api.deepseek.com",
        "api-docs.deepseek.com",
        "platform.deepseek.com",
    ):
        raise ValueError("local_billing_provider_origin_unverifiable")
    # A protected authentic provider document must be acquired and reviewed
    # outside this adapter. Its complete-account applicability is an explicit
    # #106 evidence obligation; neither a local signature nor public pricing is
    # promoted to that claim by this check.
    evidence = read_evidence(
        directory, value["provider_evidence"], kind="billing", config=config, now=now
    )
    if evidence["origin"] != value["provider_origin"]:
        raise ValueError("local_billing_provider_origin_unverifiable")
    verification = exact(
        value["verification"],
        "method complete_final_liability coverage account_attribution "
        "no_other_consumers no_automatic_credit",
    )
    if (
        verification["method"] != "provider_terms_account_applicability_review"
        or verification["complete_final_liability"] is not True
        or verification["coverage"] != COVERAGE
        or verification["account_attribution"] != value["account_scope"]
        or verification["no_other_consumers"] is not True
        or verification["no_automatic_credit"] is not True
    ):
        raise ValueError("local_billing_bound_unverifiable")
    if type(value["pricing"]) is not dict or set(value["pricing"]) != {"flash", "pro"}:
        raise ValueError("local_billing_bound_unverifiable")
    for group, terms in value["pricing"].items():
        validate_terms(terms, group)
    measurement = exact(
        value["input_measurement"],
        "method numerator denominator overhead provider_evidence_sha256",
    )
    if (
        measurement["method"] != "provider_certified_complete_wire_utf8_bound"
        or measurement["provider_evidence_sha256"]
        != value["provider_evidence"]["sha256"]
    ):
        raise ValueError("complete_payload_token_proof_unverifiable")
    integer(measurement["numerator"], minimum=1)
    integer(measurement["denominator"], minimum=1)
    integer(measurement["overhead"])
    text(value["approval_ref"])
    return value


def read_advisory_budget(directory, config, *, now, model_policy=None):
    """Review a planning source without upgrading it to final-liability proof."""
    from .contract import (
        ADVISORY_RISKS,
        FLASH_POLICY,
        flash_input_policy,
        validate_terms,
    )

    if model_policy not in (None, FLASH_POLICY):
        raise ValueError("local_advisory_budget_unverifiable")
    flash = model_policy == FLASH_POLICY

    value = directory.json(config["billing_path"])
    exact(
        value,
        "contract version installation_id candidate_id account_scope provider_origin "
        "provider_evidence verification pricing input_measurement valid_from "
        "valid_until approval_ref" + (" input_policy" if flash else ""),
        "local_advisory_budget_unverifiable",
    )
    if (
        value["contract"] != (FLASH_BILLING if flash else ADVISORY_BILLING)
        or type(value["version"]) is not int
        or value["version"] != (3 if flash else 2)
        or any(value[key] != config[key] for key in ("installation_id", "candidate_id"))
        or not instant(value["valid_from"]) <= now < instant(value["valid_until"])
    ):
        raise ValueError("local_advisory_budget_unverifiable")
    if flash and digest(value["input_policy"]) != digest(flash_input_policy()):
        raise ValueError("controlled_input_policy_invalid")
    text(value["account_scope"])
    origin = urlparse(value["provider_origin"])
    if origin.scheme != "https" or origin.hostname not in (
        "api.deepseek.com",
        "api-docs.deepseek.com",
        "platform.deepseek.com",
    ):
        raise ValueError("local_advisory_provider_origin_unverifiable")
    evidence = read_evidence(
        directory,
        value["provider_evidence"],
        kind="advisory_budget",
        config=config,
        now=now,
    )
    if evidence["origin"] != value["provider_origin"]:
        raise ValueError("local_advisory_provider_origin_unverifiable")
    verification = exact(
        value["verification"],
        "method account_attribution no_automatic_credit final_bill_ceiling_proven "
        "actual_input_ceiling_proven risks",
    )
    if (
        verification["method"] != "public_terms_and_account_readback"
        or verification["account_attribution"] != value["account_scope"]
        or verification["no_automatic_credit"] is not True
        or verification["final_bill_ceiling_proven"] is not False
        or verification["actual_input_ceiling_proven"] is not False
        or verification["risks"] != ADVISORY_RISKS
    ):
        raise ValueError("local_advisory_budget_unverifiable")
    if type(value["pricing"]) is not dict or set(value["pricing"]) != (
        {"flash"} if flash else {"flash", "pro"}
    ):
        raise ValueError("local_advisory_budget_unverifiable")
    for group, terms in value["pricing"].items():
        validate_terms(terms, group, advisory=True)
        if (
            terms["source_price_ref"] != value["provider_evidence"]["sha256"]
            or terms["evidence"] != value["provider_evidence"]["sha256"]
        ):
            raise ValueError("local_advisory_price_source_mismatch")
    measurement = exact(
        value["input_measurement"], "method numerator denominator overhead"
    )
    if measurement["method"] != "complete_wire_utf8_planning_estimate":
        raise ValueError("local_advisory_input_estimator_unverifiable")
    numerator = integer(measurement["numerator"], minimum=1)
    denominator = integer(measurement["denominator"], minimum=1)
    integer(measurement["overhead"])
    if numerator < denominator:
        raise ValueError("local_advisory_input_estimator_unverifiable")
    text(value["approval_ref"])
    return value


def final_charge_path(directory, row):
    name = "final-charge-" + digest({"attempt_id": row["attempt_id"]}) + ".json"
    return directory.path / name


def read_final_charge(
    directory, config, row, response, quote, original_billing, *, now
):
    """Optional fresh provider receipt for one original Attempt, never a resend."""
    path = final_charge_path(directory, row)
    if not path.exists() and not path.is_symlink():
        return None
    value = directory.json(path)
    advisory = original_billing.get("contract") in (ADVISORY_BILLING, FLASH_BILLING)
    exact(
        value,
        "contract version installation_id candidate_id attempt_id "
        "payload_sha256 response_sha256 quote_sha256 account_scope "
        + (
            "source_currency charged_cny_units normalized_usd_units "
            "fx_numerator fx_denominator "
            if advisory
            else "actual_units "
        )
        + "provider_origin provider_record observed_at valid_until approval_ref",
    )
    if (
        value["contract"]
        != ("V2-LOCAL-ADVISORY-FINAL-CHARGE" if advisory else "V1-LOCAL-FINAL-CHARGE")
        or type(value["version"]) is not int
        or value["version"] != (2 if advisory else 1)
        or any(value[key] != config[key] for key in ("installation_id", "candidate_id"))
        or value["attempt_id"] != row["attempt_id"]
        or value["payload_sha256"] != row["payload_sha256"]
        or value["response_sha256"] != digest(response)
        or value["quote_sha256"] != digest(quote)
        or value["account_scope"] != original_billing["account_scope"]
        or value["provider_origin"] != original_billing["provider_origin"]
        or not instant(value["observed_at"]) <= now < instant(value["valid_until"])
    ):
        raise ValueError("local_final_charge_unverifiable")
    if advisory:
        if value["source_currency"] != "CNY":
            raise ValueError("local_final_charge_unverifiable")
        source = integer(value["charged_cny_units"])
        normalized = integer(value["normalized_usd_units"])
        numerator = integer(value["fx_numerator"], minimum=1)
        denominator = integer(value["fx_denominator"], minimum=1)
        terms = quote["terms"]
        if (
            numerator != terms["fx_numerator"]
            or denominator != terms["fx_denominator"]
            or normalized != (source * numerator + denominator - 1) // denominator
        ):
            raise ValueError("local_final_charge_conversion_mismatch")
    else:
        integer(value["actual_units"])
    reference = exact(value["provider_record"], "path sha256")
    if (
        hashlib.sha256(directory.read(reference["path"])).hexdigest()
        != reference["sha256"]
    ):
        raise ValueError("local_final_charge_evidence_changed")
    text(value["approval_ref"])
    return value


def inspect_local_installation(config_path):
    """Read-only diagnostics. A successful inspection still grants no execution."""
    from datetime import UTC, datetime

    result = {
        "contract": CONTRACT,
        "version": 1,
        "trust_profile": PROFILE,
        "online_executable": False,
        "blockers": [],
        "checks": {},
    }
    try:
        directory, config, _, _, _ = verify_installation(config_path)
    except (OSError, ValueError, KeyError, ImportError) as error:
        result["blockers"].append(str(error))
        return result
    result["installation_id"] = config["installation_id"]
    result["candidate_id"] = config["candidate_id"]
    if sys.platform != "darwin":
        result["blockers"].append("local_macos_installation_required")
    from .contract import ADVISORY_BUDGET, FLASH_POLICY

    model_policy = None
    try:
        declared = directory.json(config["readiness_path"])
        if (
            type(declared) is dict
            and declared.get("contract") == FLASH_READINESS
            and declared.get("version") == 3
        ):
            model_policy = FLASH_POLICY
        policy = (
            ADVISORY_BUDGET
            if model_policy == FLASH_POLICY
            or (
                type(declared) is dict
                and declared.get("contract") == ADVISORY_READINESS
                and declared.get("version") == 2
            )
            else None
        )
    except (OSError, ValueError, KeyError) as error:
        result["blockers"].append("readiness:" + str(error))
        policy = None
    result["budget_policy"] = policy or "final_liability_hard_cap"
    if model_policy is not None:
        result["model_policy"] = model_policy
    for key, reader in (
        (
            "readiness",
            lambda: read_readiness(
                directory,
                config,
                now=datetime.now(UTC),
                budget_policy=policy,
                model_policy=model_policy,
            ),
        ),
        (
            "billing",
            lambda: (
                read_advisory_budget(
                    directory, config, now=datetime.now(UTC), model_policy=model_policy
                )
                if policy == ADVISORY_BUDGET
                else read_billing(directory, config, now=datetime.now(UTC))
            ),
        ),
    ):
        try:
            value = reader()
            result["checks"][key] = {
                "document_sha256": digest(value),
                "owner_review": "NOT_READ",
            }
        except (OSError, ValueError, KeyError) as error:
            result["blockers"].append(key + ":" + str(error))
    result["blockers"].append("live_owner_decisions_and_run_admission_required")
    return deepcopy(result)
