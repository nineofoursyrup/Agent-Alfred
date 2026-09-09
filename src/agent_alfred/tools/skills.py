"""Managed creation uses the current directory, not a stale catalog snapshot."""

import json
from pathlib import Path

from agent_alfred.messages import TextBlock
from agent_alfred.skills.catalog import NAME, SkillCatalog, parse_skill, read_directory
from agent_alfred.tools import Tool, ToolContext, ToolFailure, ToolSuccess
from agent_alfred.tools.calendar import object_schema, string_schema
from agent_alfred.tools.files import digest, operation_id


class SkillTools:
    def __init__(self, files, *, builtin=None):
        self._files = files
        self._store = files.recording_store
        self.builtin = builtin or Path(__file__).parents[1] / "skills" / "builtin"
        self.user = None if files.state_path is None else files.state_path / "skills"
        self.catalog = (
            None
            if self.user is None
            else SkillCatalog(
                builtin=self.builtin,
                user=self.user,
                excluded=tuple(
                    files.state_path / target for target in files.unverified_targets()
                ),
            )
        )

    def declarations(self):
        return (
            Tool(
                "create_skill",
                "Create a new user Skill; requires restart to load. "
                "Existing user names are protected; "
                "builtin overrides need user confirmation.",
                object_schema(
                    {key: string_schema() for key in ("name", "description", "body")},
                    ("name", "description", "body"),
                ),
                self.create,
                "local_write",
            ),
        )

    def create(self, args, context):
        with self._files.tool_deadline(context):
            return self._create(args, context)

    def _create(self, args, context):
        context.checkpoint()
        if self._store.transaction_in_progress:
            return ToolFailure(
                "execution_error", (TextBlock("Caller transaction is active."),)
            )
        if self.user is None:
            return ToolFailure(
                "unavailable", (TextBlock("State directory unavailable."),)
            )
        name = args["name"]
        if NAME.fullmatch(name) is None:
            return ToolFailure("invalid_input", (TextBlock("Use a valid Skill name."),))
        content = (
            "---\nname: "
            + json.dumps(name)
            + "\ndescription: "
            + json.dumps(args["description"], ensure_ascii=False)
            + "\n---\n"
            + args["body"]
        )
        replay = self._files.replay(
            operation_id(context),
            "create_skill",
            f"skills/{name}/SKILL.md",
            content,
            None,
        )
        if replay is not None:
            return replay
        candidate = operation_id(context)
        with self._store.reading() as conn:
            previous = conn.execute(
                "SELECT content_digest FROM skill_candidates WHERE candidate_id=?",
                (candidate,),
            ).fetchone()
        if previous is not None:
            if previous[0] != digest(content):
                return ToolFailure(
                    "execution_error", (TextBlock("Candidate parameter mismatch."),)
                )
            return self.candidate_action(candidate, "查看候选")
        if name in read_directory(self.user, "user", checkpoint=self._files.checkpoint):
            return ToolFailure(
                "execution_error",
                (TextBlock("User Skill exists; choose another name."),),
            )
        try:
            parse_skill(content)
        except ValueError:
            return ToolFailure("invalid_input", (TextBlock("Invalid Skill metadata."),))
        builtins = read_directory(
            self.builtin, "builtin", checkpoint=self._files.checkpoint
        )
        if name in builtins:
            context.checkpoint()
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO skill_candidates VALUES (?,?,?,?,?,"
                    "'awaiting_confirmation',?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        candidate,
                        name,
                        content,
                        digest(content),
                        digest(builtins[name][2]),
                        context.run_id,
                        context.step_index,
                        context.call_id,
                        context.source,
                        context.session_id,
                    ),
                )
                conn.commit()
            return ToolSuccess(
                (
                    TextBlock(
                        json.dumps(
                            {
                                "candidate_id": candidate,
                                "state": "awaiting_confirmation",
                                "name": name,
                                "description": args["description"],
                                "body": args["body"],
                                "warning": "将整体覆盖内置 Skill；"
                                "先查看完整候选，再确认创建。",
                                "view_command": "查看候选 " + candidate,
                            },
                            ensure_ascii=False,
                        )
                    ),
                )
            )
        result = self._files.write(
            operation_id(context),
            "create_skill",
            f"skills/{name}/SKILL.md",
            content,
            None,
            context,
        )
        if isinstance(result, ToolSuccess) and result.stop_reason is None:
            receipt = json.loads(result.content[0].text)
            receipt["activation"] = "重启后加载；尚未匹配或注入。"
            return ToolSuccess((TextBlock(json.dumps(receipt, ensure_ascii=False)),))
        return result

    def handle_command(self, message):
        for prefix in ("查看候选 ", "确认创建 ", "取消 "):
            if message.startswith(prefix):
                candidate = message[len(prefix) :].strip()
                return self.candidate_action(candidate, prefix.strip())
        return None

    def candidate_action(self, candidate, action):
        if self._store.transaction_in_progress:
            return ToolFailure(
                "execution_error", (TextBlock("Caller transaction is active."),)
            )
        if action not in ("查看候选", "取消", "确认创建"):
            return ToolFailure(
                "invalid_input", (TextBlock("Invalid candidate action."),)
            )
        with self._store.reading() as conn:
            row = conn.execute(
                "SELECT name,content,content_digest,builtin_digest,state,"
                "run_id,step_index,call_id,source,session_id "
                "FROM skill_candidates WHERE candidate_id=?",
                (candidate,),
            ).fetchone()
        if row is None:
            return ToolFailure("invalid_input", (TextBlock("Unknown candidate."),))
        (
            name,
            content,
            expected,
            builtin_digest,
            state,
            run,
            step,
            call,
            source,
            session,
        ) = row
        if action == "查看候选":
            return ToolSuccess(
                (
                    TextBlock(
                        json.dumps(
                            {
                                "candidate_id": candidate,
                                "state": state,
                                "name": name,
                                "content": content,
                                "warning": "整体覆盖内置 Skill。",
                                "confirm_command": "确认创建 " + candidate,
                            },
                            ensure_ascii=False,
                        )
                    ),
                )
            )
        if action == "取消":
            if state == "awaiting_confirmation":
                with self._store.transaction() as conn:
                    conn.execute(
                        "UPDATE skill_candidates SET state='cancelled' "
                        "WHERE candidate_id=?",
                        (candidate,),
                    )
                    conn.commit()
                state = "cancelled"
            return ToolSuccess(
                (TextBlock(json.dumps({"candidate_id": candidate, "state": state})),)
            )
        if state in ("cancelled", "invalidated"):
            return ToolFailure(
                "execution_error", (TextBlock("Candidate is no longer confirmable."),)
            )
        if state == "confirmed" and self._files.get_operation(candidate) is not None:
            return self._files.recover(candidate)
        try:
            parse_skill(content)
        except ValueError:
            return ToolFailure("invalid_input", (TextBlock("Invalid Skill metadata."),))
        builtins = read_directory(
            self.builtin, "builtin", checkpoint=self._files.checkpoint
        )
        if (
            digest(content) != expected
            or name not in builtins
            or digest(builtins[name][2]) != builtin_digest
            or name
            in read_directory(self.user, "user", checkpoint=self._files.checkpoint)
        ):
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE skill_candidates SET state='invalidated' "
                    "WHERE candidate_id=?",
                    (candidate,),
                )
                conn.commit()
            return ToolFailure(
                "execution_error",
                (TextBlock("Candidate changed or target exists; not created."),),
            )
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE skill_candidates SET state='confirmed' WHERE candidate_id=?",
                (candidate,),
            )
            conn.commit()
        return self._files.write(
            candidate,
            "create_skill",
            f"skills/{name}/SKILL.md",
            content,
            None,
            ToolContext(run, step, call, source, float("inf"), session),
        )
