"""Issue 46 Memory page adapters over the real guarded Dashboard HTTP boundary."""

import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic.test_memory_http import post, read
from agent_alfred.evals.deterministic.test_web_http import _request
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_dashboard

CONTEXT = CommandContext(ManualOrigin("web"), "web")


@pytest.fixture
def dashboard(tmp_path):
    runtime = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel([])),
        port=free_loopback_port(),
    )
    runtime.start()
    try:
        yield runtime
    finally:
        assert runtime.close()


def query(path, **params):
    return path + "?" + urlencode(params)


def command(dashboard, operation_id, action, payload, **extra):
    return post(
        dashboard,
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "kind": extra.pop("kind", "semantic"),
            "action": action,
            "payload": payload,
            **extra,
        },
        "/api/memory/commands",
    )


def test_save_receipt_replay_and_current_record_reads(dashboard):
    status, empty = read(dashboard, query("/api/memory/records", kind="semantic"))
    assert status == 200
    assert empty["records"] == [] and empty["next_cursor"] is None
    fact = {"subject": "food", "fact": "Zephyr likes coriander"}
    status, saved = command(dashboard, "save-1", "save", fact)
    assert status == 200
    receipt = saved["result"]
    assert receipt["status"] == "saved" and receipt["record_version"] == 1
    assert saved["forgetting"] is None
    assert command(dashboard, "save-1", "save", fact) == (200, saved)
    status, mismatch = command(
        dashboard, "save-1", "save", {**fact, "fact": "different"}
    )
    assert (status, mismatch) == (409, {"error": {"code": "operation_mismatch"}})
    status, current = read(
        dashboard,
        query("/api/memory/record", kind="semantic", id=receipt["memory_id"]),
    )
    assert status == 200
    record = current["record"]
    assert record["id"] == receipt["memory_id"]
    assert (record["subject"], record["fact"]) == ("food", "Zephyr likes coriander")
    assert record["origin"] == {"type": "manual", "source": "web"}
    assert record["last_change_origin"] == record["origin"]
    assert record["record_version"] == 1 and record["human_protected"] is True
    assert record["created_at"] == record["modified_at"]
    assert record["provenance"] == {"state": "known_none", "source_groups": []}
    state = read(dashboard, "/api/memory/state")[1]
    assert state["memory_revision"] == current["memory_revision"]
    assert state["process_instance_id"] == dashboard.host.process_instance_id
    listed = read(dashboard, query("/api/memory/records", kind="semantic"))[1]
    assert [item["id"] for item in listed["records"]] == [receipt["memory_id"]]
    assert "provenance" not in listed["records"][0]
    assert listed["read_revision"] == state["memory_revision"]
    found = read(
        dashboard,
        query("/api/memory/records", kind="semantic", mode="search", text="Zephyr"),
    )[1]
    assert [item["id"] for item in found["records"]] == [receipt["memory_id"]]
    assert "relevance" not in json.dumps(found) and "bm25" not in json.dumps(found)
    assert (
        read(
            dashboard,
            query(
                "/api/memory/records", kind="semantic", mode="search", text="unrelated"
            ),
        )[1]["records"]
        == []
    )


def test_episode_times_require_offsets_and_half_open_filters(dashboard):
    episode = {
        "summary": "Planned the trip",
        "occurred_at": "2026-09-01T23:30:00+08:00",
        "occurred_until": None,
    }
    status, saved = command(dashboard, "episode", "save", episode, kind="episodic")
    assert status == 200
    status, current = read(
        dashboard,
        query(
            "/api/memory/record", kind="episodic", id=saved["result"]["memory_id"]
        ),
    )
    assert current["record"]["occurred_at"] == "2026-09-01T15:30:00+00:00"
    assert current["record"]["occurred_until"] is None

    def search(**bounds):
        return read(
            dashboard,
            query("/api/memory/records", kind="episodic", mode="search", **bounds),
        )

    assert [r["summary"] for r in search(since="2026-09-01T15:30:00Z")[1]["records"]]
    assert search(until="2026-09-01T23:30:00+08:00")[1]["records"] == []
    assert search(since="2026-09-01T23:30:00")[1] == {
        "error": {"code": "invalid_input", "field": "since"}
    }
    assert search(since="2026-09-02T00:00:00Z", until="2026-09-01T00:00:00Z")[0] == 400
    naive = {**episode, "occurred_at": "2026-09-01T23:30:00"}
    assert command(dashboard, "naive", "save", naive, kind="episodic")[0] == 400
    backwards = {**episode, "occurred_until": "2026-09-01T00:00:00+08:00"}
    assert command(dashboard, "backwards", "save", backwards, kind="episodic")[0] == 400


