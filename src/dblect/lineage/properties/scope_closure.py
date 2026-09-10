"""The scope-closure engine: one relation-algebra walk over a SQL scope's
qualified attributes and row-identity tokens, so a key or a functional
dependency is a closure question ("does K determine the output row") rather
than a literal-mint question. Tokens (:class:`RowToken`) give row identity
its own attribute, alongside a column's qualified identity (:class:`QCol`)
and a projected expression's (:class:`Computed`), so both keys and
dependencies fall out of one Armstrong closure over the same fact set instead
of two separately hand-mangled rename maps.

A resolved FROM/JOIN source contributes an :class:`Input`: its keys, its plain
dependencies, and its declared dependency instances, each in the source's own
output column names. :func:`scope_facts` mints qualified facts for one SELECT
or UNION, combining sources through the join algebra (predicates, join sides,
GROUP BY, DISTINCT), then projects that fact set back down to an ``Input`` in
the scope's own output names, closure-aware: a key or dependency survives
whenever the closure reaches it, however indirectly the columns that witness
it are named.

Every key and dependency the engine derives is a sound under-approximation: a
shape it cannot model yields nothing rather than a guess. ``Input.exact``
records whether that "yields nothing" was a proven absence or a give-up, so a
consumer wanting a negative claim (a key is not derivable, a grain does not
hold) can tell the two apart. The fragment the engine models exactly:

Exact: a FROM that is a table, CTE, or subquery; INNER, CROSS, LEFT, RIGHT,
FULL, SEMI, ANTI joins and the ``LEFT JOIN ... IS NULL`` idiom; a WHERE, ON,
HAVING, or QUALIFY whose every conjunctive leaf is a column equality, a
literal equality, the recognized ``ROW_NUMBER`` guard, or a row-local
comparison over this scope's own columns (``a > 1``, ``a <> b``, ``a IN (1,
2)``, ``a IS NULL``, ``LIKE``, ``BETWEEN``, and AND/OR/NOT over these); a
GROUP BY over bare columns; DISTINCT; UNION and UNION ALL; a projection of
bare columns, or expressions with no subquery and no window other than the
recognized ``ROW_NUMBER`` dedup.

Inexact: any subquery, EXISTS, IN-with-subquery, or window reference inside a
predicate; a projection containing a subquery or an unrecognized window; a
group target that is not a bare column; a star over a join; a FROM that is
anything else (a function, UNNEST, VALUES); INTERSECT and EXCEPT; and every
site where the engine gives up on a shape it does not recognize. A row-local
comparison is exact because such a filter cannot guarantee a collapse, so a
coarser key being absent after it is real evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Hashable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TypeVar, cast

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.graph import SourceRef
from dblect.lineage.predicate import Canon, CmpAtom, InAtom, atom_column, rename_atom
from dblect.sql import _sqlglot as sg
from dblect.sql import anti_join

# --- attributes ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QCol:
    """A column qualified by its FROM/JOIN alias, both case-folded."""

    alias: str
    column: str


@dataclass(frozen=True, slots=True)
class RowToken:
    """The identity of one resolved input's row, or (a reserved alias) the
    scope's own output row. Minted fresh per scope: a token never crosses a
    scope boundary, so two scopes' tokens never collide even when they reuse
    the same alias name."""

    alias: str


@dataclass(frozen=True, slots=True)
class Computed:
    """A projected output that is an expression rather than a bare column,
    named by its output alias."""

    name: str


Attr = QCol | RowToken | Computed

# The reserved row-token aliases a real FROM/JOIN alias never collides with
# (SQL aliases cannot contain ``$``).
_OUTPUT_TOKEN = RowToken("$out")
_GROUP_TOKEN = RowToken("$group")


# --- generic Armstrong closure ----------------------------------------------

_A = TypeVar("_A", bound=Hashable)


def closure(fds: Collection[tuple[frozenset[_A], _A]], attrs: frozenset[_A]) -> frozenset[_A]:
    """Attribute closure of ``attrs`` under ``fds``, Armstrong's axioms applied
    to a fixed point: sound and complete for functional-dependency entailment
    over any hashable attribute type.

    Shared by :func:`~dblect.lineage.properties.functional_dependency.determines`
    (over plain column names) and this engine (over qualified attributes), so a
    key or a dependency is decided by the same one implementation everywhere.
    """
    out = set(attrs)
    changed = True
    while changed:
        changed = False
        for determinant, dependent in fds:
            if dependent not in out and determinant <= out:
                out.add(dependent)
                changed = True
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class FD:
    """One dependency over a relation's output column names, in canonical
    single-dependent form (``X -> yz`` splits into ``X -> y`` and ``X -> z``).
    Names are case-folded to match the graph. An empty determinant says the
    dependent is constant over the whole relation, the strongest claim."""

    determinant: frozenset[str]
    dependent: str


@dataclass(frozen=True, slots=True)
class DeclaredFD:
    """A declared dependency's live instance in one relation.

    ``origin`` and ``declared`` name the axiom: the relation the dependency was
    declared about, and the dependency in that relation's column names.
    ``binding`` maps each declared column to the output column now carrying its
    value, one pair per declared column, maintained as the walk climbs. The
    per-column form is what a union merge compares: origin alone is not enough
    (one relation can declare two dependencies that arms rename onto the same
    output columns), and a renamed dependency is not either, because it forgets
    which declared column feeds which output, so arms crossing the columns of a
    multi-column determinant would look identical while running the axiom two
    different ways."""

    origin: SourceRef
    declared: FD
    binding: frozenset[tuple[str, str]]

    def __post_init__(self) -> None:
        cols = self.declared.determinant | {self.declared.dependent}
        if frozenset(c for c, _ in self.binding) != cols or len(self.binding) != len(cols):
            raise ValueError(f"binding must map exactly the declared columns {sorted(cols)}")

    @staticmethod
    def identity(origin: SourceRef, declared: FD) -> DeclaredFD:
        """The instance at its declaring relation: every column bound to itself."""
        cols = declared.determinant | {declared.dependent}
        return DeclaredFD(origin, declared, frozenset((c, c) for c in cols))

    @property
    def fd(self) -> FD:
        """The dependency under the current relation's output names."""
        current = dict(self.binding)
        return FD(
            frozenset(current[c] for c in self.declared.determinant),
            current[self.declared.dependent],
        )

    def renamed(self, lookup: Callable[[str], tuple[str, ...] | None]) -> DeclaredFD | None:
        """The instance carried through a projection: each bound column renamed to
        one of the output names ``lookup`` gives it, or ``None`` when a column does
        not survive. One output name per column suffices (copies of a column carry
        equal values), picked stably."""
        bound: set[tuple[str, str]] = set()
        for declared_col, current in self.binding:
            names = lookup(current)
            if not names:
                return None
            bound.add((declared_col, min(names)))
        return replace(self, binding=frozenset(bound))


