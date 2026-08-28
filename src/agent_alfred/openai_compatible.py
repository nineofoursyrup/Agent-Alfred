"""OpenAI-compatible chat/completions Adapter. Wire decode only."""

from __future__ import annotations

import time
import uuid
from typing import Any

from agent_alfred.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptStarted,
    BlockDelta,
    BlockStarted,
    BlockStopped,
)
from agent_alfred.messages import TextBlock, message_plain_text
from agent_alfred.model import (
    AttemptRecord,
    ModelError,
    ModelRef,
    ModelRequest,
    ModelResponse,
    ModelResult,
    StopReason,
    Usage,
)


class OpenAICompatibleAdapter:
    """Wire encode/decode and attempt identity. No clock, retry, or fallback."""

    def __init__(self, *, client: Any, model: ModelRef, stream: bool = False):
        self._client = client
        self._model = model
        self._stream = stream

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
        started = time.monotonic()
        _emit(
            events,
            AttemptStarted(
                attempt_id=attempt_id, model=request.model, streamed=False
            ),
            attempt_id,
        )
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            error = _error_from_exc(attempt_id, exc)
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt_id,
                    partial=False,
                    error=error,
                    duration_ms=_duration_ms(started),
                ),
                attempt_id,
            )
            return _aborted(attempt_id, error, streamed=False)
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = _usage_from_sdk(getattr(response, "usage", None))
        blocks = (TextBlock(text),)
        stop = _stop_reason(getattr(choice, "finish_reason", None))
        duration = _duration_ms(started)
        _emit(
            events,
            AttemptCommitted(
                attempt_id=attempt_id,
                blocks=blocks,
                stop_reason=stop,
                usage=usage,
                duration_ms=duration,
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

    def _respond_stream(
        self,
        attempt_id: str,
        request: ModelRequest,
        kwargs: dict[str, Any],
        events: Any | None,
    ) -> ModelResult:
        started = time.monotonic()
        _emit(
            events,
            AttemptStarted(
                attempt_id=attempt_id, model=request.model, streamed=True
            ),
            attempt_id,
        )
        try:
            stream_resp = self._client.chat.completions.create(
                **kwargs, stream=True
            )
        except Exception as exc:
            error = _error_from_exc(attempt_id, exc)
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt_id,
                    partial=False,
                    error=error,
                    duration_ms=_duration_ms(started),
                ),
                attempt_id,
            )
            return _aborted(attempt_id, error, streamed=True)
        text_parts: list[str] = []
        finish: str | None = None
        usage = Usage()
        block_open = False
        try:
            for chunk in stream_resp:
                usage_obj = getattr(chunk, "usage", None)
                if usage_obj is not None:
                    usage = _usage_from_sdk(usage_obj)
                choices = getattr(chunk, "choices", None) or ()
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    if content:
                        if not block_open:
                            _emit(
                                events,
                                BlockStarted(
                                    attempt_id=attempt_id,
                                    index=0,
                                    block_type="text",
                                ),
                                attempt_id,
                            )
                            block_open = True
                        _emit(
                            events,
                            BlockDelta(
                                attempt_id=attempt_id, index=0, text=content
                            ),
                            attempt_id,
                        )
                        text_parts.append(content)
                reason = getattr(choice, "finish_reason", None)
                if reason:
                    finish = reason
        except Exception as exc:
            error = _error_from_exc(attempt_id, exc)
            blocks = _partial_blocks(text_parts)
            if block_open:
                _emit(
                    events,
                    BlockStopped(attempt_id=attempt_id, index=0),
                    attempt_id,
                )
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt_id,
                    partial=bool(text_parts),
                    blocks=blocks,
                    usage=usage,
                    error=error,
                    duration_ms=_duration_ms(started),
                ),
                attempt_id,
            )
            return _aborted(attempt_id, error, streamed=True, usage=usage)
        if finish is None:
            error = ModelError(
                retryable=True,
                status_code=None,
                body_excerpt="incomplete_stream",
                attempt_id=attempt_id,
                code="incomplete_stream",
            )
            blocks = _partial_blocks(text_parts)
            if block_open:
                _emit(
                    events,
                    BlockStopped(attempt_id=attempt_id, index=0),
                    attempt_id,
                )
            _emit(
                events,
                AttemptAborted(
                    attempt_id=attempt_id,
                    partial=True,
                    blocks=blocks,
                    usage=usage,
                    error=error,
                    duration_ms=_duration_ms(started),
                ),
                attempt_id,
            )
            return _aborted(attempt_id, error, streamed=True, usage=usage)
        if block_open:
            _emit(
                events, BlockStopped(attempt_id=attempt_id, index=0), attempt_id
            )
        blocks = (TextBlock("".join(text_parts)),)
        stop = _stop_reason(finish)
        duration = _duration_ms(started)
        _emit(
            events,
            AttemptCommitted(
                attempt_id=attempt_id,
                blocks=blocks,
                stop_reason=stop,
                usage=usage,
                duration_ms=duration,
            ),
            attempt_id,
        )
        return ModelResult(
            attempts=(
                AttemptRecord(
                    attempt_id=attempt_id,
                    streamed=True,
                    outcome="committed",
                    usage=usage,
                ),
            ),
            response=ModelResponse(
                blocks=blocks, stop_reason=stop, model=request.model
            ),
            final_error=None,
        )


def _emit(events: Any | None, payload: object, attempt_id: str) -> None:
    del attempt_id
    if events is None:
        return
    emit = getattr(events, "emit", None)
    if emit is None:
        return
    emit(payload)


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _partial_blocks(parts: list[str]) -> tuple[TextBlock, ...]:
    if not parts:
        return ()
    return (TextBlock("".join(parts)),)


def _error_from_exc(attempt_id: str, exc: BaseException) -> ModelError:
    return ModelError(
        retryable="unknown",
        status_code=getattr(exc, "status_code", None),
        body_excerpt=str(exc)[:500],
        attempt_id=attempt_id,
    )


def _aborted(
    attempt_id: str,
    error: ModelError,
    *,
    streamed: bool,
    usage: Usage | None = None,
) -> ModelResult:
    return ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id=attempt_id,
                streamed=streamed,
                outcome="aborted",
                usage=usage or Usage(),
                error=error,
            ),
        ),
        response=None,
        final_error=error,
    )


def _to_wire_messages(request: ModelRequest) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if request.system:
        messages.append(
            {
                "role": "system",
                "content": "\n\n".join(block.text for block in request.system),
            }
        )
    for message in request.messages:
        messages.append(
            {"role": message.role, "content": message_plain_text(message)}
        )
    return messages


def _usage_from_sdk(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    return Usage(
        total_input_tokens=prompt,
        output_tokens=completion,
        raw=usage.model_dump() if hasattr(usage, "model_dump") else {},
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
