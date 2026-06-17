"""Walk a node's source Jinja and emit a :class:`VarUsage` per ``var()`` /
``env_var()`` reference, tagged with the syntactic position it sits in.

The walk is context-carrying: each recursive step knows what context to assign a
bare ``var`` call found directly at that position (the ``default`` argument),
while the operator nodes that define a richer context (``If`` tests, ``Compare``,
arithmetic, other calls) classify their ``var`` operands themselves. The
control-flow versus value-substitution signal the world enumerator depends on is
read straight off the resulting :data:`UsageContext` variant, with no text
heuristics.

Direct usage only. A ``var`` reached through a macro is the macro-following
stream's concern, so a ``var`` passed to another call is recorded as a
:class:`MacroArg` here and followed there. A body the environment cannot parse
degrades to one :class:`OpaqueNode` diagnostic rather than raising.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jinja2 import TemplateError, nodes

from dblect.varinf.environment import make_environment
from dblect.varinf.usage import (
    Arithmetic,
    ArithOp,
    Comparison,
    ComparisonOp,
    InSet,
    LiteralValue,
    MacroArg,
    OpaqueNode,
    SourceLocation,
    SqlLiteral,
    TruthyTest,
    Unknown,
    UsageContext,
    VarKind,
    VarUsage,
    WalkResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_VAR_BUILTINS = frozenset({"var", "env_var"})

# Jinja arithmetic ``BinExpr`` subclasses to our operator enum. ``And`` / ``Or``
# are also ``BinExpr`` subclasses but are handled as boolean (control-flow)
# before this mapping is consulted.
_ARITH_OPS: dict[type[nodes.BinExpr], ArithOp] = {
    nodes.Add: ArithOp.ADD,
    nodes.Sub: ArithOp.SUB,
    nodes.Mul: ArithOp.MUL,
    nodes.Div: ArithOp.DIV,
    nodes.FloorDiv: ArithOp.FLOORDIV,
    nodes.Mod: ArithOp.MOD,
    nodes.Pow: ArithOp.POW,
}

# ComparisonOp's values are jinja2's own compare op-names (see usage.py), so a
# string is a comparison we model exactly when it parses to a member. Deriving the
# set from the enum keeps one source of truth; membership (in/notin) is a different
# shape, classified as InSet before this set is consulted.
_COMPARISON_OP_VALUES: frozenset[str] = frozenset(op.value for op in ComparisonOp)

# How each comparison flips when the literal is written on the left
# (``100 < var('x')`` is ``var('x') > 100``). Equality is symmetric, so eq/ne map
# to themselves. Total over ComparisonOp and its own inverse.
_FLIPPED_COMPARISON: dict[ComparisonOp, ComparisonOp] = {
    ComparisonOp.EQ: ComparisonOp.EQ,
    ComparisonOp.NE: ComparisonOp.NE,
    ComparisonOp.LT: ComparisonOp.GT,
    ComparisonOp.GT: ComparisonOp.LT,
    ComparisonOp.LTEQ: ComparisonOp.GTEQ,
    ComparisonOp.GTEQ: ComparisonOp.LTEQ,
}


def walk_source(source: str, *, unique_id: str, file_path: str | None = None) -> WalkResult:
    """Parse ``source`` and collect every direct ``var()`` / ``env_var()`` usage.

    ``unique_id`` names the node for an opaque diagnostic; ``file_path`` is the
    source path stamped onto each usage's location. A parse failure yields a
    :class:`WalkResult` carrying one :class:`OpaqueNode` and no usages.
    """
    env = make_environment()
    try:
        template = env.parse(source)
    except (TemplateError, SyntaxError, ValueError) as exc:
        # Degrade, don't lie: a body we cannot parse becomes one diagnostic, never a
        # crash and never a silent miss.
        reason = f"{type(exc).__name__}: {exc}"
        return WalkResult(opaque=OpaqueNode(unique_id=unique_id, reason=reason))

    walker = _Walker(file_path=file_path)
    for stmt in template.body:
        walker.visit(stmt, default=Unknown())
    return WalkResult(usages=tuple(walker.usages))


class _Walker:
    """Carries the source path and accumulates usages across one node's tree."""

    def __init__(self, *, file_path: str | None) -> None:
        self.file = file_path
        self.usages: list[VarUsage] = []

    def visit(self, node: nodes.Node, *, default: UsageContext) -> None:
        """Dispatch one node. ``default`` is the context for a bare ``var`` call
        found directly here; operator nodes override it for their operands."""
        builtin = _call_name(node)
        if builtin in _VAR_BUILTINS:
            assert isinstance(node, nodes.Call)
            self._emit(node, builtin, default)
            return

        match node:
            case nodes.Output():
                for child in node.nodes:
                    self.visit(child, default=SqlLiteral())
            case nodes.If():
                self.visit(node.test, default=TruthyTest())
                self._visit_all((*node.body, *node.elif_, *node.else_))
            case nodes.For():
                # The iterable steers how the loop unrolls, so a var here is
                # control-flow, the same bucket as an if-test.
                self.visit(node.iter, default=TruthyTest())
                if node.test is not None:
                    self.visit(node.test, default=TruthyTest())
                self._visit_all((*node.body, *node.else_))
            case nodes.CondExpr():
                self.visit(node.test, default=TruthyTest())
                self.visit(node.expr1, default=Unknown())
                if node.expr2 is not None:
                    self.visit(node.expr2, default=Unknown())
            case nodes.And() | nodes.Or():
                self.visit(node.left, default=TruthyTest())
                self.visit(node.right, default=TruthyTest())
            case nodes.Not():
                self.visit(node.node, default=TruthyTest())
            case nodes.Compare():
                self._visit_compare(node)
            case nodes.Call():
                self._visit_call(node)
            case _ if isinstance(node, nodes.BinExpr):
                self._visit_arith(node)
            case _:
                for child in node.iter_child_nodes():
                    self.visit(child, default=Unknown())

    def _visit_all(self, children: Iterable[nodes.Node]) -> None:
        for child in children:
            self.visit(child, default=Unknown())

    def _visit_compare(self, cmp: nodes.Compare) -> None:
        # Single-operand comparisons are the classifiable shape; chained compares
        # (``a < x < b``) fall through to a generic recurse.
        if len(cmp.ops) == 1:
            ctx, var_call, builtin = self._classify_compare(cmp.expr, cmp.ops[0])
            if ctx is not None and var_call is not None and builtin is not None:
                self._emit(var_call, builtin, ctx)
                return
        self.visit(cmp.expr, default=Unknown())
        for operand in cmp.ops:
            self.visit(operand.expr, default=Unknown())

    def _classify_compare(
        self, left: nodes.Expr, operand: nodes.Operand
    ) -> tuple[UsageContext | None, nodes.Call | None, str | None]:
        """Classify ``left <op> operand.expr`` when one side is a var and the
        other a literal. Returns ``(None, None, None)`` to signal a generic recurse."""
        right = operand.expr
        left_var, right_var = _call_name(left), _call_name(right)
        op = operand.op

        if op in ("in", "notin"):
            # Membership reads as a domain only with the var on the left and a
            # literal collection on the right; ``'a' in var('x')`` is a different shape.
            if left_var in _VAR_BUILTINS and isinstance(left, nodes.Call):
                elements = _const_sequence(right)
                if elements is not None:
                    return InSet(elements), left, left_var
            return None, None, None

        var_call, builtin, literal = _split_var_and_literal(left, left_var, right, right_var)
        if var_call is None or builtin is None or isinstance(literal, _NoLiteral):
            return None, None, None

        if op in _COMPARISON_OP_VALUES:
            cmp_op = ComparisonOp(op)
            if left_var not in _VAR_BUILTINS:
                # Literal on the left: rewrite to the var-on-left orientation.
                cmp_op = _FLIPPED_COMPARISON[cmp_op]
            return Comparison(literal, cmp_op), var_call, builtin
        return None, None, None

    def _visit_arith(self, expr: nodes.BinExpr) -> None:
        op = _ARITH_OPS.get(type(expr))
        if op is None:
            # An unmapped BinExpr (e.g. string concat) is not arithmetic; recurse.
            for child in expr.iter_child_nodes():
                self.visit(child, default=Unknown())
            return
        other = _first_const(expr.left, expr.right)
        for operand in (expr.left, expr.right):
            builtin = _call_name(operand)
            if builtin in _VAR_BUILTINS and isinstance(operand, nodes.Call):
                self._emit(operand, builtin, Arithmetic(op, other))
            else:
                self.visit(operand, default=Unknown())

    def _visit_call(self, call: nodes.Call) -> None:
        # var / env_var are handled in ``visit`` before reaching here, so this is
        # always another call: its var arguments are macro arguments.
        callee = _call_name(call) or "<expr>"
        position = 0
        for arg in call.args:
            self._visit_arg(arg, callee, position)
            position += 1
        for keyword in call.kwargs:
            self._visit_arg(keyword.value, callee, position)
            position += 1

    def _visit_arg(self, arg: nodes.Expr, callee: str, position: int) -> None:
        builtin = _call_name(arg)
        if builtin in _VAR_BUILTINS and isinstance(arg, nodes.Call):
            self._emit(arg, builtin, MacroArg(macro=callee, position=position))
        else:
            self.visit(arg, default=Unknown())

    def _emit(self, call: nodes.Call, builtin: str, context: UsageContext) -> None:
        """Record one var usage, then walk its arguments past the name.

        Every classified emission funnels through here, so the inline-default walk
        lives in one place: ``var('x', <default>)`` can carry another var whatever
        the outer var's position. The default is walked even when the outer name is
        dynamic and its own usage skipped, the inner var being real either way.
        """
        name = _string_arg0(call)
        if name is not None:
            kind = VarKind.VAR if builtin == "var" else VarKind.ENV_VAR
            location = SourceLocation(file=self.file, line=call.lineno)
            self.usages.append(
                VarUsage(var_name=name, var_kind=kind, context=context, location=location)
            )
        for extra in call.args[1:]:
            self.visit(extra, default=Unknown())
        for keyword in call.kwargs:
            self.visit(keyword.value, default=Unknown())


