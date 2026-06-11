"""``@contract``: mark a method whose body builds a symbolic expression.

The marker is intentionally tiny. It tags the function so the model contract's
metaclass finds it during the scan, then :func:`capture` runs the body once with
a :class:`~dblect.contracts.proxy.ContractSelf` standing in for ``self`` and
records the AST it returns. Dispatch on that AST decides what the contract does:
a :data:`~dblect.contracts.ast.FactNode` feeds the substrate (read and trusted
unless contradicted), a :data:`~dblect.contracts.ast.Pred` is checked by running.
See ``docs/design/dsl-reference.md`` (Contracts).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dblect.contracts.ast import ContractResult, FactNode, Pred
from dblect.contracts.proxy import (
    ContractError,
    ContractSelf,
    FactProxy,
    PredicateProxy,
)

_MARKER = "_dblect_contract"

# A contract body takes a self-proxy and returns a fact or predicate proxy. The
# return is typed ``object`` because authors annotate it loosely (or not at all)
# and the materialized escape hatch returns other shapes; :func:`capture` checks
# the actual proxy kind at runtime.
ContractMethod = Callable[[ContractSelf], object]


def contract(method: ContractMethod) -> ContractMethod:
    """Mark a contract method. The body returns a fact or a predicate built from
    column proxies; the framework reads the fact or runs the predicate."""
    setattr(method, _MARKER, True)
    return method


def is_contract(obj: object) -> bool:
    """Whether ``obj`` is a function marked by :func:`contract`."""
    return callable(obj) and getattr(obj, _MARKER, False) is True


@dataclass(frozen=True, slots=True)
class CapturedContract:
    """One contract method evaluated to its AST. ``result`` is a fact the analyzer
    reads or a predicate it checks; the kind is read off the node's type. A capture
    that tripped a :class:`ContractError` carries ``error`` instead of a ``result``,
    so a malformed body becomes a finding rather than aborting class creation."""

    name: str
    result: ContractResult | None
    error: str | None = None

    @property
    def is_fact(self) -> bool:
        return isinstance(self.result, FactNode)

    @property
    def is_predicate(self) -> bool:
        return isinstance(self.result, Pred)


def capture(name: str, method: ContractMethod) -> CapturedContract:
    """Run a marked method once over a self-proxy and record its AST.

    A body that returns anything other than a fact or predicate proxy is a
    contract error: the surface only builds those two, so any other return means
    the body did not use the proxy API as intended.
    """
    produced = method(ContractSelf())
    if isinstance(produced, FactProxy):
        return CapturedContract(name, produced.fact)
    if isinstance(produced, PredicateProxy):
        return CapturedContract(name, produced.pred)
    raise ContractError(
        f"contract {name!r} must return a fact or a predicate, got {type(produced).__name__}"
    )