# A candidate key: a set of case-folded column names.
Key = frozenset[str]


@dataclass(frozen=True, slots=True)
class ConditionalKey:
    """A candidate key that holds only over the rows matching ``predicate``.

    Grounded from a filtered declaration (a ``where``-scoped uniqueness test, or
    nullability's one-column NON_NULL claim riding the same carrier), captured
    rather than folded into a relation's unconditional keys until a scope's
    flowed row filter implies ``predicate``, at which point activation promotes
    it. ``predicate`` is the declaration's filter parsed to the engine's atoms,
    so it feeds :func:`~dblect.lineage.predicate.entails_atoms` directly.

    Carried through the relation algebra alongside the scope's other facts: an
    input's conditional key survives a scope when the closure shows its row is
    not multiplied (``closure({r_a})`` reaches the scope's output token) and
    both the key's columns and the predicate's columns rename to exactly one
    output name each.
    """

    key: Key
    predicate: frozenset[Canon]


@dataclass(frozen=True, slots=True)
class Input:
    """What a resolved FROM/JOIN source contributes to a scope: its candidate
    keys, its plain dependencies, its declared dependency instances, and its
    carried conditional keys, each in the source's own output column names. A
    base table resolves through the caller's ``base_resolve``; a CTE or inline
    subquery resolves to its own nested scope's projected facts, which are
    exactly this shape.

    ``exact`` says whether this input's own derivation passed only through
    operators the engine models exactly (see the module docstring for the
    fragment) and every input *it* read was itself exact. A scope that mints
    from an inexact input, or that itself contains an operator outside the
    fragment, is inexact; the bit is the identity ``True`` everywhere else, so
    a caller that never touches exactness sees no change in behavior.
    """

    keys: frozenset[Key] = frozenset()
    fds: frozenset[FD] = frozenset()
    declared: frozenset[DeclaredFD] = frozenset()
    conditional: frozenset[ConditionalKey] = frozenset()
    exact: bool = True


# The scope gave up: a shape outside the modelled fragment (see the module
# docstring).
_GIVE_UP: Input = Input(exact=False)

BaseResolve = Callable[[exp.Table], Input]

QFD = tuple[frozenset[Attr], Attr]

_CANDIDATE_CAP = 32


# --- one SELECT/UNION scope --------------------------------------------------


def scope_facts(
    node: Expr,
    *,
    cte_scope: Mapping[str, Input],
    base_resolve: BaseResolve,
    record: dict[int, Input] | None = None,
) -> Input:
    """The closure-derived ``Input`` a SELECT or UNION scope projects.

    Dispatches on shape: a SELECT mints the join algebra's qualified facts and
    projects them; a UNION keeps the declared instances every arm shares (see
    :func:`_union_facts`) plus a DISTINCT full-tuple key. INTERSECT, EXCEPT, and
    every other shape prove nothing, the conservative default.

    ``record``, when given, collects every SELECT/UNION scope's projected
    ``Input`` keyed by ``id(node)`` as the walk reaches it, so a caller (a
    detector needing a CTE's or inline subquery's own keys) can read an
    intermediate scope's facts without a second walk over the same tree.
    """
    if isinstance(node, exp.Select):
        result = _select_facts(node, cte_scope=cte_scope, base_resolve=base_resolve, record=record)
    elif isinstance(node, exp.Union):
        result = _union_facts(node, cte_scope=cte_scope, base_resolve=base_resolve, record=record)
    else:
        return _GIVE_UP  # INTERSECT, EXCEPT, or any other shape outside the modelled fragment
    if record is not None:
        record[id(node)] = result
    return result


def _with_scope(
    node: Expr,
    cte_scope: Mapping[str, Input],
    base_resolve: BaseResolve,
    record: dict[int, Input] | None,
) -> dict[str, Input]:
    return sg.with_scope(
        node,
        cte_scope,
        lambda n, s: scope_facts(n, cte_scope=s, base_resolve=base_resolve, record=record),
    )


def _resolve_source(
    node: Expr,
    *,
    cte_scope: Mapping[str, Input],
    base_resolve: BaseResolve,
    record: dict[int, Input] | None,
) -> tuple[str, Input] | None:
    if isinstance(node, exp.Table):
        alias = node.alias_or_name.lower()
        if node.name in cte_scope:
            return alias, cte_scope[node.name]
        return alias, base_resolve(node)
    if isinstance(node, exp.Subquery):
        inner = node.this
        alias = node.alias_or_name
        if not isinstance(inner, Expr) or not alias:
            return None
        return alias.lower(), scope_facts(
            inner, cte_scope=cte_scope, base_resolve=base_resolve, record=record
        )
    return None


def _qcol(col: exp.Column, *, default_alias: str) -> QCol:
    return QCol((sg.column_table(col) or default_alias).lower(), sg.column_name(col).lower())


def _predicate_qfds(predicate: Expr, *, default_alias: str) -> set[QFD]:
    """Facts a WHERE or an INNER join's ON contributes: an equality between two
    columns mints both directions, an equality pinning a column to a literal
    mints the constant, every other leaf mints nothing (a filter cannot break an
    FD or a key)."""
    out: set[QFD] = set()
    for leaf in sg.conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            continue
        left, right = leaf.this, leaf.expression
        left_col = (
            left if isinstance(left, exp.Column) and not isinstance(left.this, exp.Star) else None
        )
        right_col = (
            right
            if isinstance(right, exp.Column) and not isinstance(right.this, exp.Star)
            else None
        )
        if left_col is not None and right_col is not None:
            ql = _qcol(left_col, default_alias=default_alias)
            qr = _qcol(right_col, default_alias=default_alias)
            out.add((frozenset({ql}), qr))
            out.add((frozenset({qr}), ql))
        elif left_col is not None and isinstance(right, exp.Literal):
            out.add((frozenset(), _qcol(left_col, default_alias=default_alias)))
        elif right_col is not None and isinstance(left, exp.Literal):
            out.add((frozenset(), _qcol(right_col, default_alias=default_alias)))
    return out


# --- exactness ---------------------------------------------------------------
#
# Predicates get a stricter fragment than projections: a projection carries an
# opaque expression through by name, but a predicate decides which rows survive,
# so only recognized row-local comparisons count as understood.

