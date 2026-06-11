"""The contract/proxy layer: the symbolic expression surface a ``@contract``
method builds, the AST it folds into, and the compiler that lowers a predicate to
SQL for the execution path.

A model contract relates several columns, rows, or models through methods marked
:func:`contract`. The method body manipulates column proxies (``self.col``,
``models.other.col``), and the operators build an :mod:`~dblect.contracts.ast`
tree the framework reads. A returned fact feeds the substrate; a returned
predicate is checked by running. See ``docs/design/declaration-dsl.md`` and
``docs/design/dblect_technical_intro.md``.
"""

from __future__ import annotations

from dblect.contracts import ast
from dblect.contracts.compile import (
    GroupedResult,
    compile_predicate,
    compile_value,
    evaluate_predicate,
)
from dblect.contracts.decorator import (
    CapturedContract,
    ContractMethod,
    capture,
    contract,
    is_contract,
)
from dblect.contracts.proxy import (
    AggregateProxy,
    ColumnProxy,
    ContractError,
    ContractSelf,
    FactProxy,
    ModelProxy,
    PredicateProxy,
    models,
)

__all__ = [
    "AggregateProxy",
    "CapturedContract",
    "ColumnProxy",
    "ContractError",
    "ContractMethod",
    "ContractSelf",
    "FactProxy",
    "GroupedResult",
    "ModelProxy",
    "PredicateProxy",
    "ast",
    "capture",
    "compile_predicate",
    "compile_value",
    "contract",
    "evaluate_predicate",
    "is_contract",
    "models",
]
