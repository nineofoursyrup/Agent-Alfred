"""The Dashboard's HTTP API: what a request asks for and what it is told.

Everything here is transport-shaped logic over an injected facade, with no
sockets and no ``http.server``: the handler is a thin IO shroud around these
functions, so the contract -- 202 only after the lease, accepted transaction
and handoff have all happened; 409 while the lease is held, including while
the recording is pending; 503 after a committed handoff fails or once
``recording_failed`` has landed; a
busy card that reads the same whether it came from a race or from a known-busy
client -- is testable without a browser.

Nothing here infers "recorded" from anything but the database. A trace that
flushed, an event that persisted, a cursor that advanced and an entry in the
replay ring are all things that can happen in a process whose transaction
never committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, Protocol
from urllib.parse import quote

from agent_alfred.runtime import runs
from agent_alfred.runtime.cursor import MalformedCursor
from agent_alfred.runtime.recording import RecordingUnavailable
from agent_alfred.runtime.sessions import (
    SessionInboxPage,
    SessionMessagesPage,
    SessionNotFound,
)
from agent_alfred.runtime.snapshot import RuntimeSnapshot
from agent_alfred.runtime.work import (
    AdmissionObservationKind,
    SubmitRequest,
    SubmitResult,
)
from agent_alfred.schema import PURPOSES

# The one stage label the decision names explicitly (#30 补正一): a Run whose
# reply exists but whose recording has not settled is still holding the only
# admission lease, and the honest words for that are "saving".
STAGE_SAVING = "正在保存"

# What the coordinator state means to a person reading the busy card. Only
# ``recording_pending`` is prescribed by the decision; the rest are the plain
# reading of the phase, because a card that said nothing would invite the
# reader to conclude the Run had died.
_STAGE_LABELS = {
    "accepted": "已接受",
    "running": "运行中",
    "recording_pending": STAGE_SAVING,
    "recording_failed": "保存失败",
    "idle": "空闲",
}

# The runs page's closed filter set and the page-size bounds are owned by
# the read side that implements them; a second copy here would be the kind
# of duplicate that drifts the moment one side gets a fourth filter.
RUN_FILTERS = runs.RUN_FILTERS
DEFAULT_PAGE_SIZE = min(runs.DEFAULT_RUN_PAGE_SIZE, runs.DEFAULT_MAINBAR_LIMIT)
MAX_PAGE_SIZE = 100


def _map_read_errors(method):
    """Give every HTTP database read the same secret-free failure shapes."""

    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except MalformedCursor:
            return 400, {"code": "malformed_cursor"}
        except RecordingUnavailable:
            return 503, {"code": "recording_unavailable"}

    return guarded

__all__ = [
    "BusySummary",
    "CreateSessionResult",
    "DashboardApi",
    "DashboardFacade",
    "MutationGate",
    "STAGE_SAVING",
    "SubmitOutcome",
    "busy_summary_from",
    "safe_navigation",
]


class DashboardFacade(Protocol):
    """The narrow slice of the Host the API is allowed to touch."""

    def create_session(self) -> str: ...

    def submit(self, request: SubmitRequest) -> SubmitResult: ...

    # The mutation gate needs the Host's own judgement, not a lock it could
    # hold for the length of a function call. Delegated through the facade
    # so a test can drive the gate without a Host.
    def try_begin_mutation(self) -> str | None: ...

    def end_mutation(self) -> None: ...

    def mutation_in_flight(self) -> bool: ...

    def admission_observe(
        self,
    ) -> tuple[AdmissionObservationKind, RuntimeSnapshot]: ...

    def session_exists(self, session_id: str) -> bool: ...

    def snapshot(self) -> RuntimeSnapshot: ...

    def list_sessions(self, *, limit: int, cursor: str | None) -> SessionInboxPage: ...

    def open_session(
        self, session_id: str, *, page_size: int, cursor: str | None
    ) -> SessionMessagesPage: ...

    def list_runs(
        self, *, filter: str, limit: int, cursor: str | None
    ) -> Any: ...

    def locate_run(self, run_id: str, *, limit: int) -> Any | None: ...

    def list_session_chat_runs(
        self, *, session_id: str, limit: int, cursor: str | None
    ) -> Any: ...

    def mainbar_pairs(
        self, *, session_id: str, limit: int, cursor: str | None
    ) -> Any: ...


@dataclass(frozen=True)
class SubmitOutcome:
    """The HTTP answer to a submit. Exactly one of the four decided shapes."""

    status: int
    run_id: str | None = None
    session_id: str | None = None
    code: str | None = None
    busy: "BusySummary | None" = None

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if self.run_id is not None:
            body["run_id"] = self.run_id
        if self.session_id is not None:
            body["session_id"] = self.session_id
        if self.code is not None:
            body["code"] = self.code
        if self.busy is not None:
            body["busy"] = self.busy.to_json()
        return body


@dataclass(frozen=True)
class BusySummary:
    """The one busy card, rendered from one shape whether the client asked
    before it sent (prevention) or found out afterwards (409).

    The field set is a whitelist, not a projection of the Run row: purpose,
    Gateway, start time, the nullable current Step, an optional redacted and
    length-limited prompt preview, and a same-origin navigation target. A
    second copy of this card with different fields is how "busy" would become
    two different states that look the same on screen.
    """

    purpose: str
    gateway: str
    started_at: str | None
    current_step: int | None
    prompt_preview: str | None
    stage: str
    run_id: str
    filter: str

    def to_json(self) -> dict[str, Any]:
        # The whitelist is exactly the seven fields the decision names. The
        # run id and the shelf are not two more fields -- they are parts of
        # the one navigation target, which is where the client reads them.
        return {
            "purpose": self.purpose,
            "gateway": self.gateway,
            "started_at": self.started_at,
            "current_step": self.current_step,
            "prompt_preview": self.prompt_preview,
            "stage": self.stage,
            "navigation": {
                "href": safe_navigation(self.run_id, self.filter),
                "run_id": self.run_id,
                "filter": self.filter,
            },
        }


def safe_navigation(run_id: str, filter: str) -> str:
    """A same-origin path, and never anything else.

    A navigation target that could carry a host would let a value from the
    database decide where the browser goes, so the only thing interpolated is
    the run id and the only host in the result is none: the path is relative,
    absolute-path form, with no scheme, no authority and no ``//`` that a
    browser would read as one.
    """
    if filter not in RUN_FILTERS:
        filter = "all"
    path = "/runs/%s?filter=%s" % (quote(run_id, safe=""), quote(filter, safe=""))
    assert path.startswith("/") and not path.startswith("//")
    return path


def busy_summary_from(snapshot: RuntimeSnapshot) -> BusySummary | None:
    """Render the busy card from the authoritative snapshot.

    The stage comes from the *coordinator* state, not from the Run's phase:
    the two are different quantities and only the coordinator knows whether
    the lease is still held. A Run that is finished but still pending reads
    "saving", which is the whole point -- it is busy saving, not busy running.
    """
    active = snapshot.active_run
    if active is None:
        return None
    projection = snapshot.unrecorded_terminal_projection
    if (
        snapshot.coordinator_state == "recording_failed"
        and active.phase == "finished"
        and active.outcome == "interrupted"
        and active.started_at is None
        and active.recording_state == "failed"
        and projection is not None
        and projection.run_id == active.run_id
        and projection.session_id == active.session_id
        and projection.outcome == "interrupted"
        and projection.recording_state == "failed"
        and projection.reply_text is None
        and projection.error == "handoff_failed"
    ):
        # This Run committed its accepted row but never reached the unique
        # execution thread. If closing it as interrupted also failed, the
        # recovery projection remains authoritative for fail-closed admission,
        # but it does not make the unexecuted Run an addressable destination.
        return None
    shelf, _known = runs.classify_purpose(active.purpose)
    return BusySummary(
        purpose=active.purpose,
        gateway=active.gateway,
        started_at=active.started_at,
        current_step=active.current_step,
        prompt_preview=active.prompt_preview,
        stage=_STAGE_LABELS.get(snapshot.coordinator_state, active.phase),
        run_id=active.run_id,
        filter=shelf,
    )


@dataclass(frozen=True)
class CreateSessionResult:
    """A signed Session id, or the reason none was issued."""

    status: int = 201
    session_id: str | None = None
    code: str | None = None


class MutationGate:
    """The one door every entry-originated write goes through.

    #23 §4: locking chat alone and letting every other write through does
    not deliver serialisation -- the thing that is serial is the queue, not
    the Run. So the gate sits above both writes, and a future endpoint that
    changes memory, settings or the outside world has exactly one place to
    go instead of a convention to remember.

    **The judgement is not this object's.** A lock held for the duration of
    one call cannot express the thing that has to be guarded: the admission
    lease a Run holds runs from ``accepted`` until its recording settles
    (ADR-0026), which is minutes, not microseconds. A gate that only
    serialised the calls would answer "free" for that entire window and let
    a Session write through underneath a Run that is still saving -- which
    is precisely the promise #23 §4 was making.

    So the gate holds no lock at all. It asks the one authority that knows
    both facts -- whether a Run holds the lease and whether another write is
    inside -- and that authority answers under the lock that also decides
    admission. Two writes therefore cannot interleave, a write cannot slip
    under a Run, and a Run cannot be admitted behind a write.

    It never queues. Both operations are O(1) and non-blocking, and a
    refusal is returned the instant it is known.
    """

    def __init__(self, facade: "DashboardFacade"):
        self._facade = facade

    def submit(self, request: SubmitRequest) -> SubmitResult | None:
        """None means another write owns the gate. It never means "wait".

        A Run submit asks nothing of the gate's own slot: admission is the
        authority for Runs, and it refuses on its own terms -- with the busy
        card that says what is running. The gate only has to keep a Run from
        starting behind a write from another door.
        """
        if self._facade.mutation_in_flight():
            return None
        return self._facade.submit(request)

    def preflight_submit(self) -> SubmitResult | None:
        """Return an immediate refusal before a submit can reach slow I/O.

        This observation neither reserves admission nor owns a second busy
        state. An admissible caller must still pass through ``submit()``,
        whose Host-side reserve repeats the authoritative check and closes
        the race between this observation and the lease decision.
        """
        kind, snapshot = self._facade.admission_observe()
        if kind == "admissible":
            return None
        return SubmitResult(kind=kind, snapshot=snapshot)

    def create_session(self) -> tuple[str | None, str | None]:
        """``(session id, refusal code)`` -- exactly one of the two is set.

        The refusal comes back as the authority's own word for why the gate
        did not open, passed through rather than reinterpreted: a caller
        that has to report the reason has to report the true one, and
        "busy" is not the same thing as "closed".
        """
        reason = self._facade.try_begin_mutation()
        if reason is not None:
            return None, reason
        try:
            return self._facade.create_session(), None
        finally:
            self._facade.end_mutation()


class DashboardApi:
    """The API surface. Stateless: the Host owns every fact it reads."""

    def __init__(
        self, *, facade: DashboardFacade, gate: "MutationGate | None" = None
    ):
        self._facade = facade
        # One gate per process, shared by every write route. Injected so a
        # test can drive two writes at once without a socket.
        self._gate = gate if gate is not None else MutationGate(facade)

    # -- writes ------------------------------------------------------------

    def create_session(self) -> CreateSessionResult:
        # The id is minted by the server and never taken from the client: a
        # Session is not a label the caller gets to choose, and letting it
        # pick would let two Sessions collide into one.
        session_id, reason = self._gate.create_session()
        if session_id is None:
            # A held gate is a short-lived conflict; a recording-failed Host
            # has closed admission and is genuinely unavailable (ADR-0026).
            # Preserve the authority's code and distinguish those two facts.
            status = 503 if reason == "recording_unavailable" else 409
            return CreateSessionResult(status=status, code=reason)
        return CreateSessionResult(status=201, session_id=session_id)

    def submit(self, body: dict[str, Any]) -> SubmitOutcome:
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return SubmitOutcome(status=400, code="empty_message")
        purpose = body.get("purpose", "chat")
        if purpose not in PURPOSES:
            return SubmitOutcome(status=400, code="unknown_purpose")
        session_id = body.get("session_id")
        # A Web chat names its Session (#28). Absent -- the key missing or
        # JSON null -- is refused here, at this boundary, before the gate:
        # admission's own fallback for a Session-less chat exists for the
        # non-Web origins, and letting a Web request reach it would create a
        # Session behind the caller's back. ``is None`` is the test, never
        # truthiness: a historic Session id may be the empty string.
        if purpose == "chat" and session_id is None:
            return SubmitOutcome(status=400, code="missing_session_id")
        if session_id is not None and not isinstance(session_id, str):
            return SubmitOutcome(status=400, code="bad_session_id")
        refusal = self._gate.preflight_submit()
        if refusal is not None:
            return self._outcome(refusal)
        if session_id is not None:
            try:
                exists = self._facade.session_exists(session_id)
            except RecordingUnavailable:
                return SubmitOutcome(status=503, code="recording_unavailable")
            if not exists:
                return SubmitOutcome(status=404, code="unknown_session")

        result = self._gate.submit(
            SubmitRequest(
                message=message,
                purpose=purpose,
                session_id=session_id,
                gateway="web",
                entry_surface_id="mainbar",
                wait_for_result=False,
            )
        )
        if result is None:
            # Another write is inside the gate. A conflict, answered at
            # once: nothing in this process is unavailable.
            return SubmitOutcome(status=409, code="mutation_in_flight")
        return self._outcome(result)

    def _outcome(self, result: SubmitResult) -> SubmitOutcome:
        """Map the coordinator's answer onto the four decided HTTP shapes.

        202 is reached only through ``kind == "accepted"``, which the
        admission path sets after the lease was reserved, the accepted
        transaction committed and the work item was handed to the single
        execution thread. A handoff failure is distinct from an earlier
        internal admission failure: it closes the committed Run and reports
        service unavailability without exposing its unreachable run id.
        """
        if result.kind == "accepted":
            return SubmitOutcome(
                status=202, run_id=result.run_id, session_id=result.session_id
            )
        if result.kind == "handoff_failed":
            # The committed Run is unreachable, including through a busy-card
            # navigation target. Return before consulting a failed-recording
            # snapshot, which still carries that Run for local recovery.
            return SubmitOutcome(status=503, code="admission_failed")
        snapshot = result.snapshot or self._facade.snapshot()
        busy = busy_summary_from(snapshot)
        if result.kind == "run_in_progress":
            # Includes recording_pending: the Run still holds the lease until
            # its recording settles, and the card says so ("saving") instead
            # of showing a phase that has already ended.
            return SubmitOutcome(status=409, code="run_in_progress", busy=busy)
        if result.kind == "recording_unavailable":
            # Only ever returned once the coordinator has actually reached
            # recording_failed -- the state lands before the answer does.
            return SubmitOutcome(
                status=503, code="recording_unavailable", busy=busy
            )
        if result.kind == "mutation_in_flight":
            # The same conflict the gate reports, reached through admission
            # because the write arrived between the gate's question and the
            # reserve. One code for one fact, whichever door saw it first.
            return SubmitOutcome(status=409, code="mutation_in_flight", busy=busy)
        return SubmitOutcome(status=500, code="admission_failed", busy=busy)

    # -- reads -------------------------------------------------------------

    @_map_read_errors
    def session_inbox(self, params: dict[str, str]) -> tuple[int, Any]:
        limit = _page_size(params, "limit")
        return 200, _inbox_payload(
            self._facade.list_sessions(limit=limit, cursor=params.get("cursor"))
        )

    @_map_read_errors
    def session_messages(
        self, session_id: str, params: dict[str, str]
    ) -> tuple[int, Any]:
        page_size = _page_size(params, "page_size")
        try:
            page = self._facade.open_session(
                session_id, page_size=page_size, cursor=params.get("cursor")
            )
        except SessionNotFound:
            return 404, {"code": "unknown_session"}
        return 200, _messages_payload(page)

    @_map_read_errors
    def runs_page(self, params: dict[str, str]) -> tuple[int, Any]:
        filter_name = params.get("filter", "all")
        if filter_name not in RUN_FILTERS:
            return 400, {"code": "unknown_filter"}
        limit = _page_size(params, "limit")
        return 200, _runs_payload(
            self._facade.list_runs(
                filter=filter_name, limit=limit, cursor=params.get("cursor")
            )
        )

    @_map_read_errors
    def locate_run(self, run_id: str, params: dict[str, str]) -> tuple[int, Any]:
        limit = _page_size(params, "limit")
        page = self._facade.locate_run(run_id, limit=limit)
        if page is None:
            return 404, {"code": "unknown_run"}
        return 200, _runs_payload(page)

    @_map_read_errors
    def session_runs(self, params: dict[str, str]) -> tuple[int, Any]:
        # Same rule as the other Session-scoped reads: the Session is named
        # by the query parameter, verbatim, and only its absence is a bad
        # request -- the empty string is a value the database may hold.
        session_id = params.get("session_id")
        if session_id is None:
            return 400, {"code": "missing_session_id"}
        limit = _page_size(params, "limit")
        try:
            page = self._facade.list_session_chat_runs(
                session_id=session_id, limit=limit, cursor=params.get("cursor")
            )
        except SessionNotFound:
            return 404, {"code": "unknown_session"}
        return 200, _session_runs_payload(page)

    @_map_read_errors
    def mainbar(self, params: dict[str, str]) -> tuple[int, Any]:
        # The MainBar answers one Session, named by the query parameter like
        # the messages read: absent and empty are different facts, and only
        # a missing parameter is a bad request.
        session_id = params.get("session_id")
        if session_id is None:
            return 400, {"code": "missing_session_id"}
        limit = _page_size(params, "limit")
        try:
            page = self._facade.mainbar_pairs(
                session_id=session_id, limit=limit, cursor=params.get("cursor")
            )
        except SessionNotFound:
            return 404, {"code": "unknown_session"}
        return 200, _mainbar_payload(page)


def _page_size(params: dict[str, str], key: str) -> int:
    """A bounded ASCII-decimal page size, clamped rather than trusted.

    An unclamped limit is a denial of service with one query parameter, and
    a non-numeric one is a client bug that must not become a 500. Compare the
    normalized text with the maximum before conversion so an arbitrarily long
    query never reaches Python's process-wide integer digit limit.
    """
    raw = params.get(key)
    if raw is None:
        return DEFAULT_PAGE_SIZE
    if not raw or any(character < "0" or character > "9" for character in raw):
        return DEFAULT_PAGE_SIZE
    normalized = raw.lstrip("0")
    if not normalized:
        return 1
    maximum = str(MAX_PAGE_SIZE)
    if len(normalized) > len(maximum) or (
        len(normalized) == len(maximum) and normalized > maximum
    ):
        return MAX_PAGE_SIZE
    return int(normalized)


def _inbox_payload(page: SessionInboxPage) -> dict[str, Any]:
    return {
        "sessions": [
            {
                "session_id": summary.session_id,
                "created_at": summary.created_at,
                "activity_revision": summary.activity_revision,
                "title": summary.title,
            }
            for summary in page.sessions
        ],
        "next_cursor": page.next_cursor,
    }


def _messages_payload(page: SessionMessagesPage) -> dict[str, Any]:
    return {
        "session_id": page.session_id,
        "title": page.title,
        "messages": [
            {
                "role": message.role,
                "blocks": [
                    _block_json(block) for block in message.blocks
                ],
                "source": message.source,
                "created_at": message.created_at,
                # Null for historic rows, and that is the point: a message
                # with no Run is never dressed up as one that had one.
                "run_id": message.run_id,
            }
            for message in page.messages
        ],
        "next_cursor": page.next_cursor,
        "runs_pending": page.runs_pending,
    }


def _block_json(block) -> dict[str, Any]:
    from agent_alfred.messages import (
        TextBlock,
        ThinkingBlock,
        ToolCallBlock,
        ToolResultBlock,
    )

    # MainBar and the session view render text only; every other block is
    # reported as a type and a count, never as its contents (#30).
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking"}
    if isinstance(block, ToolCallBlock):
        return {"type": "tool_call"}
    if isinstance(block, ToolResultBlock):
        return {"type": "tool_result", "count": len(block.content)}
    return {"type": type(block).__name__}


def _runs_payload(page) -> dict[str, Any]:
    return {
        "filter": page.filter,
        "runs": [_run_json(run) for run in page.runs],
        # Deliberately outside ``runs``: the client pins it and folds it away
        # by run_id. A non-terminal Run keeps taking new revisions, so paging
        # over it would show it twice.
        "non_terminal": (
            None if page.non_terminal is None else _run_json(page.non_terminal)
        ),
        "next_cursor": page.next_cursor,
    }


def _run_json(run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "purpose": run.purpose,
        "filter": run.filter,
        "purpose_known": run.purpose_known,
        "session_id": run.session_id,
        "gateway": run.gateway,
        "entry_surface_id": run.entry_surface_id,
        "prompt_preview": run.prompt_preview,
        "phase": run.phase,
        "outcome": run.outcome,
        "accepted_at": run.accepted_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "activity_revision": run.activity_revision,
    }


def _mainbar_payload(page) -> dict[str, Any]:
    return {
        "items": [_mainbar_item_json(item) for item in page.items],
        "next_cursor": page.next_cursor,
        "runs_pending": page.runs_pending,
    }


def _mainbar_item_json(item) -> dict[str, Any]:
    if isinstance(item, runs.MainBarRunPair):
        return {
            "type": "run_pair",
            "run_id": item.run_id,
            "activity_revision": item.activity_revision,
            "session_id": item.session_id,
            "created_at": item.created_at,
            "user": (
                None
                if item.user_message is None
                else [_block_json(block) for block in item.user_message.blocks]
            ),
            "assistant": (
                None
                if item.assistant_message is None
                else [_block_json(block) for block in item.assistant_message.blocks]
            ),
        }
    if not isinstance(item, runs.MainBarHistoricMessage):
        raise TypeError(f"unknown MainBar item: {type(item).__name__}")
    return {
        "type": "historic_message",
        "run_id": item.run_id,
        "role": item.message.role,
        "blocks": [_block_json(block) for block in item.message.blocks],
        "source": item.source,
        "created_at": item.created_at,
    }


def _session_runs_payload(page) -> dict[str, Any]:
    return {
        "session_id": page.session_id,
        "runs": [
            {
                "run_id": run.run_id,
                "phase": run.phase,
                "outcome": run.outcome,
                "accepted_at": run.accepted_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "activity_revision": run.activity_revision,
                "reply_preview": run.reply_preview,
                "reply_source": run.reply_source,
            }
            for run in page.runs
        ],
        "next_cursor": page.next_cursor,
    }
