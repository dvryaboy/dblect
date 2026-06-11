"""Column proxies: the author's surface that builds the expression AST.

A contract body manipulates proxies, not values. ``self.col`` and
``models.other.col`` return symbolic objects whose Python operators fold an
:mod:`~dblect.contracts.ast` tree instead of computing. This is the
SQLAlchemy / Ibis / Polars idiom: operators on a column object return more
column objects, and a comparison returns a predicate rather than a Python bool.
Nothing here touches the manifest or any data; resolution happens later in the
bridge. See ``docs/design/dblect_technical_intro.md``.

The proxies are deliberately thin wrappers over AST nodes. The base
:class:`_ValueProxy` carries one :data:`~dblect.contracts.ast.ValueExpr` and the
arithmetic and comparison operators every value shares; :class:`ColumnProxy` adds
the reductions, row predicates, and fact constructors that only make sense on a
bare column, and :class:`AggregateProxy` adds grouping.
"""

from __future__ import annotations

from typing import Final

from dblect.contracts.ast import (
    Agg,
    AggFunc,
    Arith,
    ArithOp,
    Between,
    BoolNode,
    BoolOp,
    CmpOp,
    Col,
    Compare,
    DeterminesFact,
    FactNode,
    GrainFact,
    InSet,
    IsNull,
    KeyFact,
    Lit,
    Pred,
    ReferencesFact,
    Tolerance,
    ValueExpr,
)


class ContractError(Exception):
    """A contract body that does not type-check structurally (a tolerance on a
    non-equality, a non-column where a column is required, and the like). Raised
    at capture time, surfaced by the contract layer rather than crashing the scan.
    """


def _as_value(obj: _ValueProxy | int | float) -> ValueExpr:
    if isinstance(obj, _ValueProxy):
        return obj.expr
    return Lit(float(obj))


class _ValueProxy:
    """A value expression under construction: a column, an aggregate, an
    arithmetic result, or a literal. Carries the arithmetic and comparison
    operators shared by everything that denotes a value."""

    __slots__ = ("expr",)

    def __init__(self, expr: ValueExpr) -> None:
        self.expr = expr

    # arithmetic --------------------------------------------------------------
    def _arith(self, op: ArithOp, other: _ValueProxy | int | float, *, swap: bool) -> _ValueProxy:
        left, right = self.expr, _as_value(other)
        if swap:
            left, right = right, left
        return _ValueProxy(Arith(op, left, right))

    def __add__(self, other: _ValueProxy | int | float) -> _ValueProxy:
        return self._arith(ArithOp.ADD, other, swap=False)

    def __radd__(self, other: int | float) -> _ValueProxy:
        return self._arith(ArithOp.ADD, other, swap=True)

    def __sub__(self, other: _ValueProxy | int | float) -> _ValueProxy:
        return self._arith(ArithOp.SUB, other, swap=False)

    def __rsub__(self, other: int | float) -> _ValueProxy:
        return self._arith(ArithOp.SUB, other, swap=True)

    def __mul__(self, other: _ValueProxy | int | float) -> _ValueProxy:
        return self._arith(ArithOp.MUL, other, swap=False)

    def __rmul__(self, other: int | float) -> _ValueProxy:
        return self._arith(ArithOp.MUL, other, swap=True)

    def __truediv__(self, other: _ValueProxy | int | float) -> _ValueProxy:
        return self._arith(ArithOp.DIV, other, swap=False)

    def __rtruediv__(self, other: int | float) -> _ValueProxy:
        return self._arith(ArithOp.DIV, other, swap=True)

    # comparison --------------------------------------------------------------
    def _compare(self, op: CmpOp, other: _ValueProxy | int | float) -> PredicateProxy:
        return PredicateProxy(Compare(op, self.expr, _as_value(other)))

    def __eq__(self, other: _ValueProxy | int | float) -> PredicateProxy:  # type: ignore[override]
        return self._compare(CmpOp.EQ, other)

    def __ne__(self, other: _ValueProxy | int | float) -> PredicateProxy:  # type: ignore[override]
        return self._compare(CmpOp.NE, other)

    def __lt__(self, other: _ValueProxy | int | float) -> PredicateProxy:
        return self._compare(CmpOp.LT, other)

    def __le__(self, other: _ValueProxy | int | float) -> PredicateProxy:
        return self._compare(CmpOp.LE, other)

    def __gt__(self, other: _ValueProxy | int | float) -> PredicateProxy:
        return self._compare(CmpOp.GT, other)

    def __ge__(self, other: _ValueProxy | int | float) -> PredicateProxy:
        return self._compare(CmpOp.GE, other)

    # Defining __eq__ drops hashability; proxies are transient builders, never
    # keys, so that is the correct behaviour. Pin it so intent is explicit.
    __hash__ = None  # type: ignore[assignment]