def test_read_validation_uses_error_codes_without_echoing_input(dashboard):
    cases = {
        query("/api/memory/records", kind="other"): (400, "invalid_input", "kind"),
        query("/api/memory/records", kind="semantic", unknown="x"): (
            400,
            "invalid_input",
            None,
        ),
        query("/api/memory/statistics", unknown="<x>"): (400, "invalid_input", None),
        query("/api/memory/records", kind="semantic", page_size="0"): (
            400,
            "invalid_input",
            "page_size",
        ),
        query("/api/memory/records", kind="semantic", mode="list", text="x"): (
            400,
            "invalid_input",
            None,
        ),
        query("/api/memory/record", kind="semantic"): (400, "invalid_input", None),
        query("/api/memory/statistics", since="2026-09-01T00:00:00"): (
            400,
            "invalid_input",
            "since",
        ),
    }
    for path, (status, code, field) in cases.items():
        response = read(dashboard, path)
        expected = {"code": code, **({} if field is None else {"field": field})}
        assert response == (status, {"error": expected}), path
    status, missing = read(
        dashboard, query("/api/memory/record", kind="semantic", id="missing")
    )
    assert status == 404 and missing["error"]["code"] == "not_found"
    assert read(dashboard, query("/api/memory/operations", operation_id="x")) == (
        404,
        {"error": {"code": "not_found"}},
    )
    fact = {"subject": "food", "fact": "PRIVATE"}
    for extra in ({"origin": {"type": "tool"}}, {"human_protected": True}):
        status, refused = command(dashboard, "forged", "save", fact, **extra)
        assert (status, refused) == (400, {"error": {"code": "invalid_input"}})
    assert command(dashboard, "approve", "approve", fact)[0] == 400
    assert read(dashboard, query("/api/memory/operations", operation_id="forged"))[
        0
    ] == 404


def test_conflicts_keep_versions_and_delete_answers_without_body(dashboard):
    first = command(dashboard, "a", "save", {"subject": "food", "fact": "PRIVATE-A"})
    second = command(dashboard, "b", "save", {"subject": "food", "fact": "other B"})
    a, b = first[1]["result"], second[1]["result"]
    status, updated = command(
        dashboard,
        "edit",
        "update",
        {"id": a["memory_id"], "fact": "PRIVATE-A2"},
        expected_version=1,
    )
    assert status == 200 and updated["result"]["status"] == "updated"
    assert updated["result"]["record_version"] == 2
    status, stale = command(
        dashboard,
        "stale",
        "update",
        {"id": a["memory_id"], "fact": "late"},
        expected_version=1,
    )
    assert (status, stale) == (
        409,
        {"error": {"code": "version_conflict", "current_version": 2}},
    )
    status, duplicate = command(
        dashboard,
        "duplicate",
        "update",
        {"id": b["memory_id"], "fact": "PRIVATE-A2"},
        expected_version=1,
    )
    assert (status, duplicate) == (
        409,
        {"error": {"code": "duplicate_conflict", "existing_id": a["memory_id"]}},
    )
    status, stale_delete = command(
        dashboard, "delete-stale", "delete", {"id": a["memory_id"]}, expected_version=1
    )
    assert status == 409 and stale_delete["error"]["current_version"] == 2
    status, deleted = command(
        dashboard, "delete", "delete", {"id": a["memory_id"]}, expected_version=2
    )
    assert status == 200 and deleted["result"]["status"] == "deleted"
    assert deleted["forgetting"]["operation_id"] == "delete"
    assert deleted["forgetting"]["state"] in {"cleaning", "complete"}
    assert "PRIVATE" not in json.dumps(deleted) and "food" not in json.dumps(deleted)
    status, operation = read(
        dashboard, query("/api/memory/operations", operation_id="delete")
    )
    assert status == 200 and operation["result"] == deleted["result"]
    assert operation["forgetting"]["memory_id"] == a["memory_id"]
    assert isinstance(operation["scopes"], list)
    assert "PRIVATE" not in json.dumps(operation)
    old = read(dashboard, query("/api/memory/operations", operation_id="a"))[1]
    assert old["result"]["status"] == "saved" and old["forgetting"] is None
    gone = query("/api/memory/record", kind="semantic", id=a["memory_id"])
    assert read(dashboard, gone)[0] == 404
    assert (
        read(
            dashboard,
            query(
                "/api/memory/records", kind="semantic", mode="search", text="PRIVATE-A2"
            ),
        )[1]["records"]
        == []
    )
    status, absent = command(
        dashboard, "again", "delete", {"id": a["memory_id"]}, expected_version=2
    )
    assert status == 200 and absent["result"]["status"] == "already_absent"
    assert absent["forgetting"] is None


