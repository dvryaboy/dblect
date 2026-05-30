"""Demo nullability property: per-column tri-state {NON_NULL, NULLABLE, UNKNOWN}.

**Demo, not a production property.** Pins that a CTE-wrapped ``coalesce``
propagates NON_NULL to the outer projection, and that a UNION ALL with
one nullable arm taints the combined output via ``semiring.plus``.

Gaps before this is consumable as a real property:

* Source rule defaults every leaf to ``UNKNOWN``; a real one would
  consult manifest ``not_null`` tests and the declared ``nullable`` flag.
* Operator coverage is the bare minimum (``coalesce``, ``IS NOT NULL``,
  default times-fold). No ``CASE``, ``NULLIF``, ``ifnull`` family, no
  window handling.
* Aggregate rules only cover ``COUNT``. SUM/MIN/MAX/AVG over an empty
  set return NULL, which the default fold doesn't model.

``plus`` and ``times`` apply the same "any input nullable taints" rule;
``COALESCE`` overrides with "non-null wins."
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr
from sqlglot import expressions as exp

from dblect.lineage.graph import ColumnRef
from dblect.lineage.property import Property


class Nullability(StrEnum):
    NON_NULL = "non_null"
    NULLABLE = "nullable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NullabilitySemiring:
    """``plus`` and ``times`` both apply the "any nullable input taints" rule.

    NULLABLE beats UNKNOWN beats NON_NULL: a known-nullable input taints
    regardless of what's unknown about the others; UNKNOWN beats NON_NULL
    because we shouldn't claim non-null without evidence.
    """

    zero: Nullability = Nullability.NON_NULL
    one: Nullability = Nullability.NON_NULL

    def plus(self, a: Nullability, b: Nullability) -> Nullability:
        if a is Nullability.NULLABLE or b is Nullability.NULLABLE:
            return Nullability.NULLABLE
        if a is Nullability.UNKNOWN or b is Nullability.UNKNOWN:
            return Nullability.UNKNOWN
        return Nullability.NON_NULL

    def times(self, a: Nullability, b: Nullability) -> Nullability:
        return self.plus(a, b)


def _source_unknown(_: ColumnRef) -> Nullability:
    return Nullability.UNKNOWN


def _coalesce_rule(expr: Expr, child_ks: tuple[Nullability, ...]) -> Nullability:
    if not child_ks:
        return Nullability.UNKNOWN
    if any(k is Nullability.NON_NULL for k in child_ks):
        return Nullability.NON_NULL
    if all(k is Nullability.NULLABLE for k in child_ks):
        return Nullability.NULLABLE
    return Nullability.UNKNOWN


def _is_not_null_rule(expr: Expr, child_ks: tuple[Nullability, ...]) -> Nullability:
    return Nullability.NON_NULL


def _count_rule(expr: exp.AggFunc, child_k: Nullability) -> Nullability:
    # COUNT returns 0 for empty groups, never NULL.
    return Nullability.NON_NULL


nullability: Property[Nullability] = Property(
    name="nullability",
    semiring=NullabilitySemiring(),
    source=_source_unknown,
    operators={
        exp.Coalesce: _coalesce_rule,
        exp.Is: _is_not_null_rule,
    },
    aggregates={
        exp.Count: _count_rule,
    },
    unknown_value=Nullability.UNKNOWN,
)