_COMPARISONS: tuple[type[Expr], ...] = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _row_local(e: Expr) -> bool:
    """A bare column or a literal: the only operand shapes a row-local comparison
    allows, so no aggregate, subquery, or arbitrary expression sneaks through."""
    return isinstance(e, exp.Column | exp.Literal)


def _leaf_is_exact(leaf: Expr) -> bool:
    """Whether one non-boolean predicate leaf is a shape the fragment models
    exactly: a comparison, ``IN``, ``IS NULL``, ``LIKE``, or ``BETWEEN`` over bare
    columns and literals, or the ``ROW_NUMBER`` dedup guard."""
    guard_operand = sg.rank_one_guard_operand(leaf)
    if guard_operand is not None and sg.row_number_window(guard_operand) is not None:
        return True  # the inline ROW_NUMBER dedup guard
    if isinstance(leaf, _COMPARISONS):
        return _row_local(leaf.this) and _row_local(leaf.expression)
    if isinstance(leaf, exp.In):
        exprs = leaf.args.get("expressions")
        return (
            isinstance(leaf.this, exp.Column)
            and bool(exprs)
            and all(isinstance(x, exp.Literal) for x in cast("list[Expr]", exprs))
        )
    if isinstance(leaf, exp.Is):
        return isinstance(leaf.this, exp.Column) and isinstance(leaf.expression, exp.Null)
    if isinstance(leaf, exp.Like | exp.ILike):
        return isinstance(leaf.this, exp.Column) and isinstance(leaf.expression, exp.Literal)
    if isinstance(leaf, exp.Between):
        low, high = leaf.args.get("low"), leaf.args.get("high")
        return (
            isinstance(leaf.this, exp.Column)
            and isinstance(low, Expr)
            and isinstance(high, Expr)
            and _row_local(low)
            and _row_local(high)
        )
    return False


def _predicate_is_exact(e: Expr) -> bool:
    """Whether a WHERE, ON, HAVING, or QUALIFY predicate stays inside the fragment:
    AND/OR/NOT over leaves :func:`_leaf_is_exact` recognizes."""
    if isinstance(e, exp.Paren):
        return isinstance(e.this, Expr) and _predicate_is_exact(e.this)
    if isinstance(e, exp.And | exp.Or):
        left, right = e.this, e.expression
        return (
            isinstance(left, Expr)
            and isinstance(right, Expr)
            and _predicate_is_exact(left)
            and _predicate_is_exact(right)
        )
    if isinstance(e, exp.Not):
        return isinstance(e.this, Expr) and _predicate_is_exact(e.this)
    return _leaf_is_exact(e)


def _projection_is_exact(sel: exp.Select) -> bool:
    """Whether every projected expression stays inside the fragment: a star, a
    bare column, or an expression with no subquery and no window other than a
    ``ROW_NUMBER`` (the only window family the ``ROW_NUMBER`` dedup idiom needs,
    filtered locally or by an outer scope's guard on this select's own output)."""
    for proj in sel.expressions:
        if isinstance(proj, exp.Star):
            continue
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if not isinstance(inner, Expr):
            continue
        if sg.find_all_selects(inner):
            return False
        if any(sg.row_number_window(w) is None for w in sg.find_all_windows(inner)):
            return False
    return True


def _left_join_breakdown(
    on: Expr | None, *, alias: str, default_alias: str
) -> tuple[frozenset[str], frozenset[QCol]] | None:
    """A LEFT join's ON decoded around the joined-in side ``alias``: its own
    column names (``on_a``) and the qualified columns of everything it is
    equated to (``on_l``), or ``None`` when the ON is not a pure conjunction of
    column equalities each touching ``alias`` exactly once.

    Built leaf by leaf from every equality directly (unlike
    ``equality_cols_by_alias``, which requires one alias to appear in *every*
    leaf), so the accumulated side's columns can be spread across several of
    its own aliases (``ON l1.x = a.d1 AND l2.y = a.d2``): each leaf touches
    ``a`` exactly once, so both contribute to ``on_l``, even though neither
    ``l1`` nor ``l2`` is present in the other's leaf.
    """
    if on is None:
        return None
    leaves = sg.conjunctive_leaves(on)
    pairs = sg.equality_column_pairs(on)
    if len(pairs) != len(leaves):
        return None
    on_a: set[str] = set()
    on_l: set[QCol] = set()
    for left, right in pairs:
        left_qc = _qcol(left, default_alias=default_alias)
        right_qc = _qcol(right, default_alias=default_alias)
        left_is_a = left_qc.alias == alias
        right_is_a = right_qc.alias == alias
        if left_is_a == right_is_a:
            return None
        a_qc, other_qc = (left_qc, right_qc) if left_is_a else (right_qc, left_qc)
        on_a.add(a_qc.column)
        on_l.add(other_qc)
    return frozenset(on_a), frozenset(on_l)


def _within_nested_select(col: exp.Column, boundary: Expr) -> bool:
    """Whether ``col`` sits inside a SELECT nested somewhere below ``boundary``
    (that nested SELECT is its own scope, so its columns are not this one's)."""
    node: Expr | None = col.parent
    while node is not None and node is not boundary:
        if isinstance(node, exp.Select):
            return True
        node = node.parent
    return False


def _referenced_columns(sel: exp.Select, *, alias: str, from_alias: str) -> frozenset[str]:
    """Every column of ``alias`` referenced anywhere in ``sel``'s own scope
    (projections, ON, WHERE, GROUP BY, HAVING, QUALIFY, ORDER BY), never
    counting one that lies inside a nested SELECT (that is its own scope)."""
    clauses: list[Expr] = list(sel.expressions)
    clauses.extend(on for j in sg.joins_of(sel) if (on := sg.on_of(j)) is not None)
    where = sg.where_of(sel)
    if where is not None and isinstance(where.this, Expr):
        clauses.append(where.this)
    group = sg.group_of(sel)
    if group is not None:
        clauses.extend(group.expressions)
    having = sel.args.get("having")
    if isinstance(having, exp.Having) and isinstance(having.this, Expr):
        clauses.append(having.this)
    qualify = sg.qualify_of(sel)
    if qualify is not None and isinstance(qualify.this, Expr):
        clauses.append(qualify.this)
    order = sel.args.get("order")
    if isinstance(order, exp.Order):
        clauses.extend(order.expressions)

    cols: set[str] = set()
    for clause in clauses:
        for col in sg.find_columns(clause):
            if isinstance(col.this, exp.Star) or _within_nested_select(col, clause):
                continue
            if (sg.column_table(col) or from_alias).lower() == alias:
                cols.add(sg.column_name(col).lower())
    return frozenset(cols)