def test_manual_writes_are_refused_not_queued_while_the_gate_is_held(dashboard):
    host = dashboard.host
    fact = {"subject": "food", "fact": "held"}
    assert host.try_begin_mutation() is None
    try:
        assert command(dashboard, "held", "save", fact) == (
            409,
            {"error": {"code": "busy"}},
        )
        status, busy = post(
            dashboard,
            {"schema_version": 1, "action": "retry_cleanup", "operation_id": "x"},
            "/api/memory/forget/actions",
        )
        assert (status, busy["error"]["code"]) == (409, "busy")
    finally:
        host.end_mutation()
    assert read(dashboard, query("/api/memory/operations", operation_id="held"))[
        0
    ] == 404
    status, saved = command(dashboard, "held", "save", fact)
    assert status == 200 and saved["result"]["status"] == "saved"


def test_scope_confirmation_and_cleanup_retry_use_server_snapshots(dashboard):
    forgetting = dashboard.host.memory_service.forgetting
    for group, session in (("U1", "S1"), ("U2", "S2")):
        forgetting.register_group(
            group, kind="run", container_id=session, context=CONTEXT
        )
    saved = command(dashboard, "save", "save", {"subject": "s", "fact": "PRIVATE"})[1]
    status, deleted = command(
        dashboard,
        "delete",
        "delete",
        {"id": saved["result"]["memory_id"]},
        expected_version=1,
    )
    assert status == 200 and deleted["forgetting"]["state"] == "needs_scope"
    operation = read(dashboard, query("/api/memory/operations", operation_id="delete"))
    scopes = [scope for scope in operation[1]["scopes"] if scope["state"] == "pending"]
    assert {member for scope in scopes for member in scope["members"]} == {"U1", "U2"}

    def resolve(action_id, chosen, **extra):
        return post(
            dashboard,
            {
                "schema_version": 1,
                "action": "resolve_scope",
                "operation_id": "delete",
                "action_id": action_id,
                "scopes": chosen,
                **extra,
            },
            "/api/memory/forget/actions",
        )

    stale = [{"scope_id": scopes[0]["scope_id"], "expected_revision": 99}]
    status, refused = resolve("stale", stale)
    assert status == 409 and refused["error"]["code"] == "scope_stale"
    assert resolve("member", stale, members=["U3"])[0] == 400
    chosen = [
        {"scope_id": scope["scope_id"], "expected_revision": scope["revision"]}
        for scope in scopes
    ]
    status, confirmed = resolve("confirm", chosen)
    assert status == 200 and confirmed["result"]["status"] == "confirmed"
    assert confirmed["forgetting"]["state"] in {"cleaning", "complete"}
    status, replay = resolve("confirm", chosen)
    assert status == 200 and replay["result"] == confirmed["result"]
    status, mismatch = resolve("confirm", chosen[:1])
    assert (status, mismatch["error"]["code"]) == (409, "operation_mismatch")
    status, retried = post(
        dashboard,
        {"schema_version": 1, "action": "retry_cleanup", "operation_id": "delete"},
        "/api/memory/forget/actions",
    )
    assert status == 200 and retried["forgetting"]["state"] == "complete"
    assert retried["result"] is None
    status, unknown = post(
        dashboard,
        {"schema_version": 1, "action": "retry_cleanup", "operation_id": "none"},
        "/api/memory/forget/actions",
    )
    assert (status, unknown["error"]["code"]) == (404, "not_found")
    assert post(
        dashboard,
        {"schema_version": 1, "action": "purge", "operation_id": "delete"},
        "/api/memory/forget/actions",
    )[0] == 400


