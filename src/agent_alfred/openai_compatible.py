"""OpenAI-compatible chat/completions Adapter. Wire decode only."""

from __future__ import annotations

import json
import uuid
from copy import copy
from decimal import Decimal
from typing import Any

from agent_alfred.adapter_common import (
    ResponseDecodeError,
    _aborted,
    _emit,
    _error_from_exc,
    _field,
    _stream_error_from_exc,
    _timeout_ms,
    close_stream,
    decoded_tool_call,
    raw_usage,
)
from agent_alfred.attempt_io import (
    attempt_scope,
    deadline_chunks,
    prepare_attempt,
    preserve_attempt_usage,
    propagate_local_failure,
    start_transport,
)
from agent_alfred.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptStarted,
    BlockDelta,
    BlockStarted,
    BlockStopped,
    RawToolArgumentFragment,
)
from agent_alfred.messages import (
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent_alfred.model import (
    AttemptRecord,
    ModelRef,
    ModelRequest,
    ModelResponse,
    ModelResult,
    NamedToolChoice,
    StopReason,
    Usage,
    tool_schema_jsonable,
)


class OpenAICompatibleAdapter:
    """Wire encode/decode and attempt identity. No clock, retry, or fallback."""

    def __init__(
        self,
        *,
        client: Any,
        model: ModelRef,
        stream: bool = False,
        attempt_timeout_s: float | None = None,
        request_headers=None,
    ):
        self._client = client
        self._model = model
        self._stream = stream
        self._attempt_timeout_s = attempt_timeout_s
        self._request_headers = request_headers

    def with_attempt_timeout(self, timeout_s: float) -> OpenAICompatibleAdapter:
        """Return a wire-equivalent Adapter with a policy-computed timeout."""
        bound = copy(self)
        bound._attempt_timeout_s = timeout_s
        return bound

    def with_attempt_budget(self, budget):
        bound = copy(self)
        bound._io_budget = budget
        return bound

    @attempt_scope
    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
    ) -> ModelResult:
        del deadline
        attempt_id = uuid.uuid4().hex
        payload = _to_wire_messages(request)
        kwargs: dict[str, Any] = {
            "model": request.model.model_id,
            "messages": payload,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool_schema_jsonable(tool.input_schema),
                    },
                }
                for tool in request.tools
            ]
            choice = request.tool_choice
            kwargs["tool_choice"] = (
                {"type": "function", "function": {"name": choice.name}}
                if isinstance(choice, NamedToolChoice)
                else choice
            )
        if self._attempt_timeout_s is not None:
            kwargs["timeout"] = self._attempt_timeout_s
        if self._request_headers is not None:
            kwargs["extra_headers"] = self._request_headers(request)
        if self._stream:
            return self._respond_stream(attempt_id, request, kwargs, events)
        return self._respond_once(attempt_id, request, kwargs, events)

    def _respond_once(
        self,
        attempt_id: str,
        request: ModelRequest,
        kwargs: dict[str, Any],
        events: Any | None,
    ) -> ModelResult:
        prepare_attempt(
            lambda: _emit(events, AttemptStarted(
                attempt_id=attempt_id,
                model=request.model,
                streamed=False,
                timeout_ms=_timeout_ms(self._attempt_timeout_s),
            ), attempt_id),
            attempt_id,
        )
        try:
            start_transport()
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            propagate_local_failure()
            error = _error_from_exc(attempt_id, exc)
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt_id,
                    partial=False,
                    error=error,
                    duration_ms=0,
                ),
                attempt_id,
            )
            return _aborted(attempt_id, error, streamed=False)
        usage = Usage()
        try:
            usage = _usage_from_sdk(_field(response, "usage"))
            preserve_attempt_usage(usage)
            choice = _field(response, "choices")[0]
            blocks = _decode_message(_field(choice, "message"))
            stop = _stop_reason(_field(choice, "finish_reason"))
        except Exception as exc:
            propagate_local_failure()
            error = _error_from_exc(
                attempt_id, exc, retryable=False, code="invalid_response"
            )
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt_id,
                    partial=False,
                    usage=usage,
                    error=error,
                    duration_ms=0,
                ),
                attempt_id,
            )
            return _aborted(attempt_id, error, streamed=False, usage=usage)
        _emit(
            events,
            AttemptCommitted(
                attempt_id=attempt_id,
                blocks=blocks,
                stop_reason=stop,
                usage=usage,
                duration_ms=0,
            ),
            attempt_id,
        )
        return ModelResult(
            attempts=(
                AttemptRecord(
                    attempt_id=attempt_id,
                    streamed=False,
                    outcome="committed",
                    usage=usage,
                ),
            ),
            response=ModelResponse(
                blocks=blocks, stop_reason=stop, model=request.model
            ),
            final_error=None,
        )

    def _respond_stream(self, attempt_id, request, kwargs, events):
        prepare_attempt(
            lambda: _emit(events, AttemptStarted(
                attempt_id=attempt_id,
                model=request.model,
                streamed=True,
                timeout_ms=_timeout_ms(self._attempt_timeout_s),
            ), attempt_id),
            attempt_id,
        )
        usage = Usage()
        finish = None
        text_parts = []
        thoughts = []
        calls = {}
        # Tool-call indices are a separate wire namespace. Allocate public
        # content-block slots in first-observed order and reuse them at commit.
        opened = {}
        terminal_seen = False
        response = None

        def delta(key, kind, text):
            if key not in opened:
                index = len(opened)
                opened[key] = index
                _emit(
                    events,
                    BlockStarted(attempt_id=attempt_id, index=index, block_type=kind),
                    attempt_id,
                )
            index = opened[key]
            if text:
                _emit(
                    events,
                    BlockDelta(attempt_id=attempt_id, index=index, text=text),
                    attempt_id,
                )

        error = None
        try:
            start_transport()
            response = self._client.chat.completions.create(
                **kwargs, stream=True, stream_options={"include_usage": True}
            )
            for chunk in deadline_chunks(response):
                if chunk == "[DONE]":
                    terminal_seen = True
                    break
                value = _field(chunk, "usage")
                if value is not None:
                    usage = _usage_from_sdk(value)
                    preserve_attempt_usage(usage)
                choices = _field(chunk, "choices") or ()
                if not choices:
                    continue
                choice = choices[0]
                piece = _field(choice, "delta")
                text = _field(piece, "content")
                if text:
                    text_parts.append(text)
                    delta(("text", None), "text", text)
                thought = _field(piece, "reasoning_content")
                if thought:
                    thoughts.append(thought)
                    delta(("thinking", None), "thinking", thought)
                for call in _field(piece, "tool_calls", ()) or ():
                    index = _field(call, "index")
                    if type(index) is not int or index < 0:
                        raise ResponseDecodeError("invalid tool index")
                    saved = calls.setdefault(index, {"id": "", "name": "", "raw": ""})
                    saved["id"] += _field(call, "id") or ""
                    fn = _field(call, "function")
                    saved["name"] += _field(fn, "name") or ""
                    fragment = _field(fn, "arguments") or ""
                    saved["raw"] += fragment
                    delta(("tool_use", index), "tool_use", fragment)
                reason = _field(choice, "finish_reason")
                if reason is not None:
                    finish = reason
            if finish is None or not (
                terminal_seen or getattr(response, "completed", False) is True
            ):
                error = _error_from_exc(
                    attempt_id,
                    RuntimeError("incomplete_stream"),
                    retryable=True,
                    code="incomplete_stream",
                )
            decoded = {}
            if thoughts:
                decoded[("thinking", None)] = ThinkingBlock("".join(thoughts))
            if text_parts:
                decoded[("text", None)] = TextBlock("".join(text_parts))
            if error is None:
                for index, saved in calls.items():
                    decoded[("tool_use", index)] = decoded_tool_call(
                        saved["id"], saved["name"], json.loads(saved["raw"])
                    )
            blocks = [decoded[key] for key in opened if key in decoded]
        except Exception as exc:
            propagate_local_failure()
            error = _stream_error_from_exc(attempt_id, exc)
        finally:
            error = close_stream(response, attempt_id, error)
        for index in opened.values():
            _emit(events, BlockStopped(attempt_id=attempt_id, index=index), attempt_id)
        if error is not None:
            # No partially decoded tool can become executable on an aborted Attempt.
            partial = tuple(
                TextBlock("".join(text_parts))
                if kind == "text"
                else ThinkingBlock("".join(thoughts))
                for kind, _ in opened
                if kind != "tool_use"
            )
            fragments = tuple(
                RawToolArgumentFragment(
                    calls[i]["id"] or None, calls[i]["name"] or None, calls[i]["raw"]
                )
                for i in sorted(calls)
            )
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt_id,
                    partial=True,
                    blocks=partial,
                    unparsed_tool_arguments=fragments,
                    usage=usage,
                    error=error,
                ),
                attempt_id,
            )
            return _aborted(attempt_id, error, streamed=True, usage=usage)
        final = tuple(blocks) if blocks else (TextBlock(""),)
        stop = _stop_reason(finish)
        _emit(
            events,
            AttemptCommitted(
                attempt_id=attempt_id, blocks=final, stop_reason=stop, usage=usage
            ),
            attempt_id,
        )
        return ModelResult(
            (AttemptRecord(attempt_id, True, "committed", usage),),
            ModelResponse(final, stop, request.model),
            None,
        )


