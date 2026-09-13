"""Optional local MCP bridge. Host owns admission, publication and shutdown."""

import hashlib
import json
import re
import time

from agent_alfred.atomic_config import read_bytes
from agent_alfred.mcp.transport import Session, TransportError
from agent_alfred.mcp.validation import Validator
from agent_alfred.messages import TextBlock
from agent_alfred.tools import Tool, ToolFailure, ToolPolicy, ToolSuccess, UnknownCost


class MCPBridge:
    def __init__(self, directory, env, clock, redactor):
        self.directory, self.env = directory, dict(env)
        self.clock, self.redactor = clock, redactor
        self.validator = Validator()
        self.rows = {}
        self.revision = 0
        self.error = None
        self.config_fingerprint = None
        self.config = {}
        self.resources = None
        self.aliases = {}
        self.on_unavailable = lambda key, reason: None

    def prepare(self):
        from agent_alfred.mcp.resources import Resources
        from agent_alfred.memory.audit import AuditKey

        previous = getattr(self, "key_fingerprint", None)
        try:
            key = AuditKey.load_or_create(self.directory / "mcp.key", self.redactor)
            fingerprint = read_bytes(self.directory / "mcp.key")[1]
        except Exception:
            self.pause("identity_unavailable")
            raise
        self.key, self.key_fingerprint = key, fingerprint
        if self.resources is None:
            try:
                self.resources = Resources(self.directory)
            except OSError, ValueError, TypeError:
                self.pause("resource_ownership_unconfirmed")
                raise
        return previous is not None and previous != fingerprint

    def start(self):
        from agent_alfred.mcp.config import read

        if self.directory is None:
            return
        try:
            self.prepare()
        except Exception:
            return
        try:
            config, fingerprint = read(self.directory, self.env, self.redactor)
        except ValueError, OSError, TypeError:
            self.error = "configuration_invalid"
            return
        self.config_fingerprint = fingerprint
        self.config = config
        self.apply(config)

    def apply(self, config, reconnect=()):
        if self.prepare():
            self.pause("identity_changed")
            reconnect = reconnect or tuple(config)
        elif (
            self.error
            in ("publication_unconfirmed", "identity_unavailable", "identity_changed")
            and not reconnect
        ):
            reconnect = tuple(config)
        deadline = time.monotonic() + 30
        for key in list(self.rows):
            old = self.rows[key]
            definition = config.get(key)
            if (
                definition is not None
                and old["definition"] == definition
                and key not in reconnect
            ):
                continue
            session = old["session"]
            old.update(state="configured_untested", reason="reconnect_required")
            if session is not None and not session.close():
                old.update(state="error", reason="cleanup_incomplete")
                continue
            if session is not None:
                self.resources.closed(key)
            if definition is None:
                del self.rows[key]
        for key, definition in config.items():
            old = self.rows.get(key)
            if old is not None:
                if old["reason"] == "cleanup_incomplete":
                    continue
                if old["definition"] == definition and key not in reconnect:
                    continue
            row = {
                "server_key": key,
                "enabled": definition.enabled,
                "state": "configured_untested",
                "reason": "disabled",
                "tools": [],
                "session": None,
                "definition": definition,
                "history": {
                    k: old.get(k)
                    for k in ("state", "reason", "source", "server_info", "observed_at")
                }
                if old
                else None,
            }
            row["historical_tools"] = (
                (old.get("tools") or old.get("historical_tools", [])) if old else []
            )
            row["historical_source"] = (
                old.get("source", old.get("historical_source")) if old else None
            )
            self.rows[key] = row
            if definition.enabled:
                if time.monotonic() >= deadline:
                    row["reason"] = "not_attempted"
                else:
                    self.discover(row, min(deadline, time.monotonic() + 10))
        self.revision += 1

    def discover(self, row, deadline):
        import sys

        if sys.platform not in ("linux", "darwin"):
            row.update(state="error", reason="unsupported_platform")
            return
        if not self.validator.available():
            row.update(state="error", reason="dependency_missing")
            return
        definition = row["definition"]
        try:
            self.resources.intent(row["server_key"])
            session = Session(self.redactor)
            row["session"] = session
            session.start(
                definition.command, definition.args, definition.cwd, definition.env
            )
            self.resources.spawned(row["server_key"], session.process.pid)
            response = session.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-alfred", "version": "0.1.0"},
                },
                deadline,
            )
            handshake = response["result"]
            if (
                not isinstance(handshake, dict)
                or handshake.get("protocolVersion") != "2025-11-25"
                or not isinstance(handshake.get("capabilities"), dict)
                or not isinstance(handshake.get("serverInfo"), dict)
                or any(
                    not isinstance(handshake["serverInfo"].get(k), str)
                    for k in ("name", "version")
                )
            ):
                raise ValueError("invalid_handshake")
            row["source"] = self.fingerprint(
                [
                    "source-v1",
                    definition.source(),
                    handshake["protocolVersion"],
                    handshake["serverInfo"],
                ]
            )
            row["server_info"] = handshake["serverInfo"]
            session.notify("notifications/initialized", {}, deadline)
            tools, cursors, names = [], set(), set()
            cursor, size = None, 0
            if "tools" in handshake["capabilities"]:
                for page in range(20):
                    response = session.request(
                        "tools/list",
                        {} if cursor is None else {"cursor": cursor},
                        deadline,
                    )
                    size += session.last_response_bytes
                    data = response["result"]
                    if not isinstance(data, dict) or not isinstance(
                        data.get("tools"), list
                    ):
                        raise ValueError("invalid_tools_list")
                    for tool in data["tools"]:
                        if (
                            not isinstance(tool, dict)
                            or not isinstance(tool.get("name"), str)
                            or not tool["name"]
                            or tool["name"] in names
                        ):
                            raise ValueError("duplicate_or_invalid_tool")
                        if "description" in tool and not isinstance(
                            tool["description"], str
                        ):
                            raise ValueError("invalid_description")
                        names.add(tool["name"])
                        tools.append(tool)
                    if size > 4 * 1024 * 1024 or len(tools) > 100:
                        raise ValueError("discovery_limit")
                    if "nextCursor" not in data:
                        break
                    cursor = data["nextCursor"]
                    if not isinstance(cursor, str) or cursor in cursors or page == 19:
                        raise ValueError("pagination_invalid")
                    cursors.add(cursor)
            if (
                len(tools)
                + sum(len(r["tools"]) for r in self.rows.values() if r is not row)
                > 400
            ):
                raise ValueError("total_tool_limit")
            for tool in tools:
                reason = self.validator.validate(tool.get("inputSchema"), deadline)
                if reason is None and "outputSchema" in tool:
                    reason = self.validator.validate(tool["outputSchema"], deadline)
                tool["_unavailable"] = reason
                tool["_identity"] = self.fingerprint(
                    [
                        "capability-v1",
                        row["source"],
                        tool["name"],
                        tool.get("description", ""),
                        tool.get("inputSchema"),
                        tool.get("outputSchema"),
                    ]
                )
            row.update(
                state="connected",
                reason=None,
                tools=tools,
                observed_at=self.clock.wall_utc().isoformat(),
            )
        except (KeyError, ValueError, OSError, TypeError, TransportError) as exc:
            row.update(
                state="error",
                reason=exc.reason
                if isinstance(exc, TransportError)
                else "discovery_failed",
            )
            if row["session"] is not None and not row["session"].close():
                row["reason"] = "cleanup_incomplete"
        except BaseException:
            if row["session"] is not None:
                row["session"].close()
            raise

    def fingerprint(self, value):
        content = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        digest, key_id = self.key.fingerprint(content)
        return "v1:" + key_id + ":" + digest

    def declarations(self, reserved=()):
        result, used = [], set(reserved)
        self.aliases = {}
        for key, row in sorted(self.rows.items()):
            for item in sorted(row["tools"], key=lambda t: t["name"]):
                raw = key + "_" + item["name"]
                name = "mcp_" + re.sub("[^a-zA-Z0-9_-]", "_", raw)
                if len(name) > 64:
                    name = (
                        name[:51] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:12]
                    )
                stem = name
                for suffix in range(402):
                    if name not in used:
                        break
                    addition = "_" + str(suffix + 1)
                    name = stem[: 64 - len(addition)] + addition
                else:
                    raise ValueError("alias_collision")
                used.add(name)
                self.aliases[name] = (key, item)
                schema = item.get("inputSchema")
                # Preserve unusable declarations for human diagnostics; they are
                # never exposed to models and never used as guidance schemas.
                schema = schema if isinstance(schema, dict) else {}
                result.append(
                    Tool(
                        name,
                        item.get("description", ""),
                        schema,
                        lambda args, ctx, r=row, t=item: self.call(r, t, args, ctx),
                        "external",
                        budget_s=row["definition"].timeout_s,
                        source_id="mcp:" + row["source"],
                        capability_id=item["_identity"],
                        schema_mode="mcp",
                    )
                )
        return tuple(result)

    def policies(self):
        return {
            name: ToolPolicy(
                configured=True,
                unavailable_reason=item.get("_unavailable")
                or (
                    self.rows[key]["reason"]
                    if self.rows[key]["state"] != "connected"
                    else None
                ),
            )
            for name, (key, item) in self.aliases.items()
        }

    def identity_healthy(self):
        if self.directory is None or not self.config:
            return True
        try:
            return read_bytes(self.directory / "mcp.key")[1] == self.key_fingerprint
        except OSError, ValueError, AttributeError:
            return False

    def call(self, row, tool, args, context):
        from agent_alfred.tools import NotSentCost

        if not self.identity_healthy():
            self.pause("identity_unavailable")
        if row["state"] != "connected":
            return ToolFailure(
                "unavailable",
                (TextBlock("MCP connection unavailable"),),
                cost=NotSentCost("transport_not_sent"),
            )
        sent = False
        try:
            response = row["session"].request(
                "tools/call",
                {"name": tool["name"], "arguments": args},
                context.deadline,
                context.monotonic,
            )
            sent = True
            if "error" in response:
                code = response["error"]["code"]
                if code in (-32601, -32602):
                    return ToolFailure(
                        "execution_error",
                        (TextBlock(f"MCP protocol code {code}"),),
                        cost=UnknownCost(),
                    )
                raise ValueError("unverified_rpc_error")
            reply = response["result"]
            if not isinstance(reply, dict) or not isinstance(
                reply.get("content"), list
            ):
                raise ValueError("invalid_result")
            if type(reply.get("isError", False)) is not bool:
                raise ValueError("invalid_is_error")
            if "structuredContent" in reply and not isinstance(
                reply["structuredContent"], dict
            ):
                raise ValueError("invalid_structured_content")
            text = []
            for block in reply["content"]:
                if not isinstance(block, dict):
                    raise ValueError("invalid_content")
                kind = block.get("type")
                if kind == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError("invalid_text")
                    text.append(block["text"])
                elif kind in ("image", "audio"):
                    if not all(
                        isinstance(block.get(k), str) for k in ("data", "mimeType")
                    ):
                        raise ValueError("invalid_media")
                    text.append(f"[不支持的非文本内容：{kind}]")
                elif kind == "resource_link":
                    if not all(isinstance(block.get(k), str) for k in ("uri", "name")):
                        raise ValueError("invalid_resource_link")
                    text.append("[不支持的非文本内容：resource_link；未获取链接]")
                elif kind == "resource":
                    resource = block.get("resource")
                    if not isinstance(resource, dict) or not isinstance(
                        resource.get("uri"), str
                    ):
                        raise ValueError("invalid_resource")
                    if not isinstance(resource.get("text", resource.get("blob")), str):
                        raise ValueError("invalid_resource")
                    text.append("[不支持的非文本内容：resource；未获取来源]")
                else:
                    raise ValueError("unsupported_content_block")
            if not any(
                b.get("type") == "text" and b.get("text") for b in reply["content"]
            ):
                text.insert(0, "服务器已返回，但没有可展示的文本；不代表空查询。")
            content = tuple(TextBlock(self.redactor.redact_text(t)) for t in text)
            if reply.get("isError", False):
                return ToolFailure(
                    "execution_error",
                    content,
                    cost=UnknownCost(),
                    audit_data={
                        "structuredContent": reply.get("structuredContent"),
                        "schema_validated": False,
                    },
                )
            if "outputSchema" in tool:
                if "structuredContent" not in reply:
                    raise ValueError("structured_content_required")
                reason = self.validator.validate(
                    tool["outputSchema"],
                    context.deadline,
                    context.monotonic,
                    instance=reply["structuredContent"],
                )
                if reason:
                    raise ValueError(reason)
            return ToolSuccess(
                content,
                cost=UnknownCost(),
                audit_data={
                    "structuredContent": reply.get("structuredContent"),
                    "schema_validated": "outputSchema" in tool,
                },
            )
        except TransportError as exc:
            sent = bool(exc.sent)
            if not sent:
                return ToolFailure(
                    "execution_error",
                    (TextBlock(exc.reason),),
                    cost=NotSentCost("transport_not_sent"),
                )
        except ValueError, KeyError, TypeError:
            pass
        except BaseException:
            row.update(state="error", reason="control_interrupted")
            self.on_unavailable(row["server_key"], row["reason"])
            row["session"].close()
            raise
        row.update(state="error", reason="tool_result_unverified")
        self.on_unavailable(row["server_key"], row["reason"])
        if not row["session"].close():
            row["cleanup_incomplete"] = True
        return ToolFailure(
            "execution_error",
            (TextBlock("MCP 结果无法确认，可能已执行；后续调用已停止，未自动重试。"),),
            cost=UnknownCost(),
            stop_reason="tool_result_unverified",
        )

    def historical_catalog(self):
        rows = []
        for key, row in self.rows.items():
            if row["state"] == "connected":
                continue
            for item in row.get("historical_tools", []):
                rows.append(
                    {
                        "name": item["name"],
                        "original_name": item["name"],
                        "source_id": "mcp:" + str(row.get("historical_source")),
                        "capability_id": item.get("_identity"),
                        "server_key": key,
                        "availability": "historical_directory",
                        "exposure": "hidden",
                        "reason": row["reason"],
                        "description": item.get("description", ""),
                        "connection": row["state"],
                        "historical": True,
                        "effect": "external",
                    }
                )
        return self.redactor.redact_jsonable(rows)

    def affected_env(self, values):
        from agent_alfred.mcp.config import resolve

        affected = set()
        for key, definition in self.config.items():
            try:
                candidate = resolve(
                    key, definition.raw, values, self.directory, self.redactor
                )
                if candidate.env != definition.env:
                    affected.add(key)
            except ValueError, OSError, TypeError:
                affected.add(key)
        return affected

    def environment_checkpoint(self):
        return (
            dict(self.env),
            self.revision,
            {key: (row["state"], row["reason"]) for key, row in self.rows.items()},
        )

    def restore_environment(self, checkpoint):
        self.env, self.revision, states = checkpoint
        for key, (state, reason) in states.items():
            self.rows[key].update(state=state, reason=reason)

    def publish_env(self, values, affected):
        self.env = dict(values)
        for key in affected:
            if key in self.rows:
                self.rows[key].update(
                    state="configured_untested", reason="restart_required"
                )
        if affected:
            self.revision += 1

    def clean_affected(self, affected):
        for key in affected:
            row = self.rows.get(key)
            if row and row["session"] is not None and not row["session"].close():
                row["cleanup_incomplete"] = True

    def pause(self, reason):
        self.error = reason
        for row in self.rows.values():
            row.update(state="error", reason=reason)
            self.on_unavailable(row["server_key"], reason)

    def snapshot(self):
        rows = []
        for row in self.rows.values():
            session = row["session"]
            rows.append(
                {
                    k: row.get(k)
                    for k in (
                        "server_key",
                        "enabled",
                        "state",
                        "reason",
                        "history",
                        "observed_at",
                    )
                }
                | {
                    "total_tools": len(row["tools"]),
                    "available_tools": sum(
                        not t.get("_unavailable") for t in row["tools"]
                    )
                    if row["state"] == "connected"
                    else 0,
                    "directory_changed": bool(session and session.changed),
                    "cleanup_incomplete": bool(
                        row.get("cleanup_incomplete")
                        or row["reason"] == "cleanup_incomplete"
                    ),
                    "diagnostics": list(session.diagnostics) if session else [],
                    "env_references": list(row["definition"].references),
                }
            )
        return {
            "config_path": str(self.directory / "mcp.json") if self.directory else None,
            "config_fingerprint": self.config_fingerprint,
            "revision": self.revision,
            "error": self.error,
            "servers": self.redactor.redact_jsonable(rows),
        }

    def close(self, deadline=None):
        complete = self.validator.close(deadline)
        for row in self.rows.values():
            if row["session"] is not None and not row["session"].close(deadline):
                complete = False
        if self.resources is not None:
            for key, row in self.rows.items():
                if row["session"] is not None and row["session"].closed:
                    try:
                        self.resources.closed(key)
                    except OSError, ValueError:
                        complete = False
            if any(
                not self.resources.recovered(key)
                for key in list(self.resources.inherited)
            ):
                complete = False
        return complete
