"""The HTTP contract of the Dashboard API, without a socket.

The interesting assertions here are the ones about *when* each status is
reachable: 202 only after the lease, the committed accepted row and the
handoff have all happened; 409 while the lease is still held, including while
the recording is pending; 503 only once ``recording_failed`` has landed. A
failure to persist or to hand off is never allowed to look like an accepted
Run -- a 202 for a Run nobody is running is worse than an error, because it
is a promise the client will wait on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent_alfred.gateway.web.api import (
    STAGE_SAVING,
    DashboardApi,
    MutationGate,
    busy_summary_from,
    safe_navigation,
)
from agent_alfred.runtime.sessions import (
    SessionInboxPage,
    SessionMessagesPage,
    SessionNotFound,
    SessionSummary,
)
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.runtime.work import SubmitRequest, SubmitResult

INSTANCE = "proc-api"


def _snapshot(
    *,
    coordinator_state: str = "idle",
    active_run: ActiveRunSummary | None = None,
    projection: UnrecordedTerminalProjection | None = None,
    revision: int = 0,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        process_instance_id=INSTANCE,
        state_revision=revision,
        coordinator_state=coordinator_state,  # type: ignore[arg-type]
        active_run=active_run,
        unrecorded_terminal_projection=projection,
    )


def _active(**kwargs: Any) -> ActiveRunSummary:
    base: dict[str, Any] = {
        "run_id": "r1",
        "purpose": "chat",
        "gateway": "web",
        "phase": "running",
        "session_id": "s1",
        "prompt_preview": "hello",
        "started_at": "2026-08-27T12:00:00Z",
        "recording_state": None,
        "current_step": 2,
    }
    base.update(kwargs)
    return ActiveRunSummary(**base)


@dataclass
class _Facade:
    """Records what it was asked and answers with whatever the test set."""

    result: SubmitResult
    state: RuntimeSnapshot
    known_sessions: frozenset[str] = frozenset({"s1"})
    created: list[str] | None = None
    requests: list[SubmitRequest] | None = None
    mutation_refusal: str | None = None

    def __post_init__(self) -> None:
        self.created = []
        self.requests = []
        self.run_queries: list[tuple[str, int, str | None]] = []
        # (session_id, limit, cursor) of every MainBar read, so a test can
        # see which Session the read was aimed at.
        self.mainbar_queries: list[tuple[str, int, str | None]] = []
        # The same record for the Session group's run list.
        self.session_run_queries: list[tuple[str, int, str | None]] = []
        # The gate's authority, in the shape the Host provides it. This
        # facade stands in for a Host with no Run in flight, so the slot is
        # the only thing that can busy the gate -- which is what lets a test
        # hold it and prove a second write is refused instead of queued.
        self.mutating = False
        self.begun = 0
        self.ended = 0

    def try_begin_mutation(self) -> str | None:
        if self.mutation_refusal is not None:
            return self.mutation_refusal
        if self.mutating:
            return "mutation_in_flight"
        self.mutating = True
        self.begun += 1
        return None

    def end_mutation(self) -> None:
        self.mutating = False
        self.ended += 1

    def mutation_in_flight(self) -> bool:
        return self.mutating

    def create_session(self) -> str:
        session_id = "new-session"
        self.created.append(session_id)
        return session_id

    def submit(self, request: SubmitRequest) -> SubmitResult:
        assert self.requests is not None
        self.requests.append(request)
        return self.result

    def session_exists(self, session_id: str) -> bool:
        return session_id in self.known_sessions

    def snapshot(self) -> RuntimeSnapshot:
        return self.state

    def list_sessions(self, *, limit: int, cursor: str | None = None):
        return SessionInboxPage(
            sessions=(
                SessionSummary("s1", "2026-08-27T12:00:00Z", 3, "title"),
            ),
            next_cursor=None,
        )

    def open_session(
        self, session_id: str, *, page_size: int, cursor: str | None = None
    ):
        if session_id not in self.known_sessions:
            raise SessionNotFound(session_id)
        return SessionMessagesPage(
            session_id=session_id,
            title="title",
            messages=(),
            next_cursor=None,
        )

    def list_runs(self, *, filter: str, limit: int, cursor: str | None = None):
        assert self.run_queries is not None
        self.run_queries.append((filter, limit, cursor))
        return _page(filter)

    def locate_run(self, run_id: str, *, limit: int):
        return None if run_id == "missing" else _page("chat")

    def mainbar_pairs(self, *, session_id: str, limit: int, cursor: str | None):
        if session_id not in self.known_sessions:
            raise SessionNotFound(session_id)
        self.mainbar_queries.append((session_id, limit, cursor))
        return _pairs()

    def list_session_chat_runs(
        self, *, session_id: str, limit: int, cursor: str | None
    ):
        if session_id not in self.known_sessions:
            raise SessionNotFound(session_id)
        self.session_run_queries.append((session_id, limit, cursor))
        return _session_runs_page(session_id)


def _page(filter_name: str):
    from agent_alfred.runtime.runs import RunPage, RunSummary

    return RunPage(
        filter=filter_name,
        runs=(
            RunSummary(
                run_id="r1",
                purpose="chat",
                filter=filter_name,
                purpose_known=True,
                session_id="s1",
                gateway="web",
                entry_surface_id=None,
                prompt_preview="hi",
                phase="finished",
                outcome="completed",
                accepted_at="2026-08-27T12:00:00Z",
                started_at="2026-08-27T12:00:00Z",
                finished_at="2026-08-27T12:00:01Z",
                activity_revision=7,
            ),
        ),
        non_terminal=None,
        next_cursor=None,
    )


def _pairs():
    from agent_alfred.messages import text_message
    from agent_alfred.runtime.runs import MainBarPage, MainBarPair

    return MainBarPage(
        pairs=(
            MainBarPair(
                run_id="r1",
                activity_revision=7,
                session_id="s1",
                created_at="2026-08-27T12:00:00Z",
                user_message=text_message("user", "hello"),
                assistant_message=text_message("assistant", "hi there"),
            ),
        ),
        next_cursor=None,
    )


def _session_runs_page(session_id: str):
    from agent_alfred.runtime.runs import SessionChatRun, SessionChatRunsPage

    return SessionChatRunsPage(
        session_id=session_id,
        runs=(
            SessionChatRun(
                run_id="r1",
                phase="finished",
                outcome="completed",
                accepted_at="2026-08-27T12:00:00Z",
                started_at="2026-08-27T12:00:01Z",
                finished_at="2026-08-27T12:00:02Z",
                activity_revision=7,
                reply_preview="hi there",
                reply_source="web",
            ),
            SessionChatRun(
                run_id="r2",
                phase="running",
                outcome=None,
                accepted_at="2026-08-27T12:01:00Z",
                started_at="2026-08-27T12:01:01Z",
                finished_at=None,
                activity_revision=8,
                reply_preview=None,
                reply_source=None,
            ),
        ),
        next_cursor=None,
    )


def _api(result: SubmitResult, snapshot: RuntimeSnapshot | None = None):
    return DashboardApi(
        facade=_Facade(
            result=result,
            state=snapshot if snapshot is not None else _snapshot(),
        )
    )


def _accepted() -> SubmitResult:
    return SubmitResult(kind="accepted", run_id="r1", session_id="s1")


def _facade() -> _Facade:
    """The facade alone, for read tests that need to see what was asked."""
    return _Facade(result=_accepted(), state=_snapshot())


# --- the 202 ----------------------------------------------------------------


def test_an_accepted_run_answers_202_with_its_run_id() -> None:
    api = _api(_accepted())
    outcome = api.submit({"message": "hello", "session_id": "s1"})
    assert outcome.status == 202
    assert outcome.run_id == "r1"
    assert outcome.session_id == "s1"
    assert outcome.code is None


def test_202_comes_only_from_the_coordinators_accepted_kind() -> None:
    """The mapping is the contract.

    ``accepted`` is set by admission only after the lease was reserved, the
    accepted row committed and the work item handed to the single execution
    thread, so "202" is a statement about all three -- not just about the
    request having been understood.
    """
    for kind in ("run_in_progress", "recording_unavailable", "admission_failed"):
        outcome = _api(SubmitResult(kind=kind)).submit(  # type: ignore[arg-type]
            {"message": "hello", "session_id": "s1"}
        )
        assert outcome.status != 202, f"{kind} must never answer 202"


def test_a_failed_persist_or_handoff_never_looks_accepted() -> None:
    # A 202 here would tell the browser to wait for a Run nobody is running.
    outcome = _api(SubmitResult(kind="admission_failed")).submit(
        {"message": "hi", "session_id": "s1"}
    )
    assert outcome.status == 500
    assert outcome.code == "admission_failed"
    assert outcome.run_id is None


# --- the 409 ----------------------------------------------------------------


def test_a_second_run_while_one_is_in_flight_is_409() -> None:
    snapshot = _snapshot(
        coordinator_state="running", active_run=_active(phase="running")
    )
    outcome = _api(
        SubmitResult(kind="run_in_progress", snapshot=snapshot)
    ).submit({"message": "hi", "session_id": "s1"})
    assert outcome.status == 409
    assert outcome.code == "run_in_progress"
    assert outcome.busy is not None
    assert outcome.busy.run_id == "r1"


def test_recording_pending_is_409_and_says_it_is_saving() -> None:
    """The #30 补正一 closure.

    A Run whose reply exists but whose recording has not settled still holds
    the only admission lease, so the second submit gets the existing 409 --
    not a new status code -- and the card says what it is waiting for.
    """
    snapshot = _snapshot(
        coordinator_state="recording_pending",
        active_run=_active(phase="finished", outcome="completed"),
        revision=5,
    )
    outcome = _api(
        SubmitResult(kind="run_in_progress", snapshot=snapshot)
    ).submit({"message": "hi", "session_id": "s1"})
    assert outcome.status == 409
    assert outcome.code == "run_in_progress"
    assert outcome.busy is not None
    assert outcome.busy.stage == STAGE_SAVING


def test_the_saving_stage_is_not_claimed_while_the_run_is_merely_running() -> None:
    snapshot = _snapshot(coordinator_state="running", active_run=_active())
    assert busy_summary_from(snapshot) is not None
    assert busy_summary_from(snapshot).stage == "运行中"


# --- the 503 ----------------------------------------------------------------


def test_recording_unavailable_answers_503() -> None:
    snapshot = _snapshot(
        coordinator_state="recording_failed",
        active_run=_active(phase="finished", recording_state="failed"),
        revision=9,
    )
    outcome = _api(
        SubmitResult(kind="recording_unavailable", snapshot=snapshot)
    ).submit({"message": "hi", "session_id": "s1"})
    assert outcome.status == 503
    assert outcome.code == "recording_unavailable"


def test_503_is_only_ever_the_failed_states_answer() -> None:
    """The state lands before the answer does.

    ``recording_unavailable`` is produced by the coordinator only once it has
    reached ``recording_failed``, so a client can never be told recording is
    unavailable while it is still merely pending.
    """
    pending = _snapshot(
        coordinator_state="recording_pending", active_run=_active(phase="finished")
    )
    assert (
        _api(SubmitResult(kind="run_in_progress", snapshot=pending))
        .submit({"message": "hi", "session_id": "s1"})
        .status
        == 409
    )


# --- the shared busy card ---------------------------------------------------


def test_known_busy_and_raced_409_render_one_and_the_same_card() -> None:
    """One shape, one renderer.

    Two cards that differ in a field would make "busy" two states that look
    identical on screen, and the client would have to guess which one it got.
    """
    snapshot = _snapshot(coordinator_state="running", active_run=_active())
    from_race = _api(
        SubmitResult(kind="run_in_progress", snapshot=snapshot)
    ).submit({"message": "hi", "session_id": "s1"})
    from_prevention = busy_summary_from(snapshot)
    assert from_race.busy is not None
    assert from_race.busy.to_json() == from_prevention.to_json()


def test_the_busy_card_carries_only_the_whitelisted_fields() -> None:
    snapshot = _snapshot(coordinator_state="running", active_run=_active())
    card = busy_summary_from(snapshot)
    assert card is not None
    # Exactly the whitelist: purpose, Gateway, start time, the nullable
    # current Step, an optional preview, the stage, and one navigation
    # target. The run id and the shelf are parts of that target, not two
    # more fields the client has to know about.
    assert set(card.to_json()) == {
        "purpose",
        "gateway",
        "started_at",
        "current_step",
        "prompt_preview",
        "stage",
        "navigation",
    }
    assert set(card.to_json()["navigation"]) == {"href", "run_id", "filter"}


def test_there_is_no_busy_card_when_nothing_is_running() -> None:
    assert busy_summary_from(_snapshot()) is None


def test_the_navigation_target_is_always_a_same_origin_path() -> None:
    assert safe_navigation("r1", "chat") == "/runs/r1?filter=chat"
    # An id carrying path or host syntax is escaped, and an unknown filter
    # falls back rather than being passed through.
    assert safe_navigation("../evil", "chat") == "/runs/..%2Fevil?filter=chat"
    assert safe_navigation("r1", "javascript:alert(1)") == "/runs/r1?filter=all"
    for value in ("r1", "a/b", "x?y=1#z"):
        assert safe_navigation(value, "all").startswith("/runs/")
        assert not safe_navigation(value, "all").startswith("//")


# --- request validation -----------------------------------------------------


@pytest.mark.parametrize("message", ["", "   ", None, 42, ["hi"]])
def test_a_message_that_is_not_text_is_refused(message: Any) -> None:
    outcome = _api(_accepted()).submit({"message": message})
    assert outcome.status == 400
    assert outcome.code == "empty_message"


def test_an_unknown_purpose_is_refused_rather_than_escaped_in() -> None:
    outcome = _api(_accepted()).submit({"message": "hi", "purpose": "nonsense"})
    assert outcome.status == 400
    assert outcome.code == "unknown_purpose"


def test_a_system_purpose_is_accepted_and_travels_through() -> None:
    api = _api(_accepted())
    assert api.submit({"message": "hi", "purpose": "inference_probe"}).status == 202
    facade = api._facade
    assert facade.requests is not None
    assert facade.requests[0].purpose == "inference_probe"


def test_an_unknown_session_is_refused_before_admission_is_asked() -> None:
    api = _api(_accepted())
    outcome = api.submit({"message": "hi", "session_id": "nope"})
    assert outcome.status == 404
    assert outcome.code == "unknown_session"
    facade = api._facade
    assert facade.requests == []


def test_the_submit_is_addressed_to_the_web_gateway() -> None:
    api = _api(_accepted())
    api.submit({"message": "hi", "session_id": "s1"})
    facade = api._facade
    assert facade.requests is not None
    assert facade.requests[0].gateway == "web"
    assert facade.requests[0].entry_surface_id == "mainbar"


# --- the explicit Session contract (#28) -------------------------------------


def test_a_chat_without_a_session_is_refused_before_admission() -> None:
    """A Web chat names its Session or it does not happen (#28).

    The default purpose is ``chat``, and a chat with no ``session_id`` would
    otherwise be quietly handed one at admission -- a Session created behind
    the caller's back, exactly what "no global active Session" forbids. The
    refusal is decided at this boundary, before the gate: no run id is
    minted, no config is captured, no Session is created and no work item is
    published, because none of that lives on this side of the facade.
    """
    api = _api(_accepted())
    outcome = api.submit({"message": "hello"})
    assert outcome.status == 400
    assert outcome.code == "missing_session_id"
    assert outcome.run_id is None
    facade = api._facade
    # Admission was never asked: no SubmitRequest crossed the gate, and no
    # Session was created on the request's behalf.
    assert facade.requests == []
    assert facade.created == []


def test_a_null_session_id_is_missing_not_present() -> None:
    """A JSON ``null`` is not a Session: refusing it is the same decision."""
    api = _api(_accepted())
    outcome = api.submit({"message": "hello", "session_id": None})
    assert outcome.status == 400
    assert outcome.code == "missing_session_id"
    assert outcome.run_id is None
    facade = api._facade
    assert facade.requests == []
    assert facade.created == []


def test_an_empty_session_id_is_a_value_not_an_absence() -> None:
    """The missing test is ``is None``, never truthiness.

    A historic Session id may be the empty string, so a chat addressed to
    "" reaches admission addressed to exactly that id instead of being
    misread as having named no Session at all.
    """
    facade = _Facade(
        result=SubmitResult(kind="accepted", run_id="r1", session_id=""),
        state=_snapshot(),
        known_sessions=frozenset({"", "s1"}),
    )
    api = DashboardApi(facade=facade)
    outcome = api.submit({"message": "hello", "session_id": ""})
    assert outcome.status == 202
    assert outcome.run_id == "r1"
    assert outcome.session_id == ""
    assert facade.requests is not None
    assert facade.requests[0].session_id == ""


def test_a_non_string_session_id_is_not_reported_as_missing() -> None:
    """A wrong-typed id and an absent id are different client bugs, with
    different codes: one is a bad value, the other is no value at all."""
    api = _api(_accepted())
    outcome = api.submit({"message": "hi", "session_id": 42})
    assert outcome.status == 400
    assert outcome.code == "bad_session_id"
    assert outcome.code != "missing_session_id"
    assert api._facade.requests == []


def test_an_existing_session_still_submits_and_answers_202() -> None:
    """The rule adds a requirement, not a refusal: a chat that names one of
    the Sessions that exists keeps the whole accepted contract."""
    api = _api(_accepted())
    outcome = api.submit({"message": "hello", "session_id": "s1"})
    assert outcome.status == 202
    assert outcome.run_id == "r1"
    assert outcome.session_id == "s1"
    assert api._facade.requests is not None
    assert len(api._facade.requests) == 1


def test_a_system_run_still_needs_no_session() -> None:
    """The explicit-Session rule is a Web chat rule. A system purpose keeps
    its successful contract, carrying no Session to admission."""
    api = _api(_accepted())
    outcome = api.submit({"message": "hi", "purpose": "inference_probe"})
    assert outcome.status == 202
    facade = api._facade
    assert facade.requests is not None
    assert facade.requests[0].purpose == "inference_probe"
    assert facade.requests[0].session_id is None


# --- sessions ---------------------------------------------------------------


def test_the_server_signs_the_session_id_and_the_client_cannot_pick_one() -> None:
    import inspect

    api = _api(_accepted())
    result = api.create_session()
    assert result.session_id
    # Not a knob: a caller-supplied id would let two Sessions collide into
    # one, so the signature takes nothing at all.
    assert not inspect.signature(DashboardApi.create_session).parameters.keys() - {
        "self"
    }
    assert not inspect.signature(
        DashboardApi.session_inbox
    ).parameters.keys() - {"self", "params"}


# --- reads ------------------------------------------------------------------


def test_the_inbox_payload_is_flat_and_carries_the_cursor() -> None:
    status, payload = _api(_accepted()).session_inbox({})
    assert status == 200
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["next_cursor"] is None


def test_an_unknown_session_reads_404() -> None:
    status, payload = _api(_accepted()).session_messages("nope", {})
    assert status == 404
    assert payload["code"] == "unknown_session"


@pytest.mark.parametrize("filter_name", ["chat", "system", "all"])
def test_every_closed_filter_is_accepted(filter_name: str) -> None:
    status, payload = _api(_accepted()).runs_page({"filter": filter_name})
    assert status == 200
    assert payload["filter"] == filter_name


def test_an_unknown_filter_is_refused_not_guessed() -> None:
    status, payload = _api(_accepted()).runs_page({"filter": "nonsense"})
    assert status == 400
    assert payload["code"] == "unknown_filter"


def test_the_runs_payload_keeps_the_live_run_out_of_the_page() -> None:
    _status, payload = _api(_accepted()).runs_page({"filter": "chat"})
    assert payload["non_terminal"] is None
    assert [run["run_id"] for run in payload["runs"]] == ["r1"]


def test_a_deep_link_locates_a_run_and_a_missing_one_404s() -> None:
    api = _api(_accepted())
    assert api.locate_run("r1", {})[0] == 200
    assert api.locate_run("missing", {}) == (404, {"code": "unknown_run"})


@pytest.mark.parametrize("raw,expected", [("0", 25), ("-3", 25), ("abc", 25)])
def test_an_unusable_page_size_falls_back_to_the_default(
    raw: str, expected: int
) -> None:
    api = _api(_accepted())
    api.runs_page({"limit": raw})
    facade = api._facade
    assert facade.run_queries[0][1] == expected


@pytest.mark.parametrize("raw,expected", [("99999", 100), ("7", 7), ("1", 1)])
def test_the_page_size_is_clamped_not_trusted(raw: str, expected: int) -> None:
    # An unclamped limit is a denial of service behind one query parameter.
    api = _api(_accepted())
    api.runs_page({"limit": raw})
    facade = api._facade
    assert facade.run_queries[0][1] == expected


def test_the_mainbar_requires_a_session_id() -> None:
    """The MainBar answers one Session; without the Session there is no
    question to answer, and the request is refused before any read runs."""
    facade = _facade()
    status, payload = DashboardApi(facade=facade).mainbar({})
    assert status == 400
    assert payload == {"code": "missing_session_id"}
    assert facade.mainbar_queries == []


def test_the_mainbar_answers_an_unknown_session_with_404() -> None:
    status, payload = DashboardApi(facade=_facade()).mainbar(
        {"session_id": "nope"}
    )
    assert status == 404
    assert payload == {"code": "unknown_session"}


def test_the_mainbar_targets_the_requested_session() -> None:
    facade = _facade()
    _status, payload = DashboardApi(facade=facade).mainbar({"session_id": "s1"})
    assert facade.mainbar_queries[0][0] == "s1"
    pair = payload["pairs"][0]
    assert pair["run_id"] == "r1"
    assert pair["user"][0]["text"] == "hello"
    assert pair["assistant"][0]["text"] == "hi there"


# --- the Session group's run list -------------------------------------------


def test_the_session_run_list_requires_a_session_id() -> None:
    facade = _facade()
    status, payload = DashboardApi(facade=facade).session_runs({})
    assert status == 400
    assert payload == {"code": "missing_session_id"}
    assert facade.session_run_queries == []


def test_the_session_run_list_answers_an_unknown_session_with_404() -> None:
    status, payload = DashboardApi(facade=_facade()).session_runs(
        {"session_id": "nope"}
    )
    assert status == 404
    assert payload == {"code": "unknown_session"}


def test_the_session_run_list_targets_one_session_and_shows_its_runs() -> None:
    """One row per admitted Run, the running one among them, and no row
    invents a reply the database has not written."""
    facade = _facade()
    _status, payload = DashboardApi(facade=facade).session_runs(
        {"session_id": "s1", "limit": "1", "cursor": "some-cursor"}
    )
    assert facade.session_run_queries == [("s1", 1, "some-cursor")]
    assert [run["run_id"] for run in payload["runs"]] == ["r1", "r2"]
    assert [run["phase"] for run in payload["runs"]] == ["finished", "running"]
    assert payload["runs"][0]["reply_preview"] == "hi there"
    assert payload["runs"][0]["reply_source"] == "web"
    # The running Run's row carries its state, and nothing it has not got.
    assert payload["runs"][1]["reply_preview"] is None
    assert payload["runs"][1]["reply_source"] is None
    assert payload["runs"][1]["finished_at"] is None


# --- block rendering --------------------------------------------------------


def test_only_text_is_rendered_and_other_blocks_are_counted() -> None:
    from agent_alfred.gateway.web.api import _block_json
    from agent_alfred.messages import (
        TextBlock,
        ThinkingBlock,
        ToolCallBlock,
        ToolResultBlock,
    )

    assert _block_json(TextBlock("hi")) == {"type": "text", "text": "hi"}
    assert _block_json(ThinkingBlock("secret reasoning")) == {"type": "thinking"}
    assert _block_json(ToolCallBlock("c1", "t", {})) == {"type": "tool_call"}
    result = _block_json(ToolResultBlock("c1", (TextBlock("out"),)))
    assert result == {"type": "tool_result", "count": 1}


# --- the write gate --------------------------------------------------------


def test_every_write_goes_through_one_gate() -> None:
    """#23 §4: locking chat alone is not serialisation.

    The queue is the serial thing, so both writes pass the same door -- and
    a future endpoint that changes memory or settings has one place to go.
    """
    api = _api(_accepted())
    assert isinstance(api._gate, MutationGate)
    assert api._gate is api._gate


def test_the_gate_never_queues_a_write() -> None:
    """Refused, not held -- and refused for as long as the lease is held.

    A gate that blocked until the current write finished would be the queue
    ADR-0016 forbids, wearing a different hat. The proof here is stronger
    than "no deadlock": a write attempted *after* the Run has taken the lease
    is refused too, which is what makes the lease a lease rather than a
    critical section around one function call.
    """
    api = _api(_accepted())
    gate = api._gate

    class Reentrant:
        """A facade that holds the lease the way a Run does, once accepted."""

        def __init__(self, inner):
            self._inner = inner
            self.observed = None
            self.leased = False

        def submit(self, request):
            result = self._inner.submit(request)
            # The Run now holds the admission lease: from here until its
            # recording settles, no other write may enter.
            self.leased = True
            self.observed = gate.create_session()
            return result

        def try_begin_mutation(self) -> str | None:
            if self.leased:
                return "run_in_progress"
            return self._inner.try_begin_mutation()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    reentrant = Reentrant(api._facade)
    gate._facade = reentrant
    outcome = api.submit({"message": "hi", "session_id": "s1"})
    # The second write was refused rather than waited for, and told why.
    session_id, reason = reentrant.observed
    assert session_id is None
    assert reason == "run_in_progress"
    # And the outer one still completed: a refusal is not a failure cascade.
    assert outcome.status == 202


@pytest.mark.parametrize(
    ("reason", "status"),
    (
        ("mutation_in_flight", 409),
        ("run_in_progress", 409),
        ("recording_unavailable", 503),
    ),
)
def test_a_refused_session_creation_preserves_the_authoritative_reason(
    reason: str, status: int
) -> None:
    api = _api(_accepted())
    api._facade.mutation_refusal = reason
    result = api.create_session()
    assert result.session_id is None
    assert result.status == status
    assert result.code == reason
    assert api._facade.created == []


def test_a_refused_submit_is_never_an_accepted_run() -> None:
    api = _api(_accepted())
    api._facade.mutating = True  # another write is inside the gate
    outcome = api.submit({"message": "hi", "session_id": "s1"})
    # A conflict, not an unavailability: 409, and never a run id.
    assert outcome.status == 409
    assert outcome.code == "mutation_in_flight"
    assert outcome.run_id is None
