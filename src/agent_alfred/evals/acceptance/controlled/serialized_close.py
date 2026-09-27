"""Serialize retryable close effects without a Python claim-release step."""

from agent_alfred.resource_rollback import OwnedResource, reraise_failure


class _CloseAttempt:
    def __init__(self, owner):
        self.successor = {}
        self.execution = self._run(owner)

    def _run(self, owner):
        # CPython clears gi_running on yield or exceptional unwind, including
        # interruption of the close action's own exception/finally handlers.
        try:
            yield owner.close()
        except BaseException as failure:
            reraise_failure(failure)


class SerializedClose:
    """Retain close progress and reject concurrent execution of the same leaf.

    A native generator execution is the claim. Inactive unsuccessful attempts
    have one atomically published successor, so no interruptible flag clearing
    or replacement of a newer owner's claim is required. OwnedResource records
    successful returns; an optional observer covers a published effect whose
    Python return was interrupted. Such an observer never advances cleanup.
    """

    def __init__(self, action, *, completed=None):
        self._owner = OwnedResource(close=lambda operation: operation())
        self._owner.publish(action)
        self._completed = completed
        self._first = _CloseAttempt(self._owner)

    def _latest(self):
        attempt = self._first
        while "retry" in attempt.successor:
            attempt = attempt.successor["retry"]
        return attempt

    def _effect_completed(self):
        return self._owner.close_completed() or (
            self._completed is not None and self._completed()
        )

    def close_completed(self):
        while True:
            attempt = self._latest()
            if attempt.execution.gi_running:
                return False
            complete = self._effect_completed()
            if attempt is self._latest():
                return not attempt.execution.gi_running and complete

    def close(self):
        attempt = self._latest()
        while True:
            while "retry" in attempt.successor:
                attempt = attempt.successor["retry"]
            execution = attempt.execution
            if execution.gi_running:
                return False
            if self.close_completed():
                return True
            if execution.gi_frame is None or execution.gi_suspended:
                attempt = attempt.successor.setdefault(
                    "retry", _CloseAttempt(self._owner)
                )
                continue
            try:
                return next(execution)
            except StopIteration:
                # A competing caller advanced this attempt after our check.
                continue
            except ValueError as failure:
                if failure.__traceback__.tb_next is None:
                    # Native generator reentry; an action's ValueError has a
                    # generator frame and must remain a real cleanup failure.
                    return False
                reraise_failure(failure)
            except BaseException as failure:
                reraise_failure(failure)
