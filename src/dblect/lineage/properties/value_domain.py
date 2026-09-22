"""Value-domain property: a column's closed set of non-null values.

A ``NominalEnum``/``UnitEnum`` field, or a dbt ``accepted_values`` test, names the
closed set a column's value is drawn from; a literal outside that set makes an
equality comparison against the column provably dead. This module is the lattice
and transfer table that let the dead-predicate check read a column's *effective*
value set after it has flowed through renames, ``CASE``, ``COALESCE``, and
confluences, not just at its declaration site. ``Unbounded`` makes no claim (the
lattice top); ``Bounded(values)`` is a known non-null set, so two claims about
one column combine by intersection (meet) and values from two branches (a
``CASE``, a ``UNION``) combine by union (join). The empty set is an ordinary
``Bounded`` value: a column that is always NULL has no non-null values at all,
and two declarations sharing no value also intersect to it, which the check
layer reports as a declaration conflict.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import reduce
from typing import TYPE_CHECKING, Final, cast, final

from sqlglot import Expr
from sqlglot import expressions as exp

from dblect.lineage.facts.grounding import generic_test_column_ref
from dblect.lineage.facts.kit import column_kit, constant_aggregate, top_rule
from dblect.lineage.facts.lattice import Lattice, annotate_fold
from dblect.lineage.facts.model import (
    Annotation,
    Declared,
    DeclaredSource,
    Fact,
    Opacity,
    Predicate,
)
from dblect.lineage.facts.property import (
    AggregateRule,
    DepContext,
    FactDiscoverer,
    OperatorTransfer,
    Property,
)
from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.predicate import Lit, LitKind, lit_of
from dblect.manifest import generic_test_target_uid

if TYPE_CHECKING:
    from dblect.manifest import Manifest

# --- the value ----------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class Bounded:
    """A column's known, closed set of non-null values. The empty set is an
    ordinary value (a column that is always NULL) and adds nothing to a union,
    so a ``NULL`` branch leaves the other branches' set intact."""

    values: frozenset[Lit]


@final
@dataclass(frozen=True, slots=True)
class _Unbounded:
    """No claim about the column's values: any value is possible. One shared
    instance, ``UNBOUNDED``, with no payload."""


UNBOUNDED: Final[_Unbounded] = _Unbounded()

# A column's value domain: a known closed set, or no claim at all.
ValueDomain = Bounded | _Unbounded


# --- the lattice ----------------------------------------------------------------


def _meet(a: ValueDomain, b: ValueDomain) -> ValueDomain:
    if isinstance(a, _Unbounded):
        return b
    if isinstance(b, _Unbounded):
        return a
    return Bounded(a.values & b.values)


def _join(a: ValueDomain, b: ValueDomain) -> ValueDomain:
    if isinstance(a, _Unbounded) or isinstance(b, _Unbounded):
        return UNBOUNDED
    return Bounded(a.values | b.values)


VALUE_DOMAIN_LATTICE: Final[Lattice[ValueDomain]] = Lattice(
    meet=_meet,
    join=_join,
    top=UNBOUNDED,
    bottom=Bounded(frozenset()),
)


# --- transfer helpers -------------------------------------------------------


def _annotate(
    value: ValueDomain, kids: tuple[Annotation[ValueDomain], ...]
) -> Annotation[ValueDomain]:
    """Wrap a transfer's result value with the diagnostic bits derived from its
    inputs, via the shared :func:`~dblect.lineage.facts.lattice.annotate_fold`."""
    return annotate_fold(VALUE_DOMAIN_LATTICE, value, kids)


# --- operator transfers -------------------------------------------------------
#
# Every transfer rule here is named and closed rather than inferred by a generic
# fold: an unmodelled operator (arithmetic, string functions, windows, ``CAST``)
# widens to Unbounded through the catch-all below rather than guessing, which is
# what keeps the property sound by default.


def _literal_rule(
    expr: Expr, _kids: tuple[Annotation[ValueDomain], ...], _ctx: DepContext
) -> Annotation[ValueDomain]:
    """A literal is the one-element set of itself. A literal ``lit_of`` cannot
    read (only string and numeric literals are modelled) widens to Unbounded
    rather than producing the empty set, which would turn "this literal's kind
    is unknown" into "this literal can never occur"."""
    lit = lit_of(expr) if isinstance(expr, exp.Literal) else None
    if lit is None:
        return Annotation(UNBOUNDED, Opacity.IMPLICIT)
    return Annotation(Bounded(frozenset({lit})), Opacity.CONCRETE)


