"""The production configuration read path (slice-1 re-review).

``Settings`` used to be injectable-only: the default wiring built ``Settings()``
and the persona was a hard-coded string, so no CLI user could actually
configure anything. These tests pin the fail-fast ``load_settings`` path, the
persona-file priority, and that the real ``build_default_host`` assembly picks
user configuration up -- not a test-constructed ``Settings``.
"""

from __future__ import annotations

import io
import sqlite3

import pytest

from agent_alfred.gateway.cli import build_parser
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import (
    DEFAULT_PERSONA,
    Settings,
    SettingsError,
    load_settings,
)
from agent_alfred.wiring import build_default_host

# --- environment layer -------------------------------------------------------


def test_environment_overrides_reach_settings(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ALFRED_MAX_STEPS", "3")
    monkeypatch.setenv("AGENT_ALFRED_MAX_TOKENS", "256")
    monkeypatch.setenv("AGENT_ALFRED_OVERALL_DEADLINE_S", "12.5")
    monkeypatch.setenv("AGENT_ALFRED_PER_ATTEMPT_TIMEOUT_S", "4")
    monkeypatch.setenv("AGENT_ALFRED_STREAM", "true")
    monkeypatch.setenv("AGENT_ALFRED_STREAM_FALLBACK", "false")
    monkeypatch.setenv("AGENT_ALFRED_WORKING_MEMORY_ROUNDS", "0")

    settings = load_settings()
    assert settings == Settings(
        max_steps=3,
        max_tokens=256,
        overall_deadline_s=12.5,
        per_attempt_timeout_s=4.0,
        stream=True,
        stream_fallback=False,
        working_memory_rounds=0,
        persona=DEFAULT_PERSONA,
    )


def test_max_steps_zero_is_a_legal_configured_value(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ALFRED_MAX_STEPS", "0")
    assert load_settings().max_steps == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_ALFRED_MAX_STEPS", "abc"),
        ("AGENT_ALFRED_MAX_STEPS", "-1"),
        ("AGENT_ALFRED_MAX_STEPS", "1.5"),
        ("AGENT_ALFRED_MAX_TOKENS", "0"),
        ("AGENT_ALFRED_MAX_TOKENS", "nope"),
        ("AGENT_ALFRED_OVERALL_DEADLINE_S", "0"),
        ("AGENT_ALFRED_OVERALL_DEADLINE_S", "-3"),
        ("AGENT_ALFRED_OVERALL_DEADLINE_S", "soon"),
        ("AGENT_ALFRED_PER_ATTEMPT_TIMEOUT_S", "0"),
        ("AGENT_ALFRED_WORKING_MEMORY_ROUNDS", "-2"),
        ("AGENT_ALFRED_STREAM", "maybe"),
        ("AGENT_ALFRED_STREAM_FALLBACK", "2"),
    ],
)
def test_illegal_values_fail_fast_with_the_variable_named(
    monkeypatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError, match=name):
        load_settings()


# --- persona file ------------------------------------------------------------


def test_persona_file_overrides_the_builtin_default(tmp_path) -> None:
    persona = tmp_path / "persona.md"
    persona.write_text("你是 Alfred，本地优先的私人助手。", encoding="utf-8")
    settings = load_settings(persona_file=str(persona))
    assert settings.persona == "你是 Alfred，本地优先的私人助手。"


def test_persona_file_via_environment(monkeypatch, tmp_path) -> None:
    persona = tmp_path / "persona.md"
    persona.write_text("env persona", encoding="utf-8")
    monkeypatch.setenv("AGENT_ALFRED_PERSONA_FILE", str(persona))
    assert load_settings().persona == "env persona"