def _touches(fact: QFD, aliases: frozenset[str]) -> bool:
    determinant, dependent = fact
    for attr in (*determinant, dependent):
        if isinstance(attr, QCol) and attr.alias in aliases:
            return True
        if isinstance(attr, RowToken) and attr.alias in aliases:
            return True
    return False


def _is_reference_mint(fact: QFD, aliases: frozenset[str]) -> bool:
    """Whether ``fact`` is exactly a ``r_a -> (a, c)`` column-reference mint for
    some ``a`` in ``aliases``: a trivial "this row determines its own column"
    fact, never a key or a dependency claim, so it survives a FULL join's drop."""
    determinant, dependent = fact
    if len(determinant) != 1 or not isinstance(dependent, QCol):
        return False
    (only,) = determinant
    return isinstance(only, RowToken) and only.alias == dependent.alias and only.alias in aliases


def _select_facts(
    sel: exp.Select,
    *,
    cte_scope: Mapping[str, Input],
    base_resolve: BaseResolve,
    record: dict[int, Input] | None,
) -> Input:
    local = _with_scope(sel, cte_scope, base_resolve, record)

    from_ = sg.from_of(sel)
    if from_ is None or not isinstance(from_.this, Expr):
        return _GIVE_UP  # no FROM, or a shape sqlglot did not give an Expr for
    from_resolved = _resolve_source(
        from_.this, cte_scope=local, base_resolve=base_resolve, record=record
    )
    if from_resolved is None:
        return _GIVE_UP  # a FROM that is a function, UNNEST, VALUES, or other unmodelled shape
    from_alias = from_resolved[0]

    # A window computed inside the FROM subquery keys the from relation, so it joins the
    # from input's keys; one this SELECT computes keys the post-join rows (minted below).
    rn_postjoin, rn_fromside = _rownumber_facts(sel, from_node=from_.this, from_alias=from_alias)
    if rn_fromside:
        from_resolved = (
            from_alias,
            replace(from_resolved[1], keys=from_resolved[1].keys | rn_fromside),
        )

    joins = sg.joins_of(sel)
    join_sources: list[tuple[str, Input]] = []
    for j in joins:
        if not isinstance(j.this, Expr):
            return _GIVE_UP  # a join source sqlglot did not give an Expr for
        resolved = _resolve_source(
            j.this, cte_scope=local, base_resolve=base_resolve, record=record
        )
        if resolved is None:
            return _GIVE_UP  # a join source that is a function, UNNEST, VALUES, or the like
        join_sources.append(resolved)

    anti_arms = anti_join.anti_arm_ids(sel)
    facts: set[QFD] = set()
    # The subset of ``facts`` that are real value equalities (from an INNER
    # join's ON or the WHERE), as opposed to key mints, carried FDs, or a
    # mutual *functional* dependency: two columns can determine each other
    # (a declared bijection) without ever carrying the same value. Only value
    # equalities are safe grounds for the equivalence classes that let a name
    # rewrite fall back to a provably-equal column's output name.
    predicate_pairs: set[QFD] = set()
    declared_by_alias: dict[str, frozenset[DeclaredFD]] = {}
    conditional_by_alias: dict[str, frozenset[ConditionalKey]] = {}
    active_aliases: list[str] = []
    # Inexact when any minted input is, or when this scope's own WHERE/ON/HAVING/
    # QUALIFY/projection shape falls outside the fragment (checked below).
    scope_exact = True

    def mint(
        alias: str,
        inp: Input,
        *,
        keep_fds: bool = True,
        key_filter: Callable[[Key], bool] | None = None,
    ) -> None:
        nonlocal scope_exact
        scope_exact = scope_exact and inp.exact
        token = RowToken(alias)
        for key in inp.keys:
            if key_filter is not None and not key_filter(key):
                continue
            facts.add((frozenset(QCol(alias, c) for c in key), token))
        for c in _referenced_columns(sel, alias=alias, from_alias=from_alias):
            facts.add((frozenset({token}), QCol(alias, c)))
        if keep_fds:
            for fd in inp.fds:
                facts.add(
                    (frozenset(QCol(alias, d) for d in fd.determinant), QCol(alias, fd.dependent))
                )
            declared_by_alias[alias] = inp.declared
            conditional_by_alias[alias] = inp.conditional
        active_aliases.append(alias)

    mint(from_alias, from_resolved[1])

    for (alias, inp), j in zip(join_sources, joins, strict=True):
        side = sg.join_side_of(j)
        on_expr = sg.on_of(j)
        if on_expr is not None and not _predicate_is_exact(on_expr):
            scope_exact = False
        if side in (sg.JoinSide.SEMI, sg.JoinSide.ANTI) or id(j) in anti_arms:
            continue  # a filters the accumulated side; mint nothing, exclude from r_out
        if side is sg.JoinSide.LEFT:
            breakdown = _left_join_breakdown(sg.on_of(j), alias=alias, default_alias=from_alias)
            on_a: frozenset[str]
            on_l: frozenset[QCol]
            on_a, on_l = breakdown if breakdown is not None else (frozenset(), frozenset())
            clean = breakdown is not None and bool(on_l)
            mint(
                alias,
                inp,
                keep_fds=False,
                key_filter=(lambda k, on_a=on_a: k <= on_a) if clean else (lambda _k: False),
            )
            if clean:
                for c in on_a:
                    facts.add((on_l, QCol(alias, c)))
        elif side is sg.JoinSide.RIGHT:
            accumulated = frozenset(active_aliases)
            facts = {f for f in facts if not _touches(f, accumulated)}
            predicate_pairs = {f for f in predicate_pairs if not _touches(f, accumulated)}
            for a2 in accumulated:
                declared_by_alias.pop(a2, None)
                conditional_by_alias.pop(a2, None)
            mint(alias, inp)
        elif side is sg.JoinSide.FULL:
            accumulated = frozenset(active_aliases)
            facts = {
                f
                for f in facts
                if not (_touches(f, accumulated) and not _is_reference_mint(f, accumulated))
            }
            predicate_pairs = {f for f in predicate_pairs if not _touches(f, accumulated)}
            for a2 in accumulated:
                declared_by_alias.pop(a2, None)
                conditional_by_alias.pop(a2, None)
            mint(alias, inp, keep_fds=False, key_filter=lambda _k: False)
        else:  # INNER, CROSS
            mint(alias, inp)
            if side is sg.JoinSide.INNER:
                on = sg.on_of(j)
                if on is not None:
                    minted = _predicate_qfds(on, default_alias=from_alias)
                    facts |= minted
                    predicate_pairs |= minted

    where = sg.where_of(sel)
    if where is not None and isinstance(where.this, Expr):
        if not _predicate_is_exact(where.this):
            scope_exact = False
        minted = _predicate_qfds(where.this, default_alias=from_alias)
        facts |= minted
        predicate_pairs |= minted

    qualify = sg.qualify_of(sel)
    if (
        qualify is not None
        and isinstance(qualify.this, Expr)
        and not _predicate_is_exact(qualify.this)
    ):
        scope_exact = False

    having = sel.args.get("having")
    if (
        isinstance(having, exp.Having)
        and isinstance(having.this, Expr)
        and not _predicate_is_exact(having.this)
    ):
        scope_exact = False

    if active_aliases:
        facts.add((frozenset(RowToken(a) for a in active_aliases), _OUTPUT_TOKEN))
        for a in active_aliases:
            facts.add((frozenset({_OUTPUT_TOKEN}), RowToken(a)))
    r_out = _OUTPUT_TOKEN

    group = sg.group_of(sel)
    grouped = group is not None and bool(group.expressions)
    extra_candidates: list[frozenset[Attr]] = []
    if grouped:
        g_cols = _group_qcols(sel, from_alias=from_alias)
        if g_cols is None:
            return _GIVE_UP  # a GROUP BY target that is not a bare column
        g_attrs: frozenset[Attr] = frozenset(g_cols)
        facts, declared_by_alias, r_out = _apply_group_by(facts, declared_by_alias, g_attrs)
        candidate_aliases: list[str] = []
        extra_candidates.append(g_attrs)
    else:
        candidate_aliases = list(active_aliases)

    for p in rn_postjoin:
        facts.add((p, r_out))
        extra_candidates.append(p)

    proj = _build_projection(sel, from_alias=from_alias, active_aliases=active_aliases)
    if proj.blocked:
        return _GIVE_UP  # a star projected over more than one input: an ambiguous output universe
    if not _projection_is_exact(sel):
        scope_exact = False

    for name in proj.computed:
        facts.add((frozenset({r_out}), Computed(name)))

    if sel.args.get("distinct") is not None:
        distinct_attrs: frozenset[Attr] = frozenset(proj.named) | frozenset(
            Computed(c) for c in proj.computed
        )
        if distinct_attrs:
            facts.add((distinct_attrs, r_out))
            extra_candidates.append(distinct_attrs)

    classes = _equivalence_classes(tuple(predicate_pairs))
    declared_final = _rename_declared(declared_by_alias, classes, proj)
    result = _project(
        facts,
        declared_final,
        candidate_aliases,
        extra_candidates,
        r_out=r_out,
        proj=proj,
        classes=classes,
        conditional_by_alias=conditional_by_alias,
    )
    return replace(result, exact=scope_exact)


