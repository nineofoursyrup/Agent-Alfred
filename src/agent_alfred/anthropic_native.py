"""Native messages codec; synchronous and independent of retry/time policy."""

import json
import uuid
from copy import copy

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
    ModelResponse,
    ModelResult,
    NamedToolChoice,
    Usage,
)


class AnthropicAdapter:
    def __init__(self, *, client, model, stream=False, attempt_timeout_s=None):
        self._client = client
        self._model = model
        self._stream = stream
        self._attempt_timeout_s = attempt_timeout_s

    def with_attempt_timeout(self, timeout_s):
        bound = copy(self)
        bound._attempt_timeout_s = timeout_s
        return bound

    def respond(self, request, *, events=None, deadline=None):
        del deadline
        attempt = uuid.uuid4().hex
        kwargs = {
            "model": request.model.model_id,
            "messages": [
                {"role": m.role, "content": [_encode(b) for b in m.blocks]}
                for m in request.messages
            ],
            # messages requires an explicit output limit even when settings omit it.
            "max_tokens": request.max_tokens
            if request.max_tokens is not None
            else 4096,
        }
        if request.system is not None:
            kwargs["system"] = [_encode(b) for b in request.system]
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": dict(t.input_schema),
                }
                for t in request.tools
            ]
            choice = request.tool_choice
            kwargs["tool_choice"] = (
                {"type": "tool", "name": choice.name}
                if isinstance(choice, NamedToolChoice)
                else {"type": {"required": "any"}.get(choice, choice)}
            )
        if self._attempt_timeout_s is not None:
            kwargs["timeout"] = self._attempt_timeout_s
        if self._stream:
            return self._respond_stream(request, kwargs, events, attempt)
        _emit(
            events,
            AttemptStarted(
                attempt_id=attempt,
                model=request.model,
                timeout_ms=_timeout_ms(self._attempt_timeout_s),
            ),
            attempt,
        )
        usage = Usage()
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as exc:
            error = _error_from_exc(attempt, exc)
        else:
            try:
                usage = _usage(_field(response, "usage"))
                blocks = tuple(_decode(b) for b in _field(response, "content"))
                stop = _stop(_field(response, "stop_reason"))
            except Exception as exc:
                error = _error_from_exc(
                    attempt, exc, retryable=False, code="invalid_response"
                )
            else:
                _emit(
                    events,
                    AttemptCommitted(
                        attempt_id=attempt, blocks=blocks, stop_reason=stop, usage=usage
                    ),
                    attempt,
                )
                return ModelResult(
                    (AttemptRecord(attempt, False, "committed", usage),),
                    ModelResponse(blocks, stop, request.model),
                    None,
                )
        _emit(
            events,
            AttemptAborted(attempt_id=attempt, error=error, usage=usage),
            attempt,
        )
        return _aborted(attempt, error, streamed=False, usage=usage)

    def _respond_stream(self, request, kwargs, events, attempt):
        _emit(
            events,
            AttemptStarted(
                attempt_id=attempt,
                model=request.model,
                streamed=True,
                timeout_ms=_timeout_ms(self._attempt_timeout_s),
            ),
            attempt,
        )
        state = {}
        raw = {}
        started = stopped = False
        finish = None
        response = None
        error = None
        try:
            response = self._client.messages.create(**kwargs, stream=True)
            for event in response:
                kind = _field(event, "type")
                if kind == "message_start":
                    if started:
                        raise ResponseDecodeError("duplicate message start")
                    started = True
                    raw.update(raw_usage(_field(_field(event, "message"), "usage")))
                elif kind == "content_block_start":
                    index = _field(event, "index")
                    if (
                        not started
                        or type(index) is not int
                        or index < 0
                        or index in state
                    ):
                        raise ResponseDecodeError("invalid content index")
                    block = _field(event, "content_block")
                    block_type = _field(block, "type")
                    if block_type not in ("text", "thinking", "tool_use"):
                        raise ResponseDecodeError("unsupported content block")
                    state[index] = {
                        "type": block_type,
                        "closed": False,
                        "text": _field(block, "text")
                        or _field(block, "thinking")
                        or "",
                        "signature": _field(block, "signature") or "",
                        "id": _field(block, "id"),
                        "name": _field(block, "name"),
                        "input": _field(block, "input"),
                        "raw": "",
                    }
                    _emit(
                        events,
                        BlockStarted(
                            attempt_id=attempt, index=index, block_type=block_type
                        ),
                        attempt,
                    )
                    if state[index]["text"]:
                        _emit(
                            events,
                            BlockDelta(
                                attempt_id=attempt,
                                index=index,
                                text=state[index]["text"],
                            ),
                            attempt,
                        )
                elif kind == "content_block_delta":
                    index = _field(event, "index")
                    current = state[index]
                    if current["closed"]:
                        raise ResponseDecodeError("delta after block stop")
                    delta = _field(event, "delta")
                    delta_type = _field(delta, "type")
                    field, target, expected = {
                        "text_delta": ("text", "text", "text"),
                        "thinking_delta": ("thinking", "text", "thinking"),
                        "signature_delta": ("signature", "signature", "thinking"),
                        "input_json_delta": ("partial_json", "raw", "tool_use"),
                    }[delta_type]
                    if current["type"] != expected:
                        raise ResponseDecodeError("delta type mismatch")
                    piece = _field(delta, field)
                    current[target] += piece
                    if target != "signature":
                        _emit(
                            events,
                            BlockDelta(attempt_id=attempt, index=index, text=piece),
                            attempt,
                        )
                elif kind == "content_block_stop":
                    index = _field(event, "index")
                    if state[index]["closed"]:
                        raise ResponseDecodeError("duplicate block stop")
                    state[index]["closed"] = True
                    _emit(
                        events, BlockStopped(attempt_id=attempt, index=index), attempt
                    )
                elif kind == "message_delta":
                    raw.update(
                        {
                            k: v
                            for k, v in raw_usage(_field(event, "usage")).items()
                            if v is not None
                        }
                    )
                    reason = _field(_field(event, "delta"), "stop_reason")
                    if reason is not None:
                        finish = reason
                elif kind == "message_stop":
                    stopped = True
                    break
                elif kind == "error":
                    failure = _field(event, "error")
                    code = _field(failure, "type")
                    error = _error_from_exc(
                        attempt,
                        RuntimeError(_field(failure, "message") or "stream error"),
                        retryable=(
                            True
                            if code in ("overloaded_error", "rate_limit_error")
                            else "unknown"
                        ),
                        code=code,
                    )
                    break
            if error is None and (
                not started
                or not stopped
                or finish is None
                or any(not b["closed"] for b in state.values())
            ):
                error = _error_from_exc(
                    attempt,
                    RuntimeError("incomplete_stream"),
                    retryable=True,
                    code="incomplete_stream",
                )
            usage = _usage(raw)
            blocks = []
            if error is None:
                for index in sorted(state):
                    block = state[index]
                    if block["type"] == "text":
                        blocks.append(TextBlock(block["text"]))
                    elif block["type"] == "thinking":
                        blocks.append(
                            ThinkingBlock(block["text"], block["signature"] or None)
                        )
                    else:
                        args = (
                            json.loads(block["raw"]) if block["raw"] else block["input"]
                        )
                        if (
                            not isinstance(args, dict)
                            or not block["id"]
                            or not block["name"]
                        ):
                            raise ResponseDecodeError("invalid tool call")
                        blocks.append(
                            decoded_tool_call(block["id"], block["name"], args)
                        )
        except Exception as exc:
            error = _stream_error_from_exc(attempt, exc)
            usage = _usage(raw)
        finally:
            error = close_stream(response, attempt, error)
        if error is not None:
            partial = []
            fragments = []
            for index in sorted(state):
                block = state[index]
                if block["type"] == "tool_use":
                    fragments.append(
                        RawToolArgumentFragment(
                            block["id"], block["name"], block["raw"]
                        )
                    )
                elif block["closed"]:
                    partial.append(
                        TextBlock(block["text"])
                        if block["type"] == "text"
                        else ThinkingBlock(block["text"], block["signature"] or None)
                    )
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt,
                    partial=True,
                    blocks=tuple(partial),
                    unparsed_tool_arguments=tuple(fragments),
                    error=error,
                    usage=usage,
                ),
                attempt,
            )
            return _aborted(attempt, error, streamed=True, usage=usage)
        stop = _stop(finish)
        _emit(
            events,
            AttemptCommitted(
                attempt_id=attempt, blocks=tuple(blocks), usage=usage, stop_reason=stop
            ),
            attempt,
        )
        return ModelResult(
            (AttemptRecord(attempt, True, "committed", usage),),
            ModelResponse(tuple(blocks), stop, request.model),
            None,
        )