def test_list_and_search_cursors_bind_query_revision_and_internal_order(tmp_path):
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel([])),
        clock=FakeClock(),
        port=free_loopback_port(),
    )
    dashboard.start()
    try:
        ids = [
            command(
                dashboard, f"s{index}", "save", {"subject": "same", "fact": f"k{index}"}
            )[1]["result"]["memory_id"]
            for index in range(5)
        ]
        created = {
            read(dashboard, query("/api/memory/record", kind="semantic", id=id))[1][
                "record"
            ]["created_at"]
            for id in ids
        }
        assert len(created) == 1

        def page(**params):
            return read(
                dashboard, query("/api/memory/records", kind="semantic", **params)
            )

        first = page(page_size=2)[1]
        second = page(page_size=2, cursor=first["next_cursor"])[1]
        third = page(page_size=2, cursor=second["next_cursor"])[1]
        order = [r["id"] for p in (first, second, third) for r in p["records"]]
        assert order == ids[::-1] and third["next_cursor"] is None
        assert page(page_size=3, cursor=first["next_cursor"])[1] == {
            "error": {"code": "cursor_stale"}
        }
        assert page(mode="search", text="same", cursor=first["next_cursor"])[0] == 409
        searched = page(mode="search", subject="same", page_size=2)[1]
        assert page(mode="search", subject="other", cursor=searched["next_cursor"])[
            0
        ] == 409
        command(dashboard, "late", "save", {"subject": "same", "fact": "late"})
        assert page(page_size=2, cursor=first["next_cursor"])[0] == 409
        assert page(mode="search", subject="same", cursor=searched["next_cursor"])[
            1
        ] == {"error": {"code": "cursor_stale"}}
    finally:
        assert dashboard.close()


def test_statistics_report_the_anchor_and_null_ratios(dashboard):
    status, body = read(dashboard, "/api/memory/statistics")
    assert status == 200
    statistics = body["statistics"]
    assert statistics["session_id"] is None
    assert statistics["counts"] == {"skip": 0, "hit": 0, "miss": 0, "error": 0}
    assert statistics["hit"] == {"numerator": 0, "denominator": 0, "ratio": None}
    status, scoped = read(
        dashboard,
        query(
            "/api/memory/statistics",
            since="2026-09-01T00:00:00+08:00",
            until="2026-09-08T00:00:00+08:00",
            session_id="s",
        ),
    )
    assert status == 200
    assert scoped["statistics"]["since"] == "2026-08-31T16:00:00+00:00"
    assert scoped["statistics"]["session_id"] == "s"
    assert read(
        dashboard,
        query(
            "/api/memory/statistics",
            since="2026-09-08T00:00:00Z",
            until="2026-09-01T00:00:00Z",
        ),
    )[0] == 400


def write_skill(root, directory, name, body):
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {directory} {name}\n---\n{body}")