def _rename_declared(
    declared_by_alias: Mapping[str, frozenset[DeclaredFD]],
    classes: Mapping[Attr, frozenset[Attr]],
    proj: _Projection,
) -> frozenset[DeclaredFD]:
    """Each alias's declared instances renamed through the scope's own
    projection and equivalence classes, matching :func:`_project`'s plain-FD
    rewrite so an instance's ``fd`` stays a member of that ``FDSet``. An
    instance whose columns do not all survive drops with them."""

    def lookup(cur: str, alias: str) -> tuple[str, ...] | None:
        name = _output_name(QCol(alias, cur), classes, proj)
        return None if name is None else (name,)

    out: set[DeclaredFD] = set()
    for alias, instances in declared_by_alias.items():
        for inst in instances:
            carried = inst.renamed(lambda cur, alias=alias: lookup(cur, alias))
            if carried is not None:
                out.add(carried)
    return frozenset(out)


def _qcol_set(exprs: Collection[Expr], *, default_alias: str) -> frozenset[QCol] | None:
    """A set of qualified columns from a list of expressions, or ``None`` for a shape we cannot
    name outright: an empty list, or any entry that is not a bare column (an expression, a
    star). Shared by the GROUP BY key and a ``ROW_NUMBER`` partition, both of which need exactly
    this reading of a column list."""
    if not exprs:
        return None
    out: set[QCol] = set()
    for e in exprs:
        if not isinstance(e, exp.Column) or isinstance(e.this, exp.Star):
            return None
        out.add(_qcol(e, default_alias=default_alias))
    return frozenset(out)


def _group_qcols(sel: exp.Select, *, from_alias: str) -> frozenset[QCol] | None:
    """The GROUP BY key as qualified columns, or ``None`` for a shape we cannot
    name (an expression group key), which proves nothing scope-wide."""
    targets = [t.grounded_expression for t in sg.group_targets(sel)]
    return _qcol_set(targets, default_alias=from_alias)


def _projection_by_name(sel: exp.Select, name: str) -> Expr | None:
    """The expression projected under output name ``name`` (case-folded), or ``None`` if no
    projection of ``sel`` produces it (a star, or no matching alias/bare column)."""
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias) and proj.alias_or_name.lower() == name:
            inner = proj.this
            return inner if isinstance(inner, Expr) else None
        if (
            isinstance(proj, exp.Column)
            and not isinstance(proj.this, exp.Star)
            and sg.column_name(proj).lower() == name
        ):
            return proj
    return None


def _rownumber_facts(
    sel: exp.Select, *, from_node: Expr, from_alias: str
) -> tuple[frozenset[frozenset[QCol]], frozenset[Key]]:
    """Keys the ``ROW_NUMBER() ... = 1`` dedup idiom introduces, split by where the window is
    evaluated: ``(post_join, from_side)``.

    A relation filtered to the first row per ``PARTITION BY c1..cn`` keeps one row per
    partition, so ``{c1..cn}`` is a candidate key. The dedup guard lives in ``QUALIFY`` or an
    outer ``WHERE``; the window is inline in that guard (``QUALIFY ROW_NUMBER() OVER (...) = 1``),
    named by a projection of this SELECT (``QUALIFY rn = 1``), or named by a projection of the
    FROM subquery the guard filters (``FROM (SELECT ..., ROW_NUMBER() ... AS rn FROM t) WHERE rn
    = 1``). The first two see the post-join rows and land in ``post_join``, qualified to this
    scope's own aliases; the subquery window is computed before the outer join, so its key is
    only a key of the from relation and lands in ``from_side``, in the from relation's own
    output names, for the caller to fold into the from input's keys and carry through join
    preservation. A window projected but never filtered, and a partition-less window (the empty
    key), ground nothing.
    """
    guards: list[Expr] = []
    qualify = sg.qualify_of(sel)
    if qualify is not None and isinstance(qualify.this, Expr):
        guards.extend(sg.conjunctive_leaves(qualify.this))
    where = sg.where_of(sel)
    if where is not None and isinstance(where.this, Expr):
        guards.extend(sg.conjunctive_leaves(where.this))

    post_join: set[frozenset[QCol]] = set()
    from_side: set[Key] = set()
    for leaf in guards:
        operand = sg.rank_one_guard_operand(leaf)
        if operand is None:
            continue
        inline = sg.row_number_window(operand)
        if inline is not None:
            key = _qcol_set(sg.partition_of(inline), default_alias=from_alias)
            if key is not None:
                post_join.add(key)
        elif isinstance(operand, exp.Column):
            own = _same_select_rownumber_key(operand, sel=sel, from_alias=from_alias)
            if own is not None:
                post_join.add(own)
            else:
                sub = _subquery_rownumber_key(operand, from_node=from_node, from_alias=from_alias)
                if sub is not None:
                    from_side.add(sub)
    return frozenset(post_join), frozenset(from_side)


