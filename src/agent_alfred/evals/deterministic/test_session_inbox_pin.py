"""An active Session moves independently of the inbox's persistent cursor."""

from agent_alfred.evals.deterministic._thread_test_helpers import EnteredEvent
from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
    build_runtime_host,
    dashboard_api,
)


def test_active_session_crossing_an_old_cursor_is_pinned_once_outside_the_page():
    recording = EnteredEvent()
    host, conn = build_runtime_host(["reply"], before_recording_commit=recording)
    host.start()
    try:
        api = dashboard_api(host)
        old = api.create_session().session_id
        middle = api.create_session().session_id
        newest = api.create_session().session_id
        status, first = api.session_inbox({"limit": "1"})
        assert status == 200
        assert [s["session_id"] for s in first["sessions"]] == [newest]
        cursor = first["next_cursor"]
        assert cursor is not None

        outcome = api.submit(
            {"message": "old session becomes active", "session_id": old}
        )
        assert outcome.status == 202
        assert recording.entered.wait(2), "Run did not reach recording"

        status, second = api.session_inbox({"limit": "1", "cursor": cursor})
        assert status == 200
        assert second["non_terminal"]["session_id"] == old
        assert second["non_terminal"]["title"] == "old session becomes active"
        assert [s["session_id"] for s in second["sessions"]] == [middle]
        assert second["next_cursor"] is None
        assert api.session_inbox({"limit": "1", "cursor": cursor}) == (200, second)

        status, refreshed = api.session_inbox({"limit": "1"})
        assert status == 200
        assert refreshed["non_terminal"]["session_id"] == old
        assert [s["session_id"] for s in refreshed["sessions"]] == [newest]
        recording.set()
        assert host.close(timeout=2)
        status, settled = api.session_inbox({"limit": "1"})
        assert status == 200
        assert settled["non_terminal"] is None
        assert [s["session_id"] for s in settled["sessions"]] == [old]
    finally:
        recording.set()
        assert host.close(timeout=2)
        conn.close()
