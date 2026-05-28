"""Demo nullability property: per-column tri-state {NON_NULL, NULLABLE, UNKNOWN}.

**This is a demo, not a production property.** It exists to exercise the
substrate's structural reshape from #25: a CTE-wrapped ``coalesce`` should
propagate NON_NULL through to the outer projection, and a UNION ALL with
one nullable arm should taint the combined output via ``semiring.plus``.
Both depend on the immediate-upstream graph having CTE columns and UNION
arms materialised as their own entries; the operator rules below then
fire on the wrapped expressions directly.

The shortcuts that keep this from being production-ready are tracked in
#26; the rough list:

* The source rule defaults every leaf to ``UNKNOWN``. A real property
  would consult the manifest's ``not_null`` tests and the column's
  declared ``nullable`` flag.
* Operator coverage is the bare minimum: ``coalesce``, ``IS NOT NULL``,
  and the default times-fold for arithmetic. No ``CASE``, no ``NULLIF``,
  no ``ifnull``-family functions, no window-function handling.
* Aggregate rules are minimal: ``COUNT`` is always NON_NULL; SUM/MIN/MAX
  inherit from their input. A real property would distinguish "aggregate
  over empty set returns NULL" (SUM, MAX, MIN, AVG) from the COUNT case.

The algebra is a near-semiring: ``plus`` and ``times`` are the same
"any-input-nullable taints" rule, since UNION arms and times-folded
inputs both contribute their nullability to the output. Coalesce overrides
this with its specific "non-null wins" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr
from sqlglot import expressions as exp

from dblect.lineage.graph import ColumnRef
from dblect.lineage.property import Property


class Nullability(StrEnum):
    """The three values a column's nullability can take.

    Ordering is informational only; the lattice operations are defined in
    ``NullabilitySemiring`` and the operator rules below.
    """

    NON_NULL = "non_null"
    NULLABLE = "nullable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NullabilitySemiring:
    """``plus`` and ``times`` both apply the "any nullable input wins" rule.

    UNKNOWN beats NON_NULL because we shouldn't claim non-null when an
    input's nullability is unknown. NULLABLE beats UNKNOWN because a
    known-nullable input is enough to taint the output regardless of what
    we don't know about the others.

    Identities ``zero == one == NON_NULL`` keep ``plus(zero, x) == x``
    and ``times(one, x) == x``. The semiring is non-strict (``zero x x``
    isn't always ``zero``); the same near-semiring shape as
    ``UnionSemiring``.
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
    """Default leaf rule: nothing is claimed without manifest evidence.

    Tests that want NON_NULL leaves can build a ``Property`` with a custom
    source rule. The demo deliberately doesn't ship a manifest-reading
    source rule because the real one wants ``not_null`` test integration
    and that belongs to the production work tracked in the follow-up.
    """
    return Nullability.UNKNOWN


def _coalesce_rule(expr: Expr, child_ks: tuple[Nullability, ...]) -> Nullability:
    """``COALESCE(a, b, ...)`` is NON_NULL when any input is NON_NULL.

    With all inputs NULLABLE, output is NULLABLE. With any UNKNOWN input
    among NULLABLEs and no NON_NULLs, output is UNKNOWN (a NON_NULL among
    the unknowns would still rescue it, but we can't prove that here).
    """
    if not child_ks:
        return Nullability.UNKNOWN
    if any(k is Nullability.NON_NULL for k in child_ks):
        return Nullability.NON_NULL
    if all(k is Nullability.NULLABLE for k in child_ks):
        return Nullability.NULLABLE
    return Nullability.UNKNOWN


def _is_not_null_rule(expr: Expr, child_ks: tuple[Nullability, ...]) -> Nullability:
    """``x IS NOT NULL`` is NON_NULL (returns a boolean, never NULL)."""
    return Nullability.NON_NULL


def _count_rule(expr: exp.AggFunc, child_k: Nullability) -> Nullability:
    """``COUNT(...)`` is always NON_NULL: empty input groups still count to 0."""
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