def _col_of(proxy: _ValueProxy, role: str) -> Col:
    """The :class:`Col` a proxy denotes, or a contract error when it is not a bare
    column (a grouping key or a key field has to be a column, not an expression)."""
    if isinstance(proxy, ColumnProxy):
        return proxy.col
    raise ContractError(f"{role} must be a column, got {type(proxy).__name__}")


class ColumnProxy(_ValueProxy):
    """A bare column reference. Adds the reductions, row predicates, and fact
    constructors that only make sense on a column."""

    __slots__ = ()

    def __init__(self, col: Col) -> None:
        super().__init__(col)

    @property
    def col(self) -> Col:
        assert isinstance(self.expr, Col)
        return self.expr

    # reductions --------------------------------------------------------------
    def sum(self) -> AggregateProxy:
        return AggregateProxy(Agg(AggFunc.SUM, self.col))

    def avg(self) -> AggregateProxy:
        return AggregateProxy(Agg(AggFunc.AVG, self.col))

    def min(self) -> AggregateProxy:
        return AggregateProxy(Agg(AggFunc.MIN, self.col))

    def max(self) -> AggregateProxy:
        return AggregateProxy(Agg(AggFunc.MAX, self.col))

    def count(self) -> AggregateProxy:
        return AggregateProxy(Agg(AggFunc.COUNT, self.col))

    def count_distinct(self) -> AggregateProxy:
        return AggregateProxy(Agg(AggFunc.COUNT_DISTINCT, self.col))

    # row predicates ----------------------------------------------------------
    def is_null(self) -> PredicateProxy:
        return PredicateProxy(IsNull(self.col))

    def is_not_null(self) -> PredicateProxy:
        return PredicateProxy(IsNull(self.col, negated=True))

    def in_(self, values: tuple[float | str, ...]) -> PredicateProxy:
        return PredicateProxy(InSet(self.col, tuple(values)))

    def between(self, low: float, high: float) -> PredicateProxy:
        return PredicateProxy(Between(self.col, low, high))

    def equals(self, value: _ValueProxy | int | float) -> PredicateProxy:
        return self._compare(CmpOp.EQ, value)

    # fact constructors -------------------------------------------------------
    def references(self, parent: ColumnProxy) -> FactProxy:
        """A referencing edge from this column into ``parent`` (a foreign key)."""
        return FactProxy(ReferencesFact(self.col, parent.col))

    def determines(self, dependent: ColumnProxy) -> FactProxy:
        """The functional dependency ``self -> dependent``."""
        return FactProxy(DeterminesFact((self.col,), dependent.col))


class AggregateProxy(_ValueProxy):
    """A reduced column. ``group_by`` makes it per-group; ``joined_on`` names the
    join it ranges over when the grouping key lives on another relation."""

    __slots__ = ()

    def __init__(self, agg: Agg) -> None:
        super().__init__(agg)

    @property
    def agg(self) -> Agg:
        assert isinstance(self.expr, Agg)
        return self.expr

    def group_by(self, *cols: ColumnProxy) -> AggregateProxy:
        keys = tuple(_col_of(c, "a group_by key") for c in cols)
        return AggregateProxy(Agg(self.agg.func, self.agg.operand, keys, self.agg.joined_on))

    def joined_on(self, predicate: PredicateProxy) -> AggregateProxy:
        return AggregateProxy(
            Agg(self.agg.func, self.agg.operand, self.agg.group_by, predicate.pred)
        )


