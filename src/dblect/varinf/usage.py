"""The records the Jinja front end emits: one :class:`VarUsage` per ``var()`` /
``env_var()`` reference, tagged with the syntactic position it was found in.

These are the contract between the source-Jinja walker and the inference layer
that folds usages into a type and a domain. The walker produces them; nothing
here interprets them. The position is carried as a :data:`UsageContext`, a sum
of small frozen records (one per syntactic shape) rather than a string tag, so a
consumer matches over real variants and the type checker enforces exhaustiveness.

The control-flow versus value-substitution distinction the world enumerator
hinges on is read off the variant: :class:`TruthyTest`, :class:`Equality`,
:class:`Inequality`, :class:`InSet`, and :class:`Arithmetic` are the
branch-steering shapes; :class:`SqlLiteral` and :class:`MacroArg` are value
substitution; :class:`Unknown` is the honest fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

# A Jinja ``Const`` operand resolved inline by the parser. ``bool`` is listed
# ahead of ``int`` deliberately: in Python ``bool`` is a subtype of ``int``, so a
# consumer that wants the boolean reading must test ``isinstance(x, bool)`` before
# the numeric one. The walker preserves the parser's own value, it does not coerce.
LiteralValue: TypeAlias = bool | int | float | str


class VarKind(StrEnum):
    """Which builtin produced the reference: ``{{ var(...) }}`` or ``{{ env_var(...) }}``."""

    VAR = "var"
    ENV_VAR = "env_var"


class Confidence(StrEnum):
    """How sure the walker is that a usage is what it looks like.

    ``FULL`` is a usage read directly with no unresolved indirection. ``PARTIAL``
    marks a usage collected through an unresolved branch (both arms walked).
    ``OPAQUE`` marks one the walker could not resolve at all; downstream it
    degrades the var to a single resolved world rather than dropping it.
    """

    FULL = "full"
    PARTIAL = "partial"
    OPAQUE = "opaque"


class ComparisonOp(StrEnum):
    """The ordering comparisons :class:`Inequality` covers, named as Jinja names them."""

    LT = "lt"
    GT = "gt"
    LTEQ = "lteq"
    GTEQ = "gteq"


class ArithOp(StrEnum):
    """The binary arithmetic operators :class:`Arithmetic` covers."""

    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"
    FLOORDIV = "floordiv"
    MOD = "mod"
    POW = "pow"


class LiteralPosition(StrEnum):
    """A best-effort hint about where an interpolated value lands in the SQL.

    ``STRING_QUOTED`` is the only position the front end commits to in v1, read
    from the template text immediately around the interpolation (a value wrapped
    in matching quotes). Numeric and identifier positions need SQL-level context
    the Jinja AST does not carry, so they stay ``UNKNOWN`` here and are a noted
    follow-up rather than a guess.
    """

    STRING_QUOTED = "string_quoted"
    NUMERIC = "numeric"
    IDENTIFIER = "identifier"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TruthyTest:
    """The ``var`` call is the test of an ``{% if %}`` / ``{% for %}`` (or nested
    under boolean ops within one). Branch-steering: the strongest control-flow signal."""


@dataclass(frozen=True, slots=True)
class Equality:
    """The ``var`` call is compared for equality against a literal: ``var('x') == 'a'``."""

    operand: LiteralValue


@dataclass(frozen=True, slots=True)
class Inequality:
    """The ``var`` call is ordered against a literal: ``var('x') > 100``.

    ``op`` is the comparison as written with the ``var`` call on the left; the
    walker normalizes a literal-on-the-left form to the equivalent left-hand op.
    """

    operand: LiteralValue
    op: ComparisonOp


@dataclass(frozen=True, slots=True)
class InSet:
    """The ``var`` call is tested for membership: ``var('x') in ['a', 'b']``."""

    elements: tuple[LiteralValue, ...]


@dataclass(frozen=True, slots=True)
class Arithmetic:
    """The ``var`` call is an operand of arithmetic: ``var('x') + 1``.

    ``other`` is the literal operand when the other side is a ``Const``; ``None``
    when it is a non-literal expression (the operator is still recorded).
    """

    op: ArithOp
    other: LiteralValue | None = None


@dataclass(frozen=True, slots=True)
class SqlLiteral:
    """The ``var`` call is interpolated into the rendered SQL as a value (it sits
    under an ``Output`` node), not steering a branch."""

    position: LiteralPosition = LiteralPosition.UNKNOWN


@dataclass(frozen=True, slots=True)
class MacroArg:
    """The ``var`` call is passed as an argument to another call (a macro the
    direct walk does not follow). ``macro`` is the callee name, ``position`` the
    zero-based argument index."""

    macro: str
    position: int


@dataclass(frozen=True, slots=True)
class Unknown:
    """Any syntactic position the walker recognizes as a ``var`` call but does not
    classify further. The honest fallback, never a crash."""


# The syntactic position of a ``var`` call. Consumers match over the variants;
# adding a shape is a new member here plus the match arms that need it.
UsageContext: TypeAlias = (
    TruthyTest | Equality | Inequality | InSet | Arithmetic | SqlLiteral | MacroArg | Unknown
)


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where a usage was found. ``column`` is best-effort: jinja2 AST nodes carry a
    line number but not a column, so it is ``None`` until recovered by re-lexing."""

    file: str | None
    line: int
    column: int | None = None


@dataclass(frozen=True, slots=True)
class VarUsage:
    """One ``var()`` / ``env_var()`` reference, with the position it was found in.

    ``macro_trail`` is empty for a direct reference; the macro-following stream
    fills it with the macros traversed to reach a usage. ``confidence`` carries
    the walker's certainty (see :class:`Confidence`).
    """

    var_name: str
    var_kind: VarKind
    context: UsageContext
    location: SourceLocation
    macro_trail: tuple[str, ...] = ()
    confidence: Confidence = Confidence.FULL


@dataclass(frozen=True, slots=True)
class OpaqueNode:
    """A node (or macro body) the environment could not parse, recorded instead of
    raised. The reason names what defeated the parse so the diagnostic report can
    tell the user exactly what needs a manual declaration."""

    unique_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class WalkResult:
    """What walking one node's source yields: the usages found and, when the parse
    failed, the opaque diagnostic explaining why none were."""

    usages: tuple[VarUsage, ...] = ()
    opaque: OpaqueNode | None = None

    @property
    def parsed(self) -> bool:
        """True when the source parsed (whether or not it contained any vars)."""
        return self.opaque is None