def _decode_message(message: Any) -> tuple:
    blocks = []
    thinking = _field(message, "reasoning_content")
    if thinking:
        blocks.append(ThinkingBlock(thinking))
    text = _field(message, "content")
    if text:
        blocks.append(TextBlock(text))
    for call in _field(message, "tool_calls", ()) or ():
        fn = _field(call, "function")
        arguments = json.loads(_field(fn, "arguments"))
        if not isinstance(arguments, dict):
            raise ResponseDecodeError("tool arguments must be an object")
        blocks.append(
            decoded_tool_call(_field(call, "id"), _field(fn, "name"), arguments)
        )
    return tuple(blocks) if blocks else (TextBlock(""),)


def _to_wire_messages(request: ModelRequest) -> list[dict]:
    messages = []
    if request.system:
        messages.append(
            {"role": "system", "content": "\n\n".join(b.text for b in request.system)}
        )
    for message in request.messages:
        texts = []
        calls = []
        reasoning = []
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                content = "\n".join(b.text for b in block.content)
                if block.is_error:
                    content = '{"ok":false}\n' + content
                messages.append(
                    {"role": "tool", "tool_call_id": block.call_id, "content": content}
                )
            elif isinstance(block, ToolCallBlock):
                calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(dict(block.input)),
                        },
                    }
                )
            elif isinstance(block, ThinkingBlock):
                reasoning.append(block.text)
            else:
                texts.append(block.text)
        if texts or calls or reasoning or not message.blocks:
            row = {"role": message.role, "content": "\n".join(texts) or None}
            if calls:
                row["tool_calls"] = calls
            if reasoning:
                row["reasoning_content"] = "\n".join(reasoning)
            messages.append(row)
    return messages


def _usage_from_sdk(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    raw = raw_usage(usage)
    prompt = _field(usage, "prompt_tokens")
    details = _field(usage, "prompt_tokens_details")
    hit = _field(usage, "prompt_cache_hit_tokens")
    miss = _field(usage, "prompt_cache_miss_tokens")
    if hit is None:
        hit = _field(details, "cached_tokens")
    if miss is None and prompt is not None and hit is not None:
        miss = prompt - hit
    ticks = _field(usage, "cost_in_usd_ticks")
    return Usage(
        total_input_tokens=prompt,
        uncached_input_tokens=miss,
        cache_read_tokens=hit,
        cache_write_tokens=_field(details, "cache_write_tokens"),
        output_tokens=_field(usage, "completion_tokens"),
        reasoning_tokens=_field(
            _field(usage, "completion_tokens_details"), "reasoning_tokens"
        ),
        endpoint_reported_cost_usd=(
            Decimal(str(ticks)) / Decimal(10**10) if ticks is not None else None
        ),
        raw=raw,
    )


def _stop_reason(raw: str | None) -> StopReason:
    mapping: dict[str, StopReason] = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "content_filter",
    }
    if raw is None:
        return "unknown"
    return mapping.get(raw, "unknown")
