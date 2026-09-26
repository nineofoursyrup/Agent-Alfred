"""Closed acceptance setup declarations, never executable or arbitrary paths."""

from dataclasses import asdict
from datetime import datetime
from uuid import uuid4

from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.model import ScriptedModel

from .schema import encode, identifier


def validate_setup(case, profile):
    setup = case.get("setup")
    if not isinstance(setup, dict):
        raise ValueError("invalid_setup")
    if set(setup) - {
        "version",
        "memory",
        "delete_memory",
        "skills",
        "calendar",
        "session_seed",
        "aggregate",
        "routing",
        "local_tool_allowlist",
        "limits",
        "fault_fixture",
        "persona",
    }:
        raise ValueError("unknown_setup_field")
    if type(setup.get("version")) is not int or setup["version"] != 1:
        raise ValueError("unknown_setup_version")
    if type(setup.get("routing", False)) is not bool:
        raise ValueError("invalid_setup_routing")
    if len(encode(setup)) > 262144:
        raise ValueError("setup_size_limit")
    allowed = profile.get("local_tool_allowlist")
    mask = setup.get("local_tool_allowlist")
    if not isinstance(mask, list) or not isinstance(allowed, list):
        raise ValueError("case_tool_mask_required")
    if any(type(x) is not str for x in mask) or len(set(mask)) != len(mask):
        raise ValueError("invalid_case_tool_mask")
    if set(mask) - set(allowed):
        raise ValueError("case_tool_mask_exceeds_profile")
    memories = sequence(setup, "memory", 32)
    refs = set()
    for item in memories:
        fields(item, {"ref", "kind", "payload"})
        ref = identifier(item["ref"])
        if ref in refs:
            raise ValueError("duplicate_setup_memory_ref")
        refs.add(ref)
        if item["kind"] not in ("semantic", "episodic"):
            raise ValueError("invalid_setup_memory_kind")
        keys = (
            {"subject", "fact"}
            if item["kind"] == "semantic"
            else {"summary", "occurred_at", "occurred_until"}
        )
        fields(item["payload"], keys)
        for key, value in item["payload"].items():
            if key == "occurred_until" and value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError("invalid_setup_memory_payload")
            if key in ("occurred_at", "occurred_until"):
                date = datetime.fromisoformat(value)
                if date.utcoffset() is None:
                    raise ValueError("invalid_setup_memory_time")
        payload = item["payload"]
        if payload.get("occurred_until") is not None and (
            datetime.fromisoformat(payload["occurred_until"])
            < datetime.fromisoformat(payload["occurred_at"])
        ):
            raise ValueError("invalid_setup_memory_time")
    deleted = sequence(setup, "delete_memory", 32)
    if any(type(x) is not str for x in deleted) or len(set(deleted)) != len(deleted):
        raise ValueError("invalid_setup_delete")
    if set(deleted) - refs:
        raise ValueError("unknown_setup_memory_ref")
    from zoneinfo import ZoneInfo

    from agent_alfred.tools.calendar import CalendarTools, parse_instant
    from agent_alfred.tools.schema import validate_input

    calendar_schema = CalendarTools(None, None).declarations()[0].input_schema
    for event in sequence(setup, "calendar", 64):
        validate_input(calendar_schema, event)
        start = parse_instant(event["starts_at"])
        if "ends_at" in event and parse_instant(event["ends_at"]) < start:
            raise ValueError("invalid_setup_calendar_time")
        if "iana_time_zone" in event:
            ZoneInfo(event["iana_time_zone"])
    for seed in sequence(setup, "session_seed", 8):
        fields(seed, {"input", "output"})
        for value in seed.values():
            if not isinstance(value, str) or not value.strip() or len(value) > 8192:
                raise ValueError("invalid_session_seed")
        if seed["input"].lstrip().startswith("/"):
            raise ValueError("session_seed_control_forbidden")
    if case["operation"] == "aggregate":
        from agent_alfred.aggregation import AggregationRequest

        value = setup.get("aggregate")
        fields(value, {"keywords", "sources"})
        AggregationRequest(
            "validation", case["input"], value["keywords"], value["sources"]
        )
    elif "aggregate" in setup:
        raise ValueError("aggregate_setup_on_chat")
    from agent_alfred.skills.catalog import NAME

    names = set()
    for skill in sequence(setup, "skills", 16):
        fields(skill, {"name", "description", "body"})
        if (
            not isinstance(skill["name"], str)
            or not NAME.fullmatch(skill["name"])
            or skill["name"] in names
        ):
            raise ValueError("invalid_setup_skill_name")
        names.add(skill["name"])
        for key in ("description", "body"):
            if (
                not isinstance(skill[key], str)
                or not skill[key].strip()
                or len(skill[key]) > 16000
            ):
                raise ValueError("invalid_setup_skill_content")
    if "persona" in setup and (
        not isinstance(setup["persona"], str)
        or not setup["persona"].strip()
        or len(setup["persona"]) > 16000
    ):
        raise ValueError("invalid_setup_persona")
    effective_settings(setup, profile)
    fixture = setup.get("fault_fixture")
    if fixture is not None:
        if fixture not in (
            "prepared_context_projection_error_v1",
            "persona_version_conflict_v1",
            "file_publication_unknown_v1",
        ):
            raise ValueError("unknown_fault_fixture")
        if fixture == "prepared_context_projection_error_v1" and (
            setup.get("routing") is not True or case["operation"] != "chat"
        ):
            raise ValueError("projection_fixture_requires_routing_chat")


