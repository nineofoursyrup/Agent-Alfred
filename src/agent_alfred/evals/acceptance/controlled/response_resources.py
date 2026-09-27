"""Trusted response ownership spanning third-party return-to-caller storage."""

from contextlib import contextmanager
from functools import partial

from agent_alfred.resource_rollback import (
    CloseCompletion,
    ResumableRollback,
    capture_call_result,
)

from .serialized_close import SerializedClose


class _ResponseOwner:
    def __init__(self, close):
        self.response = None
        self._release = close
        self._complete = False
        self._active = True
        self._closing = SerializedClose(
            self._close_response, completed=self._response_completed
        )

    def finish(self):
        self._active = False

    def _close_response(self):
        if self.response is None or self._complete:
            return
        capture_call_result(self, "_result", partial(self._release, self.response))
        if self._result is False:
            return False
        self._complete = True

    def _response_completed(self):
        return self._complete or (
            hasattr(self, "_result") and self._result is not False
        )

    def close(self):
        if self._active:
            return False
        return self._closing.close()

    def close_completed(self):
        return not self._active and self._closing.close_completed()


def close_body(response, *, field):
    body = response.get(field)
    if body is not None:
        if isinstance(body, CloseCompletion) and body.close_completed():
            return
        return body.close()


@contextmanager
def owned_response(cleanup, operation, *, close):
    owner = ResumableRollback()
    cleanup.begin(owner)
    held = _ResponseOwner(close)
    owner.own(held)
    try:
        try:
            # A concurrent cleanup cannot retire the holder while the SDK or
            # consumer still owns its in-flight result/body.
            capture_call_result(held, "response", operation)
            yield held.response
        finally:
            held.finish()
    except BaseException as failure:
        held.finish()
        owner.raise_failure(failure)
    owner.close()
    cleanup.complete(owner)
