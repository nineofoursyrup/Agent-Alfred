"""OpenAI-compatible chat/completions Adapter. Wire decode only."""

from __future__ import annotations

import uuid
from typing import Any

from agent_alfred.clock import Clock
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
    def __init__(self, *, client: Any, model: ModelRef, clock: Clock):
        self._client = client
        self._model = model
        self._clock = clock

    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
        stream: bool = False,
    ) -> ModelResult:
        del events
        attempt_id = uuid.uuid4().hex
        payload = _to_wire_messages(request)
        timeout = None
        if deadline is not None:
            timeout = max(0.01, deadline - self._clock.monotonic())
        kwargs: dict[str, Any] = {
            "model": request.model.model_id,
            "messages": payload,
            "max_tokens": request.max_tokens,
            "timeout": timeout,
        }
        if stream:
            return self._respond_stream(attempt_id, request, kwargs)
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            return _aborted_from_exc(attempt_id, exc, streamed=False)
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = _usage_from_sdk(getattr(response, "usage", None))
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
                blocks=(TextBlock(text),),
                stop_reason=_stop_reason(getattr(choice, "finish_reason", None)),
                model=request.model,
            ),
            final_error=None,
        )

    def _respond_stream(
        self,
        attempt_id: str,
        request: ModelRequest,
        kwargs: dict[str, Any],
    ) -> ModelResult:
        try:
            stream_resp = self._client.chat.completions.create(
                **kwargs, stream=True
            )
        except Exception as exc:
            return _aborted_from_exc(attempt_id, exc, streamed=True)
        text_parts: list[str] = []
        finish: str | None = None
        usage = Usage()
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
                        text_parts.append(content)
                reason = getattr(choice, "finish_reason", None)
                if reason:
                    finish = reason
        except Exception as exc:
            error = ModelError(
                retryable="unknown",
                status_code=getattr(exc, "status_code", None),
                body_excerpt=str(exc)[:500],
                attempt_id=attempt_id,
                code="incomplete_stream",
            )
            return ModelResult(
                attempts=(
                    AttemptRecord(
                        attempt_id=attempt_id,
                        streamed=True,
                        outcome="aborted",
                        usage=usage,
                        error=error,
                    ),
                ),
                response=None,
                final_error=error,
            )
        if finish is None:
            error = ModelError(
                retryable=True,
                status_code=None,
                body_excerpt="incomplete_stream",
                attempt_id=attempt_id,
                code="incomplete_stream",
            )
            return ModelResult(
                attempts=(
                    AttemptRecord(
                        attempt_id=attempt_id,
                        streamed=True,
                        outcome="aborted",
                        usage=usage,
                        error=error,
                    ),
                ),
                response=None,
                final_error=error,
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
                blocks=(TextBlock("".join(text_parts)),),
                stop_reason=_stop_reason(finish),
                model=request.model,
            ),
            final_error=None,
        )


class UnconfiguredClient:
    """Fails without a network round-trip when the endpoint has no key."""

    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
        stream: bool = False,
    ) -> ModelResult:
        del request, events, deadline, stream
        attempt_id = "unconfigured"
        error = ModelError(
            retryable=False,
            status_code=None,
            body_excerpt="endpoint_unconfigured",
            attempt_id=attempt_id,
            code="endpoint_unconfigured",
        )
        return ModelResult(
            attempts=(
                AttemptRecord(
                    attempt_id=attempt_id,
                    streamed=False,
                    outcome="aborted",
                    usage=Usage(),
                    error=error,
                ),
            ),
            response=None,
            final_error=error,
        )


def _aborted_from_exc(
    attempt_id: str, exc: BaseException, *, streamed: bool
) -> ModelResult:
    error = ModelError(
        retryable="unknown",
        status_code=getattr(exc, "status_code", None),
        body_excerpt=str(exc)[:500],
        attempt_id=attempt_id,
    )
    return ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id=attempt_id,
                streamed=streamed,
                outcome="aborted",
                usage=Usage(),
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