def _encode(block):
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {
            "type": "thinking",
            "thinking": block.text,
            "signature": block.signature,
        }
    if isinstance(block, ToolCallBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input),
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.call_id,
            "content": [_encode(b) for b in block.content],
            "is_error": block.is_error,
        }
    raise ResponseDecodeError("unknown message block")


def _decode(block):
    kind = _field(block, "type")
    if kind == "text":
        return TextBlock(_field(block, "text"))
    if kind == "thinking":
        return ThinkingBlock(_field(block, "thinking"), _field(block, "signature"))
    if kind == "tool_use":
        arguments = _field(block, "input")
        if not isinstance(arguments, dict):
            raise ResponseDecodeError("tool input must be an object")
        return decoded_tool_call(_field(block, "id"), _field(block, "name"), arguments)
    raise ResponseDecodeError("unsupported response block")


def _stop(raw):
    mapping = {
        "end_turn": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "max_tokens",
        "stop_sequence": "stop_sequence",
        "refusal": "refusal",
        "pause_turn": "paused",
        "model_context_window_exceeded": "context_exceeded",
    }
    return mapping.get(raw, "unknown")


def _usage(value):
    raw = raw_usage(value)
    input_tokens = _field(value, "input_tokens")
    read = _field(value, "cache_read_input_tokens")
    write = _field(value, "cache_creation_input_tokens")
    complete = all(v is not None for v in (input_tokens, read, write))
    return Usage(
        total_input_tokens=input_tokens + read + write if complete else None,
        uncached_input_tokens=input_tokens,
        cache_read_tokens=read,
        cache_write_tokens=write,
        output_tokens=_field(value, "output_tokens"),
        raw=raw,
    )