def fields(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_setup_fields")


def sequence(setup, key, limit):
    value = setup.get(key, [])
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("invalid_setup_collection")
    return value


def json_record(record):
    def value(item):
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, dict):
            return {k: value(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [value(v) for v in item]
        return item

    return value(asdict(record))


def prepare_memory(host, setup):
    """Save/delete via the shared command service; never retain deleted bodies."""
    rows = []
    context = CommandContext(ManualOrigin("web"), "web")
    for item in setup.get("memory", []):
        command = {
            "operation_id": uuid4().hex,
            "action": "save",
            "kind": item["kind"],
            "payload": item["payload"],
        }
        receipt = host.memory_service.execute(command, context)
        if receipt.get("status") != "saved":
            raise ValueError("setup_memory_save_unverified")
        row = {
            "ref": item["ref"],
            "kind": item["kind"],
            "save_receipt": receipt,
            "memory_id": receipt["memory_id"],
        }
        if item["ref"] in setup.get("delete_memory", []):
            receipt = host.memory_service.execute(
                {
                    "operation_id": uuid4().hex,
                    "action": "delete",
                    "kind": item["kind"],
                    "payload": {"id": row["memory_id"]},
                    "expected_version": receipt["record_version"],
                },
                context,
            )
            if receipt.get("status") != "deleted":
                raise ValueError("setup_memory_delete_unverified")
            row["delete_receipt"] = receipt
        rows.append(row)
    return rows


def verify_memory(host, setup, rows):
    for item, row in zip(setup.get("memory", []), rows, strict=True):
        record = host.memory_service.get(row["kind"], row["memory_id"])
        row["present"] = record is not None
        if "delete_receipt" in row:
            operation_id = row["delete_receipt"]["operation_id"]
            forgetting = host.memory_service.forgetting.get_forgetting(operation_id)
            if (
                record is not None
                or forgetting is None
                or forgetting["state"] != "complete"
            ):
                raise ValueError("setup_forgetting_incomplete")
            row["forgetting"] = forgetting
        else:
            if record is None:
                raise ValueError("setup_memory_readback_mismatch")
            actual = json_record(record)
            for key, expected in item["payload"].items():
                observed = actual[key]
                if key in ("occurred_at", "occurred_until") and expected is not None:
                    equal = datetime.fromisoformat(observed) == datetime.fromisoformat(
                        expected
                    )
                else:
                    equal = observed == expected
                if not equal:
                    raise ValueError("setup_memory_readback_mismatch")
            if record.record_version != row["save_receipt"]["record_version"]:
                raise ValueError("setup_memory_version_mismatch")
            row["record"] = actual
    return rows


def prepare_calendar(state, setup):
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.tools import ToolContext, ToolRegistry

    from .artifacts import local_services

    receipts = []
    with local_services(state) as (calendar, _, clock):
        registry = ToolRegistry(calendar.declarations(), clock=clock)
        for event in setup.get("calendar", []):
            call = ToolCallBlock(uuid4().hex, "create_event", event)
            context = ToolContext(
                "setup-" + uuid4().hex,
                None,
                call.id,
                "acceptance_fixture",
                float("inf"),
            )
            result = registry.execute(call, context)
            if result.block.is_error:
                raise ValueError("setup_calendar_unverified")
            receipts.append(
                {
                    "call_id": call.id,
                    "source": "simulation",
                    "receipt": "".join(x.text for x in result.block.content),
                }
            )
    return receipts


def verify_calendar(setup, business):
    from agent_alfred.tools.calendar import parse_instant

    if business["status"] != "available":
        raise ValueError("setup_business_readback_unavailable")
    actual = business["calendar"]["entries"]
    expected = setup.get("calendar", [])
    if len(actual) != len(expected):
        raise ValueError("setup_calendar_readback_mismatch")
    # IDs are assigned by the real command; creation order maps declarations.
    for wanted, row in zip(
        expected, sorted(actual, key=lambda x: x["id"]), strict=True
    ):
        for key, value in wanted.items():
            observed = row[key]
            same = (
                parse_instant(value) == parse_instant(observed)
                if key in ("starts_at", "ends_at")
                else value == observed
            )
            if not same:
                raise ValueError("setup_calendar_readback_mismatch")


class SimulatedModel(ScriptedModel):
    """An explicit offline transport with events matching its actual responses."""

    def respond(self, request, *, events=None, deadline=None):
        from agent_alfred.events import AttemptAborted, AttemptCommitted

        result = super().respond(request, deadline=deadline)
        if events is not None:
            for attempt in result.attempts:
                if attempt.outcome == "committed" and result.response is not None:
                    events.emit(
                        AttemptCommitted(
                            attempt_id=attempt.attempt_id,
                            blocks=result.response.blocks,
                            stop_reason=result.response.stop_reason,
                            usage=attempt.usage,
                        )
                    )
                elif attempt.error is not None:
                    events.emit(
                        AttemptAborted(
                            attempt_id=attempt.attempt_id,
                            error=attempt.error,
                            usage=attempt.usage,
                        )
                    )
        return result


def seed_model(setup):
    from .examples import SKIP

    return SimulatedModel(
        [
            item
            for seed in setup.get("session_seed", [])
            for item in (SKIP, seed["output"])
        ]
    )


def prepare_session(host, setup):
    from agent_alfred.messages import message_plain_text
    from agent_alfred.runtime.host import SubmitRequest

    session = host.create_session()
    rows = []
    for seed in setup.get("session_seed", []):
        accepted = host.submit(SubmitRequest(seed["input"], session_id=session))
        if accepted.kind != "accepted":
            raise ValueError("session_seed_admission_failed")
        settled = host.wait(accepted.run_id)
        if (
            settled.outcome != "completed"
            or message_plain_text(settled.reply) != seed["output"]
        ):
            raise ValueError("session_seed_unverified")
        rows.append(
            {
                "run_id": accepted.run_id,
                "source": "simulation",
                "outcome": settled.outcome,
            }
        )
    return session, rows


def verify_session(host, session, setup, rows):
    from agent_alfred.messages import message_plain_text

    page = host.open_session(session, page_size=20)
    if page.next_cursor is not None or len(page.messages) != len(rows) * 2:
        raise ValueError("session_seed_readback_mismatch")
    for seed, row in zip(setup.get("session_seed", []), rows, strict=True):
        saved = [m for m in page.messages if m.run_id == row["run_id"]]
        if len(saved) != 2 or [(m.role, message_plain_text(m)) for m in saved] != [
            ("user", seed["input"]),
            ("assistant", seed["output"]),
        ]:
            raise ValueError("session_seed_readback_mismatch")
        row["recorded"] = True
    return rows


LIMITS = frozenset(
    {
        "max_steps",
        "input_character_limit",
        "gate_input_character_limit",
        "working_memory_rounds",
        "per_store_limit",
        "per_store_character_budget",
    }
)


def effective_settings(setup, profile):
    from dataclasses import replace

    from agent_alfred.settings import Settings

    base = Settings(**profile["parameters"], persona=profile["inputs"]["persona"])
    limits = setup.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - LIMITS:
        raise ValueError("invalid_setup_limit")
    for name, value in limits.items():
        if type(value) is not int or value < 1:
            raise ValueError("invalid_setup_limit")
        ceiling = getattr(base, name)
        if name == "gate_input_character_limit" and ceiling is None:
            ceiling = base.input_character_limit
        if value > ceiling:
            raise ValueError("case_limit_exceeds_profile")
    return replace(base, **limits)


def install_skills(builtin, setup):
    import json

    for skill in setup.get("skills", []):
        directory = builtin / skill["name"]
        directory.mkdir()
        text = (
            "---\nname: "
            + json.dumps(skill["name"])
            + "\ndescription: "
            + json.dumps(skill["description"])
            + "\n---\n"
            + skill["body"]
        )
        (directory / "SKILL.md").write_text(text, encoding="utf-8")


def verify_skills(host, setup):
    from .schema import digest

    catalog = host.skill_catalog
    expected = {s["name"]: s for s in setup.get("skills", [])}
    actual = {s.name: s for s in catalog.list()}
    if set(actual) != set(expected):
        raise ValueError("setup_skill_catalog_mismatch")
    rows = []
    for name, skill in expected.items():
        body = catalog.load(name)
        if body != skill["body"] or actual[name].description != skill["description"]:
            raise ValueError("setup_skill_readback_mismatch")
        rows.append(
            {
                "name": name,
                "description": actual[name].description,
                "body": body,
                "body_sha256": digest(body),
            }
        )
    return rows


def build_case_host(
    *,
    state_dir,
    settings,
    skill_builtin,
    local_tool_allowlist,
    factory,
    credentials,
    fault_fixture=None,
    observations=None,
    _rollback=None,
):
    from agent_alfred.wiring import build_default_host, build_host

    if fault_fixture in (
        None, "persona_version_conflict_v1", "file_publication_unknown_v1"
    ):
        from .fault_fixtures import persona_conflict, publication_unknown

        return build_default_host(
            persona_read_observer=(
                persona_conflict(observations)
                if fault_fixture == "persona_version_conflict_v1"
                else None
            ),
            file_publication_checkpoint=(
                publication_unknown(observations)
                if fault_fixture == "file_publication_unknown_v1"
                else None
            ),
            _rollback=_rollback,
            state_dir=state_dir,
            settings=settings,
            skill_builtin=skill_builtin,
            local_tool_allowlist=local_tool_allowlist,
            factory=factory,
            credentials=credentials,
        )
    if fault_fixture != "prepared_context_projection_error_v1":
        raise ValueError("unknown_fault_fixture")
    from pathlib import PurePath

    from agent_alfred.database import open_database
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.resource_rollback import ConstructionOwner
    from agent_alfred.runtime.model_settings import ModelSettingsStore
    from agent_alfred.runtime.routing import build_routing_graph

    def fail_projection(snapshot):
        from datetime import UTC, datetime

        observations.append(
            {
                "boundary": "prepared_context_projection",
                "observed_at": datetime.now(UTC).isoformat(),
                "source": "simulation",
            }
        )
        raise ValueError("acceptance_projection_fault")

    def graph(tools):
        return build_routing_graph(tools, projection=fail_projection)

    owner = ConstructionOwner(_rollback)
    try:
        directory = ManagedStateDirectory.acquire(state_dir, _rollback=owner.rollback)
        owner.rollback.own(directory)
        conn = open_database(directory, _rollback=owner.rollback)
        owner.rollback.own(conn)
        models = ModelSettingsStore(state_dir / "model_settings.json")
        models.load()
        host = build_host(
            conn=conn,
            settings=settings,
            skill_builtin=skill_builtin,
            file_state=directory,
            audit_key_path=state_dir / "audit.key",
            trace_root=(directory, PurePath("traces")),
            model_settings=models,
            factory=factory,
            credentials=credentials,
            local_tool_allowlist=local_tool_allowlist,
            routing_graph_builder=graph,
            _rollback=owner.rollback,
        )
        owner.rollback.own(host)
        host.attach_owned_resources(conn, directory, source=owner.rollback)
        owner.publish(host)
        return host
    except BaseException as error:
        owner.fail(error)


def prepare_persona(state, settings, setup):
    import json

    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.tools import ToolContext, ToolRegistry
    from agent_alfred.tools.persona import PersonaTools

    from .artifacts import local_services

    if "persona" not in setup:
        return None
    with local_services(state) as (_, files, clock):
        persona = PersonaTools(files, settings)
        registry = ToolRegistry(persona.declarations(), clock=clock)
        context = ToolContext(
            "setup-" + uuid4().hex,
            None,
            uuid4().hex,
            "acceptance_fixture",
            float("inf"),
        )
        current = persona.read({}, context)
        observed = json.loads("".join(x.text for x in current.content))
        result = registry.execute(
            ToolCallBlock(
                context.call_id,
                "update_persona",
                {
                    "content": setup["persona"],
                    "expected_version": observed["version"],
                },
            ),
            context,
        )
        if result.block.is_error:
            raise ValueError("setup_persona_unverified")
        return {
            "source": "simulation",
            "operation_id": result.operation_id,
            "receipt": "".join(x.text for x in result.block.content),
        }


def verify_runtime_setup(host, setup):
    tools = host.tools_catalog()["tools"]
    if {row["name"] for row in tools} != set(setup["local_tool_allowlist"]) or any(
        row["effect"] == "external" or row["exposure"] != "real" for row in tools
    ):
        raise ValueError("setup_tool_mask_readback_mismatch")
    behaviour = host.behaviour()
    if behaviour["status"] != "ok" or behaviour["enabled"] != setup.get(
        "routing", False
    ):
        raise ValueError("setup_routing_readback_mismatch")
    return {"capabilities": tools, "behaviour": behaviour}