def test_skill_catalog_reads_the_startup_index_only(tmp_path):
    builtin = tmp_path / "builtin"
    state = tmp_path / "state"
    write_skill(builtin, "plan", "plan", "builtin plan body")
    write_skill(builtin, "shop", "shop", "builtin shop body")
    write_skill(state / "skills", "mine", "plan", "user plan body\n  kept verbatim")
    dashboard = build_dashboard(
        state_dir=state,
        skill_builtin=builtin,
        factory=ScriptedModelFactory(ScriptedModel([])),
        port=free_loopback_port(),
    )
    dashboard.start()
    try:
        status, listed = read(dashboard, "/api/memory/skills")
        assert status == 200
        skills = {skill["name"]: skill for skill in listed["skills"]}
        assert skills["plan"] == {
            "name": "plan",
            "description": "mine plan",
            "source": "user",
            "overrides_builtin": True,
        }
        assert skills["shop"]["source"] == "builtin"
        assert not skills["shop"]["overrides_builtin"]
        status, loaded = read(dashboard, query("/api/memory/skill", name="plan"))
        assert status == 200
        assert loaded["skill"]["body"] == "user plan body\n  kept verbatim"
        write_skill(state / "skills", "mine", "plan", "changed after startup")
        assert (
            read(dashboard, query("/api/memory/skill", name="plan"))[1]["skill"]["body"]
            == "user plan body\n  kept verbatim"
        )
        for name in ("../builtin/shop", "mine", "missing"):
            assert read(dashboard, query("/api/memory/skill", name=name)) == (
                404,
                {"error": {"code": "not_found"}},
            )
        assert read(dashboard, "/api/memory/skill")[0] == 400
    finally:
        assert dashboard.close()


def test_duplicate_skill_names_fail_startup_instead_of_listing_one(tmp_path):
    state = tmp_path / "state"
    write_skill(state / "skills", "one", "same", "first")
    write_skill(state / "skills", "two", "same", "second")
    dashboard = build_dashboard(
        state_dir=state,
        skill_builtin=tmp_path / "builtin",
        factory=ScriptedModelFactory(ScriptedModel([])),
        port=free_loopback_port(),
    )
    with pytest.raises(ValueError, match="duplicate_skill_name"):
        dashboard.start()


def test_invalid_skill_name_fails_startup(tmp_path):
    state = tmp_path / "state"
    write_skill(state / "skills", "bad", "not/a/name", "body")
    dashboard = build_dashboard(
        state_dir=state,
        skill_builtin=tmp_path / "builtin",
        factory=ScriptedModelFactory(ScriptedModel([])),
        port=free_loopback_port(),
    )
    with pytest.raises(ValueError, match="invalid_skill_name"):
        dashboard.start()


def test_missing_catalog_is_unavailable_not_an_empty_directory():
    import sqlite3

    from agent_alfred import schema
    from agent_alfred.gateway.web.memory_api import serve_memory_read
    from agent_alfred.wiring import build_host

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    # A Host without a state directory has no Skill directory to index.
    host = build_host(conn=conn, factory=ScriptedModelFactory(ScriptedModel([])))
    try:
        assert host.skill_catalog is None
        for path, params in (
            ("/api/memory/skills", {}),
            ("/api/memory/skill", {"name": "plan"}),
        ):
            assert serve_memory_read(host, path, params) == (
                503,
                {"error": {"code": "unavailable"}},
            )
    finally:
        assert host.close()


def test_mirror_confirmation_token_survives_a_browser_number_round_trip(
    dashboard, tmp_path
):
    command(dashboard, "save", "save", {"subject": "food", "fact": "coriander"})
    (tmp_path / "state/memory/facts.md").write_text("External edit")
    mirror = read(dashboard, "/api/memory/mirrors?name=facts")[1]["mirrors"][0]
    assert mirror["conflict"]
    assert json.loads(mirror["confirmation_token"]) == mirror["confirmation"]

    def numbers(value):
        if isinstance(value, dict):
            return [n for item in value.values() for n in numbers(item)]
        if isinstance(value, list):
            return [n for item in value for n in numbers(item)]
        return [value] if type(value) is int else []

    # A file identity holds integers beyond 2**53, which JavaScript rounds.
    assert any(abs(n) > 2**53 for n in numbers(mirror["confirmation"]))
    browser = json.loads(
        mirror["confirmation_token"], parse_int=lambda text: int(float(text))
    )
    action = {
        "schema_version": 1,
        "action": "mirror_confirm",
        "name": "facts",
        "operation_id": "confirm",
    }
    if browser != mirror["confirmation"]:
        status, stale = post(dashboard, {**action, "observation": browser})
        assert status == 409 and stale["result"]["error"]["code"] == (
            "stale_confirmation"
        )
    assert post(dashboard, {**action, "observation": "{not json"})[0] == 400
    token = mirror["confirmation_token"]
    status, receipt = post(
        dashboard, {**action, "operation_id": "token", "observation": token}
    )
    assert status == 200 and receipt["result"]["status"] == "regenerated"


