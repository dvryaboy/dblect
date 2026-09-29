"""How a failure inside one model's analysis is handled.

A run over a whole project should not lose every model to one model's exotic SQL, so
production skips the failing model and reports it as a coverage miss. The same absorption
would hide dblect's own bugs from the test suite (a "no finding" assertion passes when the
detector crashed), so the suite runs under ``RAISE`` and only the isolation tests opt into
``SKIP``.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum, auto


class ModelErrorPolicy(Enum):
    SKIP = auto()
    RAISE = auto()


_policy = ModelErrorPolicy.SKIP


@contextmanager
def model_error_policy(policy: ModelErrorPolicy) -> Generator[None, None, None]:
    """Run the enclosed analysis under ``policy``, restoring the previous one after."""
    global _policy
    previous, _policy = _policy, policy
    try:
        yield
    finally:
        _policy = previous


def coverage_miss_reason(error: Exception) -> str:
    """The reason to report for a model whose analysis raised ``error``, or re-raise it
    under ``RAISE``. Called from an ``except Exception`` block, so ``KeyboardInterrupt`` and
    ``SystemExit`` are never absorbed. The type and message stay in the reason so a
    genuine dblect bug is visible in the report."""
    if _policy is ModelErrorPolicy.RAISE:
        raise error
    return f"analysis error: {type(error).__name__}: {error}"