def _same_select_rownumber_key(
    ref: exp.Column, *, sel: exp.Select, from_alias: str
) -> frozenset[QCol] | None:
    """Partition key of a ``ROW_NUMBER()`` window this SELECT projects as ``ref`` (``QUALIFY rn =
    1`` naming a select alias). The window is evaluated over this scope's own (post-join) rows, so
    its partition qualifies to ``from_alias``. ``None`` if ``ref`` is qualified or names no such
    window."""
    if sg.column_table(ref) is not None:
        return None
    name = sg.column_name(ref).lower()
    window_expr = _projection_by_name(sel, name)
    window = sg.row_number_window(window_expr) if window_expr is not None else None
    return _qcol_set(sg.partition_of(window), default_alias=from_alias) if window else None


def _subquery_rownumber_key(ref: exp.Column, *, from_node: Expr, from_alias: str) -> Key | None:
    """Partition key of a ``ROW_NUMBER()`` window the FROM subquery projects as ``ref``, filtered
    by an outer guard, in the subquery's own output names. The window runs inside the subquery,
    so the key is only a key of the from relation. ``None`` if ``from_node`` is not that
    subquery, ``ref`` names no such window, or a partition column it does not expose."""
    qualifier = sg.column_table(ref)
    if not isinstance(from_node, exp.Subquery) or qualifier not in (None, from_alias):
        return None
    inner = from_node.this
    if not isinstance(inner, exp.Select):
        return None
    name = sg.column_name(ref).lower()
    window_expr = _projection_by_name(inner, name)
    window = sg.row_number_window(window_expr) if window_expr is not None else None
    return _subquery_partition_key(window, inner=inner) if window else None


def _subquery_partition_key(window: exp.Window, *, inner: exp.Select) -> Key | None:
    """A subquery window's partition columns, lifted to the subquery's own output names by
    mapping each through ``inner``'s projection. ``None`` if a partition column is not a bare
    column or ``inner`` does not project it, so no output name exists for it and no key holds."""
    inner_from = sg.from_of(inner)
    if inner_from is None or not isinstance(inner_from.this, exp.Table | exp.Subquery):
        return None
    inner_alias = inner_from.this.alias_or_name.lower()
    inner_proj = _build_projection(inner, from_alias=inner_alias, active_aliases=(inner_alias,))
    partition = sg.partition_of(window)
    if not partition:
        return None
    names: set[str] = set()
    for p in partition:
        if not isinstance(p, exp.Column):
            return None
        direct = _direct_name(_qcol(p, default_alias=inner_alias), inner_proj)
        if not direct:
            return None
        names.add(min(direct))
    return frozenset(names)


def _apply_group_by(
    facts: set[QFD],
    declared_by_alias: Mapping[str, frozenset[DeclaredFD]],
    g_attrs: frozenset[Attr],
) -> tuple[set[QFD], dict[str, frozenset[DeclaredFD]], RowToken]:
    """Replace the scope's fact set with the group-by algebra: FDs among the
    group columns, constants for pinned group members, and a fresh token
    ``r_g`` the group key determines and that determines each group column.
    GROUP BY discards input row identity, so nothing else survives; a declared
    instance survives only when every one of its (qualified) columns lies
    within the group key."""
    pairs = tuple(facts)
    within: set[QFD] = {(det, dep) for det, dep in pairs if dep in g_attrs and det <= g_attrs}
    for g in g_attrs:
        for y in closure(pairs, frozenset({g})):
            if y in g_attrs and y != g:
                within.add((frozenset({g}), y))
    pinned_g: set[QFD] = {(frozenset(), dep) for det, dep in pairs if not det and dep in g_attrs}
    new_facts: set[QFD] = within | pinned_g
    new_facts.add((g_attrs, _GROUP_TOKEN))
    for g in g_attrs:
        new_facts.add((frozenset({_GROUP_TOKEN}), g))

    new_declared_by_alias: dict[str, frozenset[DeclaredFD]] = {}
    for alias, instances in declared_by_alias.items():
        kept = {
            inst
            for inst in instances
            if frozenset(QCol(alias, c) for c in inst.fd.determinant) <= g_attrs
            and QCol(alias, inst.fd.dependent) in g_attrs
        }
        if kept:
            new_declared_by_alias[alias] = frozenset(kept)
    return new_facts, new_declared_by_alias, _GROUP_TOKEN


def _union_facts(
    u: exp.Union,
    *,
    cte_scope: Mapping[str, Input],
    base_resolve: BaseResolve,
    record: dict[int, Input] | None,
) -> Input:
    """The union merge: keep exactly the declared instances every arm shares
    after positional alignment (a union adds only cross pairs, so an arm-local
    derived witness dies while a shared declared instance's grounding covers
    them and survives). The DISTINCT key is computed independently, from the
    first arm alone, so it survives even when nothing is shared to align.
    Conditional keys drop: the arms may carry different predicates, so no
    single carried key is sound across the merge."""
    keys = _union_key(u)
    arms = sg.union_arms(u)
    if arms is None:
        return Input(keys, exact=False)  # an unflattened or otherwise unreadable set-op chain
    names = [_positional_outputs(arm) for arm in arms]
    first = names[0] if names else None
    if first is None or any(n is None or len(n) != len(first) for n in names):
        return Input(keys, exact=False)  # an arm's output columns can't be read positionally
    local = _with_scope(u, cte_scope, base_resolve, record)
    shared: frozenset[DeclaredFD] | None = None
    arms_exact = True
    for arm_names, arm in zip(names, arms, strict=True):
        assert arm_names is not None
        rename = {src: (dst,) for src, dst in zip(arm_names, first, strict=True)}
        arm_input = scope_facts(arm, cte_scope=local, base_resolve=base_resolve, record=record)
        arms_exact = arms_exact and arm_input.exact
        aligned = _remap_declared(arm_input.declared, rename)
        shared = aligned if shared is None else shared & aligned
    assert shared is not None
    return Input(keys, frozenset(inst.fd for inst in shared), shared, exact=arms_exact)


