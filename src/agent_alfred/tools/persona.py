"""Managed persona versions, with explicit external configuration precedence."""

import json

from agent_alfred.messages import TextBlock
from agent_alfred.tools import Tool, ToolFailure, ToolSuccess
from agent_alfred.tools.calendar import object_schema, string_schema
from agent_alfred.tools.files import digest, operation_id

TARGET = "persona/persona.md"


class PersonaTools:
    def __init__(self, files, settings, *, read_observer=None):
        self._files = files
        self._settings = settings
        self._read_observer = read_observer

    def current(self):
        if self._settings.persona_file is not None or self._files.state_path is None:
            return self._settings.persona
        pending = self._files.pending_persona()
        if pending is not None:
            return (
                pending[0] if pending[0] is not None else self._settings.persona
            ) + ("\n[受管人格操作尚未核验；本 Run 使用上次确认的人格。]")
        content = self._files.read_file(TARGET)
        if content is None:
            if self._files.target_created(TARGET):
                raise ValueError("managed_persona_missing")
            return self._settings.persona
        if not content.strip():
            raise ValueError("managed_persona_invalid")
        return content

    def declarations(self):
        return (
            Tool(
                "read_persona",
                "Read the complete effective persona and its version.",
                object_schema({}),
                self.read,
                "local_read",
            ),
            Tool(
                "update_persona",
                "Replace the managed persona using the read version. "
                "Preserve instructions the user did not ask to change. "
                "Next Run applies it.",
                object_schema(
                    {"content": string_schema(), "expected_version": string_schema()},
                    ("content", "expected_version"),
                ),
                self.update,
                "local_write",
            ),
        )

    def read(self, args, context):
        with self._files.tool_deadline(context):
            return self._read(args, context)

    def _read(self, args, context):
        context.checkpoint()
        content = self.current()
        result = ToolSuccess(
            (
                TextBlock(
                    json.dumps(
                        {
                            "content": content,
                            "version": digest(content),
                            "external_override": self._settings.persona_file
                            is not None,
                        },
                        ensure_ascii=False,
                    )
                ),
            )
        )
        if self._read_observer is not None:
            self._read_observer(self, result, context)
        return result

    def update(self, args, context):
        with self._files.tool_deadline(context):
            return self._update(args, context)

    def _update(self, args, context):
        context.checkpoint()
        if self._settings.persona_file is not None:
            return ToolFailure(
                "execution_error",
                (
                    TextBlock(
                        "Explicit persona file is active; "
                        "remove the override before editing the managed persona."
                    ),
                ),
            )
        if self._files.state_path is None:
            return ToolFailure(
                "unavailable", (TextBlock("State directory unavailable."),)
            )
        op = operation_id(context)
        old = self._files.get_operation(op)
        if old is not None:
            with self._files.recording_store.reading() as conn:
                row = conn.execute(
                    "SELECT expected_digest,original_content FROM file_operations "
                    "WHERE operation_id=?",
                    (op,),
                ).fetchone()
            original = row[1] if row[1] is not None else self._settings.persona
            if digest(original) != args["expected_version"]:
                return ToolFailure(
                    "execution_error", (TextBlock("Operation parameter mismatch."),)
                )
            return self._files.replay(
                op, "update_persona", TARGET, args["content"], row[0]
            )
        if self._files.target_pending(TARGET):
            return ToolFailure(
                "unavailable", (TextBlock("Persona awaiting recovery."),)
            )
        content = self.current()
        if digest(content) != args["expected_version"]:
            return ToolFailure(
                "execution_error", (TextBlock("Persona version conflict; read again."),)
            )
        if not args["content"].strip():
            return ToolFailure(
                "invalid_input", (TextBlock("Persona cannot be empty."),)
            )
        existing = self._files.read_file(TARGET)
        return self._files.write(
            operation_id(context),
            "update_persona",
            TARGET,
            args["content"],
            None if existing is None else digest(content),
            context,
        )
