"""The scope-closure engine: one relation-algebra walk that reasons over a SQL
scope's qualified attributes with row-identity tokens, so a key or a functional
dependency is a closure question rather than a literal-mint question.

Two hand-written walks (the old FD walk in ``functional_dependency.py``, the old
key walk in ``uniqueness.py``) each held half of one algebra, and both lost
derived facts at the projection boundary: an FD whose determinant column was not
itself projected died even when an equal projected column existed, and a key
qualified to one join side died when the output selected the other side's copy
of the same column. This engine fixes both by working over qualified attributes
(:class:`QCol`, a column qualified by its FROM/JOIN alias; :class:`RowToken`, the
identity of one input's row or the scope's own output; :class:`Computed`, a
projected expression) and asking the closure question directly: "does K
determine the output row" is exactly what "K is a key" means, and the same
closure decides which functional dependencies survive a projection.

A resolved FROM/JOIN source contributes an :class:`Input`: its keys, its plain
dependencies, and its declared dependency instances (:class:`~dblect.lineage.
properties.functional_dependency.DeclaredFD`), each in the source's own output
column names. :func:`scope_facts` mints qualified facts for one SELECT or UNION,
combining sources through the join algebra (predicates, join sides, GROUP BY,
DISTINCT), then the projection step reduces that qualified fact set back down to an
``Input`` in the scope's own output names, closure-aware: a key or dependency
survives whenever the closure reaches it, however indirectly the columns that
witness it are named.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Hashable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TypeVar, cast

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.graph import SourceRef
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


# --- the FD value types (moved from functional_dependency.py) --------------
#
# These describe one dependency over a relation's OWN output column names
# (unqualified), the shape every ``Input`` carries and the shape the engine's
# projection step produces. They live here, not in functional_dependency.py,
# because the engine (and, from its uniqueness reducer on, the key walk too)
# builds and carries them directly; functional_dependency.py imports them back
# for its lattice, its ``FDSet``, and its public API.


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


# A candidate key is a set of case-folded column names, matching
# ``dblect.lineage.properties.uniqueness.Key`` (kept as a plain alias here, not
# imported, so this module carries no dependency on the uniqueness property;
# uniqueness's own ``Key`` and this one are structurally the same type).
Key = frozenset[str]


@dataclass(frozen=True, slots=True)
class Input:
    """What a resolved FROM/JOIN source contributes to a scope: its candidate
    keys, its plain dependencies, and its declared dependency instances, each in
    the source's own output column names. A base table resolves through the
    caller's ``base_resolve``; a CTE or inline subquery resolves to its own
    nested scope's projected facts, which are exactly this shape."""

    keys: frozenset[Key] = frozenset()
    fds: frozenset[FD] = frozenset()
    declared: frozenset[DeclaredFD] = frozenset()


EMPTY_INPUT: Input = Input()

BaseResolve = Callable[[exp.Table], Input]

QFD = tuple[frozenset[Attr], Attr]

_CANDIDATE_CAP = 32


# --- one SELECT/UNION scope --------------------------------------------------


def scope_facts(node: Expr, *, cte_scope: Mapping[str, Input], base_resolve: BaseResolve) -> Input:
    """The closure-derived ``Input`` a SELECT or UNION scope projects.

    Dispatches on shape: a SELECT mints the join algebra's qualified facts and
    projects them; a UNION keeps the declared instances every arm shares (see
    :func:`_union_facts`) plus a DISTINCT full-tuple key. INTERSECT, EXCEPT, and
    every other shape prove nothing, the conservative default.
    """
    if isinstance(node, exp.Select):
        return _select_facts(node, cte_scope=cte_scope, base_resolve=base_resolve)
    if isinstance(node, exp.Union):
        return _union_facts(node, cte_scope=cte_scope, base_resolve=base_resolve)
    return EMPTY_INPUT


def _with_scope(
    node: Expr, cte_scope: Mapping[str, Input], base_resolve: BaseResolve
) -> dict[str, Input]:
    return sg.with_scope(
        node, cte_scope, lambda n, s: scope_facts(n, cte_scope=s, base_resolve=base_resolve)
    )