def _union_key(u: exp.Union) -> frozenset[Key]:
    """The DISTINCT full-output-tuple key, read off the union's own first arm.
    A star anywhere leaves the full tuple unnamed, so it voids the key rather
    than being skipped: DISTINCT dedups every column, and a named subset is
    not a key of that wider tuple. UNION ALL, or a first arm that is itself a
    nested set operation (an unflattened chain), mints no key."""
    if not bool(u.args.get("distinct")) or not isinstance(u.this, exp.Select):
        return frozenset()
    names: list[str] = []
    for proj in u.this.expressions:
        if isinstance(proj, exp.Star):
            return frozenset()
        if isinstance(proj, exp.Alias):
            names.append(proj.alias_or_name.lower())
        elif isinstance(proj, exp.Column):
            if isinstance(proj.this, exp.Star):
                return frozenset()
            names.append(sg.column_name(proj).lower())
    return frozenset({frozenset(names)}) if names else frozenset()


def _positional_outputs(arm: Expr) -> tuple[str, ...] | None:
    """An arm's output column names in projection order, or ``None`` when they
    cannot be lined up positionally (a star, a duplicated name, not a SELECT)."""
    if not isinstance(arm, exp.Select):
        return None
    out: list[str] = []
    for proj in arm.expressions:
        if isinstance(proj, exp.Star):
            return None
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
            return None
        out.append(proj.alias_or_name.lower())
    if len(set(out)) != len(out):
        return None
    return tuple(out)


def _remap_declared(
    instances: frozenset[DeclaredFD], rename: Mapping[str, tuple[str, ...]]
) -> frozenset[DeclaredFD]:
    """Rename each instance's binding, keeping its grounding; an instance whose
    columns do not all survive drops with them."""
    return frozenset(
        carried for inst in instances if (carried := inst.renamed(rename.get)) is not None
    )


# --- closure-aware projection -----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Projection:
    """The output-name mapping a SELECT projection induces. ``named`` maps a
    bare-column projection's qualified source to the output name(s) it appears
    under; ``computed`` are the names of expression projections; ``star_alias``
    is set when a star appears over exactly one contributing input (the
    identity rename); ``blocked`` when a star appears over more than one (a
    join's output universe is then ambiguous, so the whole scope proves
    nothing)."""

    named: Mapping[QCol, tuple[str, ...]]
    computed: frozenset[str]
    star_alias: str | None
    blocked: bool


def _build_projection(
    sel: exp.Select, *, from_alias: str, active_aliases: Sequence[str]
) -> _Projection:
    named: dict[QCol, list[str]] = {}
    computed: set[str] = set()
    has_star = False
    for proj in sel.expressions:
        if isinstance(proj, exp.Star):
            has_star = True
            continue
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Column):
            if isinstance(inner.this, exp.Star):
                has_star = True
                continue
            qc = QCol((sg.column_table(inner) or from_alias).lower(), sg.column_name(inner).lower())
            named.setdefault(qc, []).append(proj.alias_or_name.lower())
            continue
        name = proj.alias_or_name
        if name:
            computed.add(name.lower())
    star_alias = active_aliases[0] if (has_star and len(active_aliases) == 1) else None
    blocked = has_star and len(active_aliases) != 1
    return _Projection(
        named={qc: tuple(ns) for qc, ns in named.items()},
        computed=frozenset(computed),
        star_alias=star_alias,
        blocked=blocked,
    )


def _direct_name(attr: Attr, proj: _Projection) -> tuple[str, ...]:
    if isinstance(attr, Computed):
        return (attr.name,) if attr.name in proj.computed else ()
    if isinstance(attr, QCol):
        names = proj.named.get(attr, ())
        if names:
            return names
        if proj.star_alias is not None and attr.alias == proj.star_alias:
            return (attr.column,)
        return ()
    return ()


def _attr_sort_key(attr: Attr) -> tuple[int, str, str]:
    if isinstance(attr, QCol):
        return (0, attr.alias, attr.column)
    if isinstance(attr, RowToken):
        return (1, attr.alias, "")
    return (2, attr.name, "")


def _attr_set_sort_key(attrs: frozenset[Attr]) -> tuple[tuple[int, str, str], ...]:
    return tuple(sorted(_attr_sort_key(a) for a in attrs))


def _equivalence_classes(pairs: Sequence[QFD]) -> dict[Attr, frozenset[Attr]]:
    """Attributes provably carrying the same value: symmetric single-attribute
    pairs (``a -> b`` and ``b -> a`` both present) partition into classes.
    ``pairs`` must be value-equality mints only, never the full fact set: a
    mutual *functional* dependency (a declared bijection) is not a value
    equality and must not merge two columns that can honestly differ."""
    singles = {(next(iter(det)), dep) for det, dep in pairs if len(det) == 1}
    symmetric = {(a, b) for (a, b) in singles if (b, a) in singles}

    parent: dict[Attr, Attr] = {}

    def find(x: Attr) -> Attr:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:
            parent[x], x = root, parent.get(x, root)
        return root

    for a, b in symmetric:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    members: dict[Attr, set[Attr]] = {}
    for a, b in symmetric:
        members.setdefault(find(a), set()).update({a, b})
    classes: dict[Attr, frozenset[Attr]] = {}
    for group in members.values():
        frozen = frozenset(group)
        for a in group:
            classes[a] = frozen
    return classes


def _output_name(
    attr: Attr, classes: Mapping[Attr, frozenset[Attr]], proj: _Projection
) -> str | None:
    members = classes.get(attr, frozenset({attr}))
    names = [n for m in members for n in _direct_name(m, proj)]
    return min(names) if names else None


def _rewrite(
    attrs: frozenset[Attr], classes: Mapping[Attr, frozenset[Attr]], proj: _Projection
) -> frozenset[str] | None:
    out: set[str] = set()
    for attr in attrs:
        name = _output_name(attr, classes, proj)
        if name is None:
            return None
        out.add(name)
    return frozenset(out)