def _null_literal_rule(
    _expr: Expr, _kids: tuple[Annotation[ValueDomain], ...], _ctx: DepContext
) -> Annotation[ValueDomain]:
    """A bare ``NULL`` literal contributes no non-null value: the empty set,
    which adds nothing to a union. Returning Unbounded here instead would widen
    every ELSE-less ``CASE`` and every ``COALESCE(x, NULL)`` to "any value" and
    silence the check on exactly the shapes this property exists to catch."""
    return Annotation(Bounded(frozenset()), Opacity.CONCRETE)


def _coalesce_rule(
    _expr: Expr, kids: tuple[Annotation[ValueDomain], ...], _ctx: DepContext
) -> Annotation[ValueDomain]:
    """``COALESCE(a, b, ...)`` is the union of its arguments' sets: whichever
    argument survives, its value came from one of them."""
    if not kids:
        return Annotation(UNBOUNDED, Opacity.IMPLICIT)
    value = reduce(VALUE_DOMAIN_LATTICE.join, (k.value for k in kids))
    return _annotate(value, kids)


def _if_arm_rule(
    expr: Expr, kids: tuple[Annotation[ValueDomain], ...], _ctx: DepContext
) -> Annotation[ValueDomain]:
    """``exp.If`` backs two shapes sqlglot shares one node for: a ``CASE ...
    WHEN cond THEN then`` arm (no ``false``), whose condition is a WHEN test
    rather than a column value, so only the THEN branch counts here; and a
    standalone ``IF(cond, then, else)`` call, which falls back to the
    catch-all (top) like any other unmodelled function rather than silently
    discarding its ELSE branch."""
    if expr.args.get("false") is not None:
        return Annotation(UNBOUNDED, Opacity.IMPLICIT, provisional=any(k.provisional for k in kids))
    if len(kids) < 2:
        return Annotation(UNBOUNDED, Opacity.IMPLICIT)
    return kids[1]


def _case_rule(
    expr: Expr, kids: tuple[Annotation[ValueDomain], ...], _ctx: DepContext
) -> Annotation[ValueDomain]:
    """``CASE ... END`` is the union of every THEN branch and the ELSE. A
    missing ELSE contributes the empty set (it yields NULL), never Unbounded,
    and a simple-CASE's own test expression contributes nothing, since it is
    what is tested, not a value the CASE can produce. ``kids`` holds the
    reduced annotations of ``expr.args``' children in order (the optional
    ``this``, one per ``ifs`` arm already reduced to its THEN value by
    :func:`_if_arm_rule`, then the optional ``default``), so this rule reads
    ``expr.args`` to single out the arm and ELSE slices regardless of order."""
    assert isinstance(expr, exp.Case)
    idx = 1 if expr.args.get("this") is not None else 0
    n_ifs = len(expr.args.get("ifs") or [])
    arm_values = [k.value for k in kids[idx : idx + n_ifs]]
    idx += n_ifs
    default = expr.args.get("default")
    else_value: ValueDomain = kids[idx].value if default is not None else Bounded(frozenset())
    value = reduce(VALUE_DOMAIN_LATTICE.join, arm_values, else_value)
    return _annotate(value, kids)


VALUE_DOMAIN_OPERATORS: Mapping[type[Expr], OperatorTransfer[ValueDomain]] = {
    exp.Literal: _literal_rule,
    exp.Null: _null_literal_rule,
    exp.Coalesce: _coalesce_rule,
    exp.If: _if_arm_rule,
    exp.Case: _case_rule,
    # The catch-all, registered on the root every sqlglot node's MRO shares, so
    # any node without a more specific entry above (CAST, arithmetic, string
    # functions, windows) widens to top.
    Expr: top_rule(VALUE_DOMAIN_LATTICE),
}

# Every aggregate widens to top: even COUNT's result is a cardinality, not a
# member of the aggregated column's own domain. MIN/MAX could in principle pass
# the set through; that is a follow-up, not this property's scope today.
VALUE_DOMAIN_AGGREGATES: Mapping[type[exp.AggFunc], AggregateRule[ValueDomain]] = {
    exp.AggFunc: constant_aggregate(UNBOUNDED, opacity=Opacity.IMPLICIT),
}