def test_record_read_refuses_a_revision_that_moved_between_its_two_reads():
    from agent_alfred.gateway.web.memory_api import serve_memory_read

    class Memory:
        # A write lands between the record read and its provenance read.
        revisions = iter((5, 6))
        memory_revision = property(lambda self: next(self.revisions))

        def get(self, kind, id):
            return SimpleNamespace(record_version=1)

        def get_provenance(self, kind, id, version):
            return {"state": "known_none", "source_groups": []}

    host = SimpleNamespace(memory_service=Memory(), process_instance_id="p")
    assert serve_memory_read(
        host, "/api/memory/record", {"kind": "semantic", "id": "m"}
    ) == (409, {"error": {"code": "memory_changed"}})


def test_storage_faults_and_other_refusals_keep_closed_codes(dashboard):
    import sqlite3

    from agent_alfred.gateway.web.memory_api import serve_memory_write

    saved = command(dashboard, "save", "save", {"subject": "s", "fact": "PRIVATE"})
    record = query(
        "/api/memory/record", kind="semantic", id=saved[1]["result"]["memory_id"]
    )
    conn = dashboard.host._conn
    conn.set_authorizer(
        lambda operation, table, *_: (
            sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_READ and table == "facts"
            else sqlite3.SQLITE_OK
        )
    )
    try:
        assert read(dashboard, record) == (
            503,
            {"error": {"code": "storage_read_failed"}},
        )
    finally:
        conn.set_authorizer(None)
    assert read(dashboard, record)[0] == 200
    for body in ({"action": []}, {"action": {"x": 1}}):
        status, refused = post(dashboard, body, "/api/memory/forget/actions")
        assert (status, refused["error"]["code"]) == (400, "invalid_input")
    raw = (
        "POST /api/memory/commands HTTP/1.1\r\n"
        f"Host: localhost:{dashboard.port}\r\n"
        f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
        "Content-Type: application/json\r\nContent-Length: 3\r\n"
        "Connection: close\r\n\r\n[1]"
    ).encode()
    head, body = _request(dashboard.port, raw)
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body) == {"error": {"code": "body_not_object"}}

    class Barrier:
        def execute(self, command, context):
            return {"error": {"code": "trace_barrier_failed"}}

    host = SimpleNamespace(memory_service=Barrier(), process_instance_id="p")
    assert serve_memory_write(host, "/api/memory/commands", {}) == (
        503,
        {"error": {"code": "unavailable", "reason": "trace_barrier_failed"}},
    )


def test_real_stream_memory_patch_has_no_seq_checkpoint_run_or_body(dashboard):
    from agent_alfred.evals.deterministic.test_web_http import _connect

    sock = _connect(dashboard.port)
    try:
        sock.sendall(
            (
                "GET /api/events HTTP/1.1\r\n"
                f"Host: localhost:{dashboard.port}\r\n\r\n"
            ).encode()
        )
        sock.settimeout(5)
        buffer = b""

        def frames():
            nonlocal buffer
            while True:
                while b"\n\n" in buffer:
                    frame, _, buffer = buffer.partition(b"\n\n")
                    yield frame
                buffer += sock.recv(65536)

        stream = frames()
        before = next(f for f in stream if b"event: memory_patch" in f)
        command(dashboard, "manual", "save", {"subject": "s", "fact": "PRIVATE"})
        frame = next(f for f in stream if b"event: memory_patch" in f)
    finally:
        sock.close()
    lines = frame.decode().splitlines()
    assert not any(line.startswith("id:") for line in lines)
    body = json.loads(next(line for line in lines if line.startswith("data:"))[5:])
    assert set(body) == {
        "schema_version",
        "process_instance_id",
        "memory_revision",
        "change",
    }
    assert body["change"] is None and "PRIVATE" not in frame.decode()
    assert body["memory_revision"] > json.loads(
        before.decode().split("data:")[1]
    )["memory_revision"]
    assert read(dashboard, "/api/runs?filter=all&limit=25")[1]["runs"] == []