class PredicateProxy:
    """A predicate under construction. Carries tolerance for an equality and the
    boolean combinators."""

    __slots__ = ("pred",)

    def __init__(self, pred: Pred) -> None:
        self.pred = pred

    def within(self, eps: float) -> PredicateProxy:
        """Set an absolute tolerance on an equality. Meaningless elsewhere, so a
        non-equality is a contract error rather than a silently dropped slack."""
        cmp = self._equality("within(...)")
        return PredicateProxy(
            Compare(cmp.op, cmp.left, cmp.right, Tolerance(eps, _existing_ref(cmp)))
        )

    def relative_to(self, reference: _ValueProxy) -> PredicateProxy:
        """Reinterpret an already-set tolerance as a fraction of ``reference``."""
        cmp = self._equality("relative_to(...)")
        if cmp.tolerance is None:
            raise ContractError("relative_to(...) needs a within(...) tolerance to scale")
        return PredicateProxy(
            Compare(cmp.op, cmp.left, cmp.right, Tolerance(cmp.tolerance.eps, reference.expr))
        )

    def _equality(self, what: str) -> Compare:
        if isinstance(self.pred, Compare) and self.pred.op is CmpOp.EQ:
            return self.pred
        raise ContractError(f"{what} applies to an equality predicate")

    def __and__(self, other: PredicateProxy) -> PredicateProxy:
        return PredicateProxy(BoolNode(BoolOp.AND, (self.pred, other.pred)))

    def __or__(self, other: PredicateProxy) -> PredicateProxy:
        return PredicateProxy(BoolNode(BoolOp.OR, (self.pred, other.pred)))

    def __invert__(self) -> PredicateProxy:
        return PredicateProxy(BoolNode(BoolOp.NOT, (self.pred,)))


def _existing_ref(cmp: Compare) -> ValueExpr | None:
    return cmp.tolerance.relative_to if cmp.tolerance is not None else None


class FactProxy:
    """A fact a contract method returns. Inert: it only carries the AST node the
    bridge lowers to a substrate fact."""

    __slots__ = ("fact",)

    def __init__(self, fact: FactNode) -> None:
        self.fact = fact


# --- the subjects: ``self`` and ``models`` --------------------------------------


class ContractSelf:
    """The ``self`` a ``@contract`` method receives at capture: a proxy onto the
    contract's own model. Attribute access yields a column; the relation-scoped
    fact constructors live here."""

    __slots__ = ()

    def __getattr__(self, name: str) -> ColumnProxy:
        if name.startswith("__"):
            raise AttributeError(name)
        return ColumnProxy(Col(None, name))

    def key(self, *columns: ColumnProxy) -> FactProxy:
        """The columns are unique together."""
        cols = tuple(_col_of(c, "a key column") for c in columns)
        if not cols:
            raise ContractError("self.key(...) needs at least one column")
        return FactProxy(KeyFact(cols))

    def grain(self, *, per: ColumnProxy | tuple[ColumnProxy, ...]) -> FactProxy:
        """One row per ``per``. A key stated as the relation's grain."""
        proxies = per if isinstance(per, tuple) else (per,)
        cols = tuple(_col_of(c, "a grain column") for c in proxies)
        if not cols:
            raise ContractError("self.grain(per=...) needs at least one column")
        return FactProxy(GrainFact(cols))


class ModelProxy:
    """A model reference. Attribute access yields a column on that model."""

    __slots__ = ("model",)

    def __init__(self, model: str) -> None:
        self.model = model

    def __getattr__(self, name: str) -> ColumnProxy:
        if name.startswith("__"):
            raise AttributeError(name)
        return ColumnProxy(Col(self.model, name))


class _Models:
    """The lazy ``models`` namespace: ``models.stg_orders`` is a :class:`ModelProxy`
    for any name, captured symbolically and validated later. No codegen, no
    import-time resolution."""

    __slots__ = ()

    def __getattr__(self, name: str) -> ModelProxy:
        if name.startswith("__"):
            raise AttributeError(name)
        return ModelProxy(name)


models: Final[_Models] = _Models()