# A sentinel distinguishing "no literal operand" from a literal whose value is
# ``None``. Used by the compare classifier.
class _NoLiteral:
    __slots__ = ()


_NO_LITERAL = _NoLiteral()


def _split_var_and_literal(
    left: nodes.Expr, left_var: str | None, right: nodes.Expr, right_var: str | None
) -> tuple[nodes.Call | None, str | None, LiteralValue | _NoLiteral]:
    """Pick the var-call side and the literal value from a two-sided comparison."""
    if left_var in _VAR_BUILTINS and isinstance(left, nodes.Call):
        value = _const_value(right)
        return left, left_var, value
    if right_var in _VAR_BUILTINS and isinstance(right, nodes.Call):
        value = _const_value(left)
        return right, right_var, value
    return None, None, _NO_LITERAL


def _call_name(node: nodes.Node) -> str | None:
    """The bare callee name of a ``Call`` (``var``, ``env_var``, ``my_macro``), or
    ``None`` when ``node`` is not a call to a bare name (a ``Getattr`` callee like
    ``adapter.dispatch`` returns ``None``)."""
    if not isinstance(node, nodes.Call):
        return None
    callee = node.node
    if isinstance(callee, nodes.Name):
        return callee.name
    return None


def _string_arg0(call: nodes.Call) -> str | None:
    """The first positional argument as a string literal (the var name), or ``None``."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, nodes.Const) and isinstance(first.value, str):
        return first.value
    return None


def _const_value(node: nodes.Node) -> LiteralValue | _NoLiteral:
    """The inline value of a ``Const`` if it is one of our literal types, else the
    no-literal sentinel."""
    if isinstance(node, nodes.Const) and isinstance(node.value, (bool, int, float, str)):
        return node.value
    return _NO_LITERAL


def _first_const(*candidates: nodes.Node) -> LiteralValue | None:
    """The first ``Const`` literal among ``candidates``, or ``None`` (the other
    arithmetic operand is itself a non-literal expression)."""
    for node in candidates:
        value = _const_value(node)
        if not isinstance(value, _NoLiteral):
            return value
    return None


def _const_sequence(node: nodes.Node) -> tuple[LiteralValue, ...] | None:
    """The literal elements of a ``List`` / ``Tuple`` when every element is a
    ``Const``, else ``None`` (a non-literal collection is not a finite domain)."""
    if not isinstance(node, (nodes.List, nodes.Tuple)):
        return None
    elements: list[LiteralValue] = []
    for item in node.items:
        value = _const_value(item)
        if isinstance(value, _NoLiteral):
            return None
        elements.append(value)
    return tuple(elements)