def _cross_product_keys(
    per_alias: Mapping[str, Sequence[frozenset[Attr]]], aliases: Sequence[str]
) -> list[frozenset[Attr]]:
    """One key-determinant choice per non-semi input, unioned; sorted order
    keeps the result deterministic and the cap bounds the combinatorics. An
    input with no known key rules out every combination."""
    if not aliases:
        return []
    combos: list[frozenset[Attr]] = [frozenset()]
    for alias in sorted(aliases):
        options = sorted(per_alias.get(alias, ()), key=_attr_set_sort_key)
        if not options:
            return []
        combos = [base | opt for base in combos for opt in options][:_CANDIDATE_CAP]
    return combos


def _validate_and_minimize(
    candidate: frozenset[Attr], pairs: Sequence[QFD], r_out: RowToken
) -> frozenset[Attr] | None:
    """``candidate`` kept only if its closure reaches ``r_out``, then minimized
    by dropping attributes (sorted, for a deterministic result) while the
    closure still reaches it. Every emitted key is checked against the closure
    before rewriting, so the candidate search is sound regardless of which
    candidates it happens to try. A candidate whose columns are all pinned
    (``WHERE id = 5`` on a relation keyed on ``id``) would minimize to nothing;
    no consumer reads an empty key, so it keeps its unminimized form."""
    if r_out not in closure(pairs, candidate):
        return None
    kept = set(candidate)
    for attr in sorted(kept, key=_attr_sort_key):
        trial = frozenset(kept - {attr})
        if r_out in closure(pairs, trial):
            kept.discard(attr)
    return frozenset(kept) if kept else candidate


def _carry_predicate(
    predicate: frozenset[Canon],
    *,
    alias: str,
    classes: Mapping[Attr, frozenset[Attr]],
    proj: _Projection,
) -> frozenset[Canon] | None:
    """Rename a conditional key's predicate through the scope's projection, or ``None`` if any
    atom cannot be carried (its column does not survive, or the atom is opaque). All-or-nothing:
    dropping one atom would weaken the predicate and let the key activate too readily, so the
    whole conditional key drops instead.

    A bare ``*`` over ``alias`` alone passes every column through under its own name, opaque
    atoms included (an opaque atom has no column to look up, but its meaning is untouched by an
    identity rename), so that case short-circuits the per-atom walk."""
    if proj.star_alias == alias:
        return predicate
    out: set[Canon] = set()
    for atom in predicate:
        if not isinstance(atom, CmpAtom | InAtom):
            return None  # opaque: its column is unknown, so it cannot be tracked
        col = atom_column(atom)
        if col is None:
            return None
        name = _output_name(QCol(alias, col), classes, proj)
        if name is None:
            return None
        out.add(rename_atom(atom, name))
    return frozenset(out)


def _carry_conditional(
    conditional_by_alias: Mapping[str, frozenset[ConditionalKey]],
    pairs: Sequence[QFD],
    r_out: RowToken,
    classes: Mapping[Attr, frozenset[Attr]],
    proj: _Projection,
) -> frozenset[ConditionalKey]:
    """Conditional keys carried through this scope, input by input.

    An input's row is not multiplied into the output exactly when its row token's closure
    reaches ``r_out`` (the same test a candidate key answers), so that is the soundness bar a
    conditional key must clear too; a GROUP BY, a UNION, or a fanning-out join all fail it,
    since none of them let a single input row determine the output row. The key's own columns
    and the predicate's columns then have to survive the projection under one output name each,
    reusing the same lookup a declared dependency renames through.
    """
    out: set[ConditionalKey] = set()
    for alias, cks in conditional_by_alias.items():
        if not cks or r_out not in closure(pairs, frozenset({RowToken(alias)})):
            continue
        for ck in cks:
            mapped_key = _rewrite(frozenset(QCol(alias, c) for c in ck.key), classes, proj)
            if mapped_key is None:
                continue
            mapped_predicate = _carry_predicate(
                ck.predicate, alias=alias, classes=classes, proj=proj
            )
            if mapped_predicate is None:
                continue
            out.add(ConditionalKey(mapped_key, mapped_predicate))
    return frozenset(out)


def _project(
    facts: set[QFD],
    declared: frozenset[DeclaredFD],
    candidate_aliases: Sequence[str],
    extra_candidates: Sequence[frozenset[Attr]],
    *,
    r_out: RowToken,
    proj: _Projection,
    classes: Mapping[Attr, frozenset[Attr]],
    conditional_by_alias: Mapping[str, frozenset[ConditionalKey]],
) -> Input:
    pairs = tuple(facts)

    per_alias_dets: dict[str, list[frozenset[Attr]]] = {}
    for det, dep in pairs:
        if (
            isinstance(dep, RowToken)
            and dep.alias in candidate_aliases
            and all(isinstance(a, QCol) for a in det)
        ):
            per_alias_dets.setdefault(dep.alias, []).append(det)
    candidates = _cross_product_keys(per_alias_dets, candidate_aliases) + list(extra_candidates)

    keys: set[Key] = set()
    for candidate in candidates:
        minimized = _validate_and_minimize(candidate, pairs, r_out)
        if minimized is None:
            continue
        rewritten = _rewrite(minimized, classes, proj)
        if rewritten is not None:
            keys.add(rewritten)

    pinned = frozenset(dep for det, dep in pairs if not det)
    fds: set[FD] = set()
    for det, dep in pairs:
        if isinstance(dep, RowToken):
            continue
        det_names = _rewrite(det - pinned, classes, proj)
        if det_names is None:
            continue
        dep_name = _output_name(dep, classes, proj)
        if dep_name is None or dep_name in det_names:
            continue  # reflexive (X -> x): true by Armstrong reflexivity, adds no closure power
        fds.add(FD(det_names, dep_name))

    output_names = frozenset(n for ns in proj.named.values() for n in ns) | proj.computed
    for key in keys:
        for c in output_names - key:
            fds.add(FD(key, c))

    # Rewriting always picks one representative name per class, so a class
    # with several distinct output names (an ON equality projected from both
    # sides) loses the mutual fact between the names it didn't pick. Mint it
    # directly.
    for group in {frozenset(g) for g in classes.values()}:
        names = frozenset(n for m in group for n in _direct_name(m, proj))
        for n1 in names:
            for n2 in names:
                if n1 != n2:
                    fds.add(FD(frozenset({n1}), n2))

    conditional = _carry_conditional(conditional_by_alias, pairs, r_out, classes, proj)
    return Input(frozenset(keys), frozenset(fds), declared, conditional)
