"""The internal expression AST a contract body builds.

A contract method returns a symbolic expression over column proxies, and the
proxies fold that expression into the nodes here (the proxy layer is the author's
surface; this is the data the framework reads). Keeping our own AST, rather than
producing sqlglot directly, lets one tree serve every consumer: the fact bridge
reads a :class:`FactNode` straight off it, the SQL compiler lowers a value or
predicate to sqlglot for the execution path, and a future change-impact reporter
walks the same shape. See ``docs/design/dblect_technical_intro.md`` (the column
proxy and expression builder) and ``docs/design/dsl-reference.md``.

Every node is a frozen value, so an expression is hashable, comparable, and safe
to stash in a :class:`~dblect.types.contract.ContractSpec`. A column reference
carries an optional model name: ``None`` is the contract's own model (``self.col``)
and a name is another model (``models.other.col``), resolved against the manifest
in the bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto


class AggFunc(StrEnum):
    """The reductions a column proxy offers. ``COUNT`` and ``COUNT_DISTINCT`` are
    always well typed; the rest carry the coherence obligation the algebra names."""

    SUM = auto()
    AVG = auto()
    MIN = auto()
    MAX = auto()
    COUNT = auto()
    COUNT_DISTINCT = auto()


class ArithOp(StrEnum):
    """Binary arithmetic over magnitudes (the dimensional algebra lives downstream;
    here we only record the operator)."""

    ADD = auto()
    SUB = auto()
    MUL = auto()
    DIV = auto()


class CmpOp(StrEnum):
    """A comparison operator, the head of a predicate."""

    EQ = auto()
    NE = auto()
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()


class BoolOp(StrEnum):
    """A boolean combinator over predicates (Polars ``& | ~`` convention)."""

    AND = auto()
    OR = auto()
    NOT = auto()


# --- value expressions ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Col:
    """A column reference. ``model`` is ``None`` for the contract's own model
    (``self.col``) and a manifest reference otherwise (``models.other.col``)."""

    model: str | None
    name: str


@dataclass(frozen=True, slots=True)
class Lit:
    """A numeric literal appearing in arithmetic or on one side of a comparison."""

    value: float


@dataclass(frozen=True, slots=True)
class Agg:
    """A reduction of a column, optionally per group and over a named join.

    ``group_by`` empty is a whole-relation reduction. ``joined_on`` names the join
    an aggregation ranges over when the grouped key lives on a different relation
    than the measure, so a sum on one model can be grouped by a key from another.
    """

    func: AggFunc
    operand: ValueExpr
    group_by: tuple[Col, ...] = ()
    joined_on: Pred | None = None


@dataclass(frozen=True, slots=True)
class Arith:
    """Binary arithmetic over two value expressions."""

    op: ArithOp
    left: ValueExpr
    right: ValueExpr


ValueExpr = Col | Lit | Agg | Arith


# --- predicates -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tolerance:
    """The slack an equality is checked under. ``relative_to`` reinterprets ``eps``
    as a fraction of that reference value rather than an absolute amount."""

    eps: float
    relative_to: ValueExpr | None = None


@dataclass(frozen=True, slots=True)
class Compare:
    """A comparison of two value expressions. ``tolerance`` is meaningful only for
    an equality and is ignored elsewhere."""

    op: CmpOp
    left: ValueExpr
    right: ValueExpr
    tolerance: Tolerance | None = None


@dataclass(frozen=True, slots=True)
class IsNull:
    """A row predicate testing a column for NULL (``negated`` flips to NOT NULL)."""

    column: Col
    negated: bool = False


@dataclass(frozen=True, slots=True)
class InSet:
    """A row predicate testing membership in a set of literal values."""

    column: Col
    values: tuple[float | str, ...]


@dataclass(frozen=True, slots=True)
class Between:
    """A row predicate testing an inclusive range."""

    column: Col
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class BoolNode:
    """A boolean combination of predicates. ``NOT`` carries exactly one operand."""

    op: BoolOp
    operands: tuple[Pred, ...]


Pred = Compare | IsNull | InSet | Between | BoolNode


# --- facts ----------------------------------------------------------------------
#
# A contract method that returns one of these feeds the substrate rather than
# being run: it is a vouched assertion the analyzer trusts to discharge
# obligations and propagate. The bridge lowers each to the matching substrate fact.


@dataclass(frozen=True, slots=True)
class KeyFact:
    """The columns are unique together: one row per value of the tuple."""

    columns: tuple[Col, ...]


@dataclass(frozen=True, slots=True)
class GrainFact:
    """This relation has one row per ``per`` tuple. A key stated as grain."""

    per: tuple[Col, ...]


@dataclass(frozen=True, slots=True)
class ReferencesFact:
    """``child`` references ``parent``: a foreign-key edge into another relation."""

    child: Col
    parent: Col


@dataclass(frozen=True, slots=True)
class DeterminesFact:
    """The functional dependency ``determinant -> dependent``."""

    determinant: tuple[Col, ...]
    dependent: Col


FactNode = KeyFact | GrainFact | ReferencesFact | DeterminesFact


# The two things a ``@contract`` method may return: a fact the analyzer reads, or
# a predicate it checks by running.
ContractResult = Pred | FactNode