def _resolve_source(
    node: Expr, *, cte_scope: Mapping[str, Input], base_resolve: BaseResolve
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
        return alias.lower(), scope_facts(inner, cte_scope=cte_scope, base_resolve=base_resolve)
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


def _referenced_columns(sel: exp.Select, *, alias: str, from_alias: str) -> frozenset[str]:
    """Every column of ``alias`` referenced anywhere in ``sel``'s own scope
    (projections, ON, WHERE, GROUP BY, HAVING, QUALIFY, ORDER BY), never
    descending into a nested SELECT (that is its own scope)."""
    cols: set[str] = set()

    def walk(node: Expr) -> None:
        for raw in node.args.values():
            value = cast("object", raw)
            items: list[object] = (
                cast("list[object]", value) if isinstance(value, list) else [value]
            )
            for item in items:
                if not isinstance(item, Expr):
                    continue
                if isinstance(item, exp.Select):
                    continue
                if isinstance(item, exp.Column):
                    if (
                        not isinstance(item.this, exp.Star)
                        and (sg.column_table(item) or from_alias).lower() == alias
                    ):
                        cols.add(sg.column_name(item).lower())
                    continue
                walk(item)

    for proj in sel.expressions:
        walk(proj)
    for j in sg.joins_of(sel):
        on = sg.on_of(j)
        if on is not None:
            walk(on)
    where = sg.where_of(sel)
    if where is not None and isinstance(where.this, Expr):
        walk(where.this)
    group = sg.group_of(sel)
    if group is not None:
        for e in group.expressions:
            walk(e)
    having = sel.args.get("having")
    if isinstance(having, exp.Having) and isinstance(having.this, Expr):
        walk(having.this)
    qualify = sg.qualify_of(sel)
    if qualify is not None and isinstance(qualify.this, Expr):
        walk(qualify.this)
    order = sel.args.get("order")
    if isinstance(order, exp.Order):
        for e in order.expressions:
            walk(e)
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
    sel: exp.Select, *, cte_scope: Mapping[str, Input], base_resolve: BaseResolve
) -> Input:
    local = _with_scope(sel, cte_scope, base_resolve)

    from_ = sg.from_of(sel)
    if from_ is None or not isinstance(from_.this, Expr):
        return EMPTY_INPUT
    from_resolved = _resolve_source(from_.this, cte_scope=local, base_resolve=base_resolve)
    if from_resolved is None:
        return EMPTY_INPUT
    from_alias = from_resolved[0]

    joins = sg.joins_of(sel)
    join_sources: list[tuple[str, Input]] = []
    for j in joins:
        if not isinstance(j.this, Expr):
            return EMPTY_INPUT
        resolved = _resolve_source(j.this, cte_scope=local, base_resolve=base_resolve)
        if resolved is None:
            return EMPTY_INPUT
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
    active_aliases: list[str] = []

    def mint(
        alias: str,
        inp: Input,
        *,
        keep_fds: bool = True,
        key_filter: Callable[[Key], bool] | None = None,
    ) -> None:
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
        active_aliases.append(alias)

    mint(from_alias, from_resolved[1])

    for (alias, inp), j in zip(join_sources, joins, strict=True):
        side = sg.join_side_of(j)
        if side in (sg.JoinSide.SEMI, sg.JoinSide.ANTI) or id(j) in anti_arms:
            continue  # a filters the accumulated side; mint nothing, exclude from r_out
        if side is sg.JoinSide.LEFT:
            breakdown = _left_join_breakdown(sg.on_of(j), alias=alias, default_alias=from_alias)
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
        minted = _predicate_qfds(where.this, default_alias=from_alias)
        facts |= minted
        predicate_pairs |= minted

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
            return EMPTY_INPUT
        g_attrs: frozenset[Attr] = frozenset(g_cols)
        facts, declared_by_alias, r_out = _apply_group_by(facts, declared_by_alias, g_attrs)
        candidate_aliases: list[str] = []
        extra_candidates.append(g_attrs)
    else:
        candidate_aliases = list(active_aliases)

    proj = _build_projection(sel, from_alias=from_alias, active_aliases=active_aliases)
    if proj.blocked:
        return EMPTY_INPUT

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
    return _project(
        facts,
        declared_final,
        candidate_aliases,
        extra_candidates,
        r_out=r_out,
        proj=proj,
        classes=classes,
    )


def _rename_declared(
    declared_by_alias: Mapping[str, frozenset[DeclaredFD]],
    classes: Mapping[Attr, frozenset[Attr]],
    proj: _Projection,
) -> frozenset[DeclaredFD]:
    """Each alias's declared instances renamed through the scope's own
    projection, through the same equivalence classes a plain dependency
    rewrites through: a declared instance's ``fd`` must always be one of the
    plain dependencies :func:`_project` derives (an ``FDSet`` invariant), so
    the two have to agree on which name represents a provably-equal qualified
    column. An instance whose columns do not all survive drops with them."""

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


