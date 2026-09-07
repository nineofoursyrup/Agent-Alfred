"""Shared wire boundary helpers; no clock or retry policy."""

from typing import Any

from agent_alfred.model import AttemptRecord, ModelError, ModelResult, Retryable, Usage


def _emit(events: Any | None, payload: object, attempt_id: str) -> None:
    del attempt_id
    if events is None:
        return
    emit = getattr(events, "emit", None)
    if emit is None:
        return
    emit(payload)


def _timeout_ms(timeout_s: float | None) -> int | None:
    if timeout_s is None:
        return None
    return max(0, int(timeout_s * 1000))


def _error_from_exc(
    attempt_id: str,
    exc: BaseException,
    *,
    retryable: Retryable | None = None,
    code: str | None = None,
) -> ModelError:
    return ModelError(
        retryable=(
            _retryable_from_status(getattr(exc, "status_code", None))
            if retryable is None
            else retryable
        ),
        status_code=getattr(exc, "status_code", None),
        body_excerpt=str(exc)[:500],
        attempt_id=attempt_id,
        code=code,
    )


def _stream_error_from_exc(attempt_id: str, exc: BaseException) -> ModelError:
    if isinstance(
        exc,
        (
            AttributeError,
            IndexError,
            KeyError,
            ResponseDecodeError,
            TypeError,
            ValueError,
        ),
    ):
        return _error_from_exc(
            attempt_id, exc, retryable=False, code="invalid_response"
        )
    if getattr(exc, "status_code", None) is None:
        return _error_from_exc(
            attempt_id, exc, retryable=True, code="incomplete_stream"
        )
    return _error_from_exc(attempt_id, exc)


def _retryable_from_status(status_code: int | None) -> Retryable:
    if status_code is None:
        return "unknown"
    if status_code in (408, 425, 429) or status_code >= 500:
        return True
    if 400 <= status_code < 500:
        return False
    return "unknown"


class ResponseDecodeError(Exception):
    """The network attempt completed but its response shape was invalid."""


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


def _field(value: Any, name: str, default: Any = None) -> Any:
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def raw_usage(value):
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    return {k: v for k, v in vars(value).items() if not callable(v)}


def decoded_tool_call(call_id, name, arguments):
    from agent_alfred.messages import ToolCallBlock

    if (
        not isinstance(call_id, str)
        or not call_id
        or not isinstance(name, str)
        or not name
        or not isinstance(arguments, dict)
    ):
        raise ResponseDecodeError("invalid tool call")
    return ToolCallBlock(call_id, name, arguments)


def close_stream(
    response, attempt_id: str, error: ModelError | None
) -> ModelError | None:
    """A cleanup failure cannot erase the consumed Attempt's usage or first error."""
    try:
        close = getattr(response, "close", None)
        if close is not None:
            close()
    except Exception as exc:
        if error is None:
            return _error_from_exc(
                attempt_id, exc, retryable=False, code="stream_close_failed"
            )
    return error