# --- the property -------------------------------------------------------------
#
# UNION needs no rule of its own: the propagator already combines a confluence's
# arms with the lattice join, the set union this property wants.
_VALUE_DOMAIN_KIT = column_kit(
    name="value_domain",
    lattice=VALUE_DOMAIN_LATTICE,
    operators=VALUE_DOMAIN_OPERATORS,
    aggregates=VALUE_DOMAIN_AGGREGATES,
)

value_domain_grounding = _VALUE_DOMAIN_KIT.grounding
value_domain_grounded_scopes = _VALUE_DOMAIN_KIT.grounded_scopes
value_domain_conflicts = _VALUE_DOMAIN_KIT.conflicts


def value_domain_property(
    facts: Mapping[ColumnRef, tuple[Fact[ValueDomain, ColumnRef], ...]],
) -> Property[ValueDomain, ColumnRef]:
    """The manifest-backed value-domain property, grounded from ``facts``
    (an ``accepted_values`` test, a contract enum, or both, already folded by
    :func:`value_domain_facts`)."""
    return _VALUE_DOMAIN_KIT.property(facts)


# --- discoverers --------------------------------------------------------------


def _lits_of(values: list[object]) -> list[Lit] | None:
    """The declared values as ``Lit``s, or ``None`` if any value falls outside
    this engine's two kinds (string and numeric). ``bool`` is excluded even
    though it subclasses ``int``: dblect never treats a boolean as a numeric
    ``Lit``."""
    out: list[Lit] = []
    for v in values:
        if isinstance(v, bool):
            return None
        if isinstance(v, str):
            out.append(Lit(LitKind.STR, v))
        elif isinstance(v, int | float):
            out.append(Lit(LitKind.NUM, Decimal(str(v))))
        else:
            return None
    return out


class _AcceptedValuesTestDiscoverer:
    """Grounds a :class:`Bounded` value domain from enabled ``accepted_values``
    generic tests. A ``where`` filter is handled as ``nullability.py``'s
    ``_NotNullTestDiscoverer`` handles its own (the fact carries the predicate
    but grounding does not fold it into the unconditional annotation), and a
    value list carrying anything outside ``Lit``'s two kinds makes the whole
    test ground nothing rather than a set silently missing that member."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[ValueDomain, ColumnRef]]:
        out: list[Fact[ValueDomain, ColumnRef]] = []
        for node in manifest.nodes.values():
            tm = node.test_metadata
            if tm is None or not tm.enabled or tm.name != "accepted_values":
                continue
            col = tm.kwargs.get("column_name")
            if not isinstance(col, str) or not col:
                continue
            values = tm.kwargs.get("values")
            if not isinstance(values, list) or not values:
                continue
            lits = _lits_of(cast("list[object]", values))
            if lits is None:
                continue
            target = generic_test_target_uid(node)
            if target is None:
                continue
            scope = generic_test_column_ref(manifest, target, col)
            if scope is None:
                continue
            out.append(
                Fact(
                    scope=scope,
                    value=Bounded(frozenset(lits)),
                    provenance=Declared(DeclaredSource.DBT_GENERIC_TEST),
                    detail=node.name,
                    condition=Predicate(tm.where) if tm.where is not None else None,
                )
            )
        return out


def accepted_values_discoverer() -> FactDiscoverer[ValueDomain, ColumnRef]:
    return _AcceptedValuesTestDiscoverer()


def value_domain_facts(
    manifest: Manifest,
    *,
    extra_facts: tuple[Fact[ValueDomain, ColumnRef], ...] = (),
) -> Mapping[ColumnRef, tuple[Fact[ValueDomain, ColumnRef], ...]]:
    """Every declared value domain, collected per column: enabled
    ``accepted_values`` tests plus ``extra_facts`` the caller already resolved
    (a bare ``NominalEnum``/``UnitEnum`` scalar, or an open enum facet on a
    domain type, from the contract bridge)."""
    return _VALUE_DOMAIN_KIT.facts(
        manifest, (accepted_values_discoverer(),), extra_facts=extra_facts
    )