def _group_qcols(sel: exp.Select, *, from_alias: str) -> frozenset[QCol] | None:
    """The GROUP BY key as qualified columns, or ``None`` for a shape we cannot
    name (an expression group key), which proves nothing scope-wide."""
    out: set[QCol] = set()
    for target in sg.group_targets(sel):
        g = target.grounded_expression
        if not isinstance(g, exp.Column) or isinstance(g.this, exp.Star):
            return None
        out.add(QCol((sg.column_table(g) or from_alias).lower(), sg.column_name(g).lower()))
    return frozenset(out)


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
    u: exp.Union, *, cte_scope: Mapping[str, Input], base_resolve: BaseResolve
) -> Input:
    """The union merge: keep exactly the declared instances every arm shares,
    after positional alignment (a union adds only cross pairs, one row per arm;
    a derived dependency's witness is arm-local and dies, a shared declared
    instance's grounding covers the cross pairs too and survives whole). A
    distinct union additionally keys on its full output column set; UNION ALL
    mints no key.

    The key is computed independently of the declared-instance alignment (it
    reads only the union's own first arm, not every arm's positional lineup),
    so a union with no declared instances to share, or one whose arms cannot
    be lined up by name at all, still gets its DISTINCT key."""
    keys = _union_key(u)
    arms = sg.union_arms(u)
    if arms is None:
        return Input(keys)
    names = [_positional_outputs(arm) for arm in arms]
    first = names[0] if names else None
    if first is None or any(n is None or len(n) != len(first) for n in names):
        return Input(keys)
    local = _with_scope(u, cte_scope, base_resolve)
    shared: frozenset[DeclaredFD] | None = None
    for arm_names, arm in zip(names, arms, strict=True):
        assert arm_names is not None
        rename = {src: (dst,) for src, dst in zip(arm_names, first, strict=True)}
        aligned = _remap_declared(
            scope_facts(arm, cte_scope=local, base_resolve=base_resolve).declared, rename
        )
        shared = aligned if shared is None else shared & aligned
        if not shared:
            return Input(keys)
    assert shared is not None
    return Input(keys, frozenset(inst.fd for inst in shared), shared)


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
    """Attributes provably carrying the same value, from value-equality pairs
    only (a caller passes the predicate mints, never the full fact set): a
    mutual *functional* dependency (a declared bijection) is not a value
    equality, so it must not merge two columns that can honestly differ.
    Symmetric single-attribute pairs (``a -> b`` and ``b -> a`` both present)
    partition into classes; this is what lets a key or dependency qualified to
    one join side survive when the output projects the other side's equal
    copy."""
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
    """One key-determinant choice per non-semi input, unioned, capped at a
    small fixed bound and built in sorted alias/attribute order for a
    deterministic result. An input with no known key rules out every
    combination (its rows are not provably not multiplied)."""
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
    candidates it happens to try."""
    if r_out not in closure(pairs, candidate):
        return None
    kept = set(candidate)
    for attr in sorted(kept, key=_attr_sort_key):
        trial = frozenset(kept - {attr})
        if r_out in closure(pairs, trial):
            kept.discard(attr)
    return frozenset(kept)


def _project(
    facts: set[QFD],
    declared: frozenset[DeclaredFD],
    candidate_aliases: Sequence[str],
    extra_candidates: Sequence[frozenset[Attr]],
    *,
    r_out: RowToken,
    proj: _Projection,
    classes: Mapping[Attr, frozenset[Attr]],
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

    # An equivalence class can carry several distinct output names at once (a
    # join ON equality projected from both sides, ``p.k AS o2, d.k AS o1``):
    # rewriting always picks one representative, so without this, a class
    # member's *other* names never appear in any determinant or dependent and
    # the mutual fact between them is lost. Mint it directly, for every pair
    # of distinct names one class carries.
    for group in {frozenset(g) for g in classes.values()}:
        names = frozenset(n for m in group for n in _direct_name(m, proj))
        for n1 in names:
            for n2 in names:
                if n1 != n2:
                    fds.add(FD(frozenset({n1}), n2))

    return Input(frozenset(keys), frozenset(fds), declared)