def test_explicit_persona_file_beats_the_environment(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "env.md"
    env_file.write_text("env persona", encoding="utf-8")
    flag_file = tmp_path / "flag.md"
    flag_file.write_text("flag persona", encoding="utf-8")
    monkeypatch.setenv("AGENT_ALFRED_PERSONA_FILE", str(env_file))
    assert load_settings(persona_file=str(flag_file)).persona == "flag persona"


def test_no_persona_file_falls_back_to_the_builtin_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_ALFRED_PERSONA_FILE", raising=False)
    assert load_settings().persona == DEFAULT_PERSONA


@pytest.mark.parametrize(
    "content",
    [b"no such content \xff\xfe", b"   \n  \t "],
)
def test_invalid_persona_files_are_clear_errors(
    monkeypatch, tmp_path, content: bytes
) -> None:
    persona = tmp_path / "persona.md"
    persona.write_bytes(content)
    monkeypatch.setenv("AGENT_ALFRED_PERSONA_FILE", str(persona))
    with pytest.raises(SettingsError, match="persona"):
        load_settings()


def test_missing_persona_file_is_a_clear_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(
        "AGENT_ALFRED_PERSONA_FILE", str(tmp_path / "absent.md")
    )
    with pytest.raises(SettingsError, match="not found"):
        load_settings()


def test_unreadable_persona_file_is_a_clear_error(monkeypatch, tmp_path) -> None:
    persona = tmp_path / "persona.md"
    persona.write_text("secret persona", encoding="utf-8")
    persona.chmod(0o000)
    monkeypatch.setenv("AGENT_ALFRED_PERSONA_FILE", str(persona))
    try:
        with pytest.raises(SettingsError, match="cannot be read"):
            load_settings()
    finally:
        persona.chmod(0o600)


# --- CLI flag layer ----------------------------------------------------------


def test_cli_flags_map_onto_load_settings_overrides(tmp_path) -> None:
    persona = tmp_path / "p.md"
    persona.write_text("cli persona", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "--max-steps",
            "5",
            "--max-tokens",
            "99",
            "--overall-deadline",
            "30",
            "--per-attempt-timeout",
            "7",
            "--stream",
            "--no-stream-fallback",
            "--working-memory-rounds",
            "2",
            "--persona-file",
            str(persona),
        ]
    )
    settings = load_settings(
        {},
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        overall_deadline_s=args.overall_deadline,
        per_attempt_timeout_s=args.per_attempt_timeout,
        stream=args.stream,
        stream_fallback=args.stream_fallback,
        working_memory_rounds=args.working_memory_rounds,
        persona_file=args.persona_file,
    )
    assert settings.max_steps == 5
    assert settings.max_tokens == 99
    assert settings.overall_deadline_s == 30
    assert settings.per_attempt_timeout_s == 7
    assert settings.stream is True
    assert settings.stream_fallback is False
    assert settings.working_memory_rounds == 2
    assert settings.persona == "cli persona"


def test_cli_flags_override_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ALFRED_MAX_STEPS", "1")
    monkeypatch.setenv("AGENT_ALFRED_STREAM", "true")
    args = build_parser().parse_args(["--max-steps", "9", "--no-stream"])
    settings = load_settings(
        {},
        max_steps=args.max_steps,
        stream=args.stream,
    )
    assert settings.max_steps == 9
    assert settings.stream is False


def test_illegal_cli_value_fails_fast(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ALFRED_HOME", str(tmp_path / "state"))
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--max-steps", "not-a-number"])


# --- the real assembly path --------------------------------------------------


def test_build_default_host_takes_user_configuration_from_the_environment(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AGENT_ALFRED_MAX_STEPS", "0")
    persona = tmp_path / "persona.md"
    persona.write_text("assembly-path persona", encoding="utf-8")
    monkeypatch.setenv("AGENT_ALFRED_PERSONA_FILE", str(persona))

    # No settings argument: the production read path must resolve them.
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    host.start()
    try:
        assert host.settings.max_steps == 0
        assert host.settings.persona == "assembly-path persona"
        # The configured budget is the one the loop actually enforces.
        submitted = host.submit(SubmitRequest(message="hi"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "max_steps"
    finally:
        host.close()


def test_persona_never_enters_the_session_record_or_the_transcript(
    monkeypatch, tmp_path
) -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    from agent_alfred import schema
    from agent_alfred.clock import FakeClock
    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.runtime.host import RuntimeHost

    schema.migrate(conn)
    persona = "PERSONA-MARKER-7f3a never store this"
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(["pong"])),
        settings=Settings(persona=persona),
        clock=FakeClock(),
        fanout=FanOutSink([capture], process_instance_id="proc-persona"),
        process_instance_id="proc-persona",
    )
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(
            SubmitRequest(message="hello", session_id=session_id)
        )
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        # The session record only ever holds the user/assistant pair.
        blob = " ".join(
            str(row[0])
            for row in conn.execute("SELECT content FROM agent_log").fetchall()
        )
        assert persona not in blob
        # The run transcript the model receives has no system entry; the
        # persona rides in request.system only (per-request composition).
        model = host._factory._model
        assert model.requests, "the scripted model saw the request"
        assert [m.role for m in model.requests[0].messages] == ["user"]
        transcript_blob = repr(model.requests[0].messages)
        assert persona not in transcript_blob
    finally:
        host.close()


def test_cli_stream_setting_reaches_the_admission_snapshot(tmp_path) -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    from agent_alfred import schema

    schema.migrate(conn)
    settings = Settings(stream=True)
    # The gateway passes the configured stream flag through SubmitRequest;
    # assert on the admission seam the factory sees.
    from agent_alfred.clock import FakeClock
    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.gateway.cli import _send
    from agent_alfred.runtime.host import RuntimeHost

    capture = CapturingSink(name="capture", flush_at_run_end=True)
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(["pong"])),
        settings=settings,
        clock=FakeClock(),
        fanout=FanOutSink([capture], process_instance_id="proc-stream-cli"),
        process_instance_id="proc-stream-cli",
    )
    out = io.StringIO()
    host.start()
    try:
        session_id = host.create_session()
        _send(host, "hi", session_id, out, stream=settings.stream)
        assert host._factory.snapshots[0].stream is True
    finally:
        host.close()
