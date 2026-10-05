"""How a failure inside one model's analysis is reported.

Production absorbs it as a coverage miss so one model's exotic SQL cannot blank a whole
run. The test suite re-raises instead, so a detector crash cannot hide behind a "no
finding" assertion; the isolation tests opt back into absorbing.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_reraise: ContextVar[bool] = ContextVar("reraise_model_errors", default=False)


def error_reason(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


@contextmanager
def reraising_model_errors(reraise: bool) -> Generator[None, None, None]:
    token = _reraise.set(reraise)
    try:
        yield
    finally:
        _reraise.reset(token)


def reason_or_reraise(error: Exception) -> str:
    """The coverage-miss reason for ``error``, or ``error`` itself raised when the suite
    asked for that. Call from an ``except Exception`` block."""
    if _reraise.get():
        raise error
    return f"analysis error: {error_reason(error)}"
