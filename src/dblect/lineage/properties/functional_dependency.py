"""Functional-dependency property: the dependencies a relation's rows satisfy.

A relation's value is a set of functional dependencies over its output column
names, each one ``X -> y``: rows equal on ``X`` are equal on ``y``. This is what
the aggregate coherence guard checks before trusting an aggregate:
``sum(amount) group by country`` over a per-row currency is well typed exactly
when the group key holds the currency constant per group, and a ``country ->
currency`` dependency is the summarizability argument for that (Lenz &
Shoshani, SSDBM 1997; Hurtado & Mendelzon, ICDT 2001). Entailment
(:func:`determines`) is attribute closure under Armstrong's axioms.

The lattice orders by precision exactly as uniqueness orders keys: knowing more
dependencies is more precise, so ``meet`` (resolution of declarations) unions the
sets, ``join`` (confluence) intersects them, ``top`` is the empty set, and
``bottom`` is a formal universal element no real resolution reaches.

Dependencies come from five places. A declaration grounds one directly (synthetic
facts until the authoring bridge lands; the ``determines(...)`` contract is its
eventual source). An equality filter pins a column constant, the empty-determinant
dependency. A GROUP BY makes its group key determine every output (the key of the
grouped result). A candidate key read from the uniqueness property determines
every column selected alongside it, since a relation unique on ``K`` admits one
row per ``K`` value. And a join carries each kept side's dependencies (qualified
by source alias) plus an inner join's ``ON`` equalities as mutual determinations.

A declared dependency is more than a relation fact: it is an axiom about the
declaring relation's world (``order_id determines user_id`` because one order
cannot belong to two users), so it travels as a grounded instance
(:class:`DeclaredFD`) beside the plain set. The distinction pays at a UNION. A
dependency is universally quantified over row pairs, and a union adds exactly the
cross pairs, one row from each arm; a derived dependency's witness is arm-local
(two arms can each pin a column to a different constant), so every derived
dependency dies at the merge, while an instance that every arm carries from one
declaration covers the cross pairs too and survives with its grounding. Posture
elsewhere is silent-when-unproven: an outer join's NULL-padded side proves
nothing, and INTERSECT and EXCEPT claim nothing (each keeps a subset of one arm's
rows, which cannot break a dependency, so carrying the covering arm is a possible
refinement).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from typing import assert_never

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.grounding import grounded_scopes, grounding
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import (
    Annotation,
    CompileValue,
    Declared,
    Fact,
    NativeConstraint,
    Opacity,
)
from dblect.lineage.facts.property import DepContext, Property, PropertyRef, relation_property
from dblect.lineage.graph import SourceRef, source_ref_meta
from dblect.lineage.properties.predicate_flow import explicit_rename, has_star
from dblect.lineage.properties.uniqueness import CandidateKeySet, Key
from dblect.sql import _sqlglot as sg

# --- the value type ------------------------------------------------------------


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
    declared about, and the dependency in that relation's column names. ``fd`` is
    the same dependency under the current relation's output names, renamed as the
    walk climbs. All three fields key the match at a union merge: origin alone is
    not enough (one relation can declare two dependencies that arms rename onto
    the same output columns), and the axiom alone is not either (an arm can cross
    the declared columns, running the instance the other way)."""

    origin: SourceRef
    declared: FD
    fd: FD


@dataclass(frozen=True, slots=True)
class FDSet:
    """The dependencies a relation is known to satisfy.

    ``fds`` holds every known dependency; the empty set is the lattice ``top``
    ("no dependency known"). ``declared`` holds the grounded instances among them,
    each still tied to its declaration; every instance's current dependency is in
    ``fds`` (enforced at construction), so a consumer reading ``fds`` alone sees
    everything. ``is_bottom`` marks the formal universal element (the lattice
    ``bottom``): it absorbs under ``meet`` and is the identity under ``join``, and
    no resolution of real declarations reaches it, since dependency claims only
    ever union. Equality is structural, so ``FDSet(frozenset())`` (top) and the
    bottom sentinel are distinct values.
    """

    fds: frozenset[FD]
    declared: frozenset[DeclaredFD] = frozenset()
    is_bottom: bool = False

    def __post_init__(self) -> None:
        missing = {inst.fd for inst in self.declared} - self.fds
        if missing:
            raise ValueError(f"declared instances name dependencies outside fds: {missing}")

    @staticmethod
    def of(*fds: FD) -> FDSet:
        return FDSet(frozenset(fds))


# The empty dependency set: "we know of no dependency", the value every
# undeclared relation grounds to and the meet identity.
NO_FDS: FDSet = FDSet(frozenset())

# The formal universal element. Unreachable when resolving real declarations
# (they only union), present so the lattice is bounded.
ALL_FDS: FDSet = FDSet(frozenset(), is_bottom=True)


def _meet(a: FDSet, b: FDSet) -> FDSet:
    """Most precise value consistent with both: the union of the dependencies,
    grounded instances included."""
    if a.is_bottom or b.is_bottom:
        return ALL_FDS
    return FDSet(a.fds | b.fds, a.declared | b.declared)


def _join(a: FDSet, b: FDSet) -> FDSet:
    """Least precise value both refine: the dependencies both sides carry. An
    instance survives only whole (same grounding, same current columns), so the
    same dependency grounded at two different origins keeps neither instance."""
    if a.is_bottom:
        return b
    if b.is_bottom:
        return a
    return FDSet(a.fds & b.fds, a.declared & b.declared)


FUNCTIONAL_DEPENDENCY_LATTICE: Lattice[FDSet] = Lattice(
    meet=_meet,
    join=_join,
    top=NO_FDS,
    bottom=ALL_FDS,
)


def determines(value: FDSet, given: frozenset[str], target: str) -> bool:
    """Whether ``value`` entails ``given -> target``: attribute closure under
    Armstrong's axioms (sound and complete for FD entailment). The bottom sentinel
    entails everything, and a target inside ``given`` holds by reflexivity."""
    if target in given:
        return True
    if value.is_bottom:
        return True
    closure = set(given)
    changed = True
    while changed:
        changed = False
        for fd in value.fds:
            if fd.dependent not in closure and fd.determinant <= closure:
                closure.add(fd.dependent)
                changed = True
    return target in closure


def minimal_cover(value: FDSet, cols: frozenset[str]) -> frozenset[str]:
    """The irreducible subset of ``cols`` that still determines all of ``cols`` under
    ``value``: fold each column into the others whenever they already entail it.

    A join spanning a declared dependency chain (``store_id -> region_id ->
    country_id``) is really keyed on the chain's root, the additional equalities
    functionally redundant. This reduces the spanned columns to that root, so a detector
    reports the declared key as one unit rather than treating co-determined columns as
    independent. With no dependency known (``NO_FDS``) nothing is entailed by the rest,
    so the result is ``cols`` unchanged.

    The fixed-order walk drops a column only when its survivors still entail it, which keeps
    the result irreducible and coverage-preserving; the lattice tests pin both as properties
    over ``determines``."""
    kept = set(cols)
    for col in sorted(cols):
        if determines(value, frozenset(kept - {col}), col):
            kept.discard(col)
    return frozenset(kept)


# --- grounding -----------------------------------------------------------------


def _lift_declared(fact: Fact[FDSet, SourceRef]) -> Fact[FDSet, SourceRef]:
    """Ground a declaration's dependencies as instances carrying its identity.

    A declaration is a person's axiom about the scope's world, so each of its
    dependencies becomes a :class:`DeclaredFD` at that scope. The provenance space
    is closed and each kind is decided explicitly: a native constraint holds by
    the warehouse's write path and a compile value is minted by the toolchain, so
    neither claims a world and neither grounds an instance."""
    match fact.provenance:
        case Declared():
            if fact.value.is_bottom:
                return fact
            instances = frozenset(DeclaredFD(fact.scope, fd, fd) for fd in fact.value.fds)
            return replace(fact, value=FDSet(fact.value.fds, fact.value.declared | instances))
        case NativeConstraint() | CompileValue():
            return fact
    assert_never(fact.provenance)


def _lifted(
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
) -> Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]]:
    return {scope: tuple(_lift_declared(f) for f in bucket) for scope, bucket in facts.items()}


def functional_dependency_grounding(
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
    *,
    opaque: Collection[SourceRef] = (),
) -> Callable[[SourceRef], Annotation[FDSet]]:
    """Fold the per-relation dependency facts into grounded annotations. The same
    fold every property uses (an opt-out grounds EXPLICIT top, a resolved bucket
    grounds its value CONCRETE, everything else the IMPLICIT-top default), over
    facts whose declarations are first lifted into grounded instances."""
    return grounding(_lifted(facts), opaque, FUNCTIONAL_DEPENDENCY_LATTICE)


def functional_dependency_grounded_scopes(
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
    *,
    opaque: Collection[SourceRef] = (),
) -> set[SourceRef]:
    """The relations a dependency fact grounded, for coverage. Reads the same fold
    ``functional_dependency_grounding`` does."""
    return grounded_scopes(_lifted(facts), opaque, FUNCTIONAL_DEPENDENCY_LATTICE)


# --- the relation reducer --------------------------------------------------------
#
# The relation-algebra walk for dependencies, the same shape as the predicate-flow
# walk: single-source scopes carry, rename through the projection, and every shape
# outside the modelled fragment drops to the empty set rather than over-claiming.
# The walk's value is an FDSet whose declared instances ride along under exactly
# the same renames and cuts as the plain dependencies, so the two can never drift.


@dataclass(frozen=True, slots=True)
class _Base:
    """What a resolved FROM source contributes: its dependency value (in its
    output column names) and, for a base table with the uniqueness edge live, its
    candidate keys (each of which determines the columns read alongside it)."""

    value: FDSet
    keys: frozenset[Key] = frozenset()


_NOTHING: _Base = _Base(NO_FDS)

# Resolves a base (non-CTE) table reference to what it contributes. The reducer's
# implementation recurses through the shared propagator via the table's stamped
# SourceRef, so declarations and the provisional taint flow across models.
_BaseResolve = Callable[["exp.Table"], _Base]


class _FdWalk:
    """Bottom-up dependency inference over one relational tree.

    ``base_resolve`` resolves a base table; CTEs and inline subqueries are
    resolved structurally within the walk. Single-source scopes carry fully; a
    join carries its kept sides (see :meth:`_join_select`); a UNION keeps the
    declared instances every arm shares and nothing else (see :meth:`_union`);
    and an unmodellable group shape (positional or computed group keys) drops the
    scope's dependencies entirely.
    """

    def __init__(self, base_resolve: _BaseResolve) -> None:
        self._base_resolve = base_resolve

    def scope_fds(self, node: Expr, *, cte_scope: Mapping[str, FDSet]) -> FDSet:
        if isinstance(node, exp.Select):
            return self._select(node, cte_scope=cte_scope)
        if isinstance(node, exp.Union):
            return self._union(node, cte_scope=cte_scope)
        # INTERSECT and EXCEPT keep a subset of one arm's rows, which cannot break
        # a dependency; they still claim nothing (carrying the covering arm is a
        # possible refinement, not the current contract), and so does every other
        # unmodelled shape.
        return NO_FDS

    def _with_scope(self, node: Expr, cte_scope: Mapping[str, FDSet]) -> dict[str, FDSet]:
        """``cte_scope`` extended with the CTEs ``node`` declares, each resolved in
        the scope the ones before it built."""
        local = dict(cte_scope)
        with_ = node.args.get("with_")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if isinstance(cte, exp.CTE) and isinstance(cte.this, Expr):
                    local[cte.alias_or_name] = self.scope_fds(cte.this, cte_scope=local)
        return local

    def _union(self, u: exp.Union, *, cte_scope: Mapping[str, FDSet]) -> FDSet:
        """The union merge: keep exactly the declared instances every arm shares.

        A union adds the cross pairs, one row from each arm, so survival is a
        coverage question. Every derived dependency's witness is arm-local and
        dies. A declared instance carried by every arm from one declaration, with
        the same current columns once the arms are lined up by position under the
        first arm's names (as SQL merges them), draws every merged row's pair from
        that one world, so the cross pairs are covered and the instance survives
        whole. UNION and UNION ALL merge alike (the dedup only removes rows). An
        arm without positionally nameable outputs (a star, a duplicated name, not
        a plain SELECT) leaves nothing to line up."""
        local = self._with_scope(u, cte_scope)
        arms = _union_arms(u)
        carried = [self.scope_fds(arm, cte_scope=local) for arm in arms]
        names = [_positional_outputs(arm) for arm in arms]
        first = names[0] if names else None
        if first is None or any(n is None or len(n) != len(first) for n in names):
            return NO_FDS
        shared: frozenset[DeclaredFD] | None = None
        for arm_names, arm_value in zip(names, carried, strict=True):
            assert arm_names is not None
            rename = {src: (dst,) for src, dst in zip(arm_names, first, strict=True)}
            aligned = _remap_declared(arm_value.declared, rename)
            shared = aligned if shared is None else shared & aligned
        if not shared:
            return NO_FDS
        return FDSet(frozenset(inst.fd for inst in shared), shared)

    def _select(self, sel: exp.Select, *, cte_scope: Mapping[str, FDSet]) -> FDSet:
        local = self._with_scope(sel, cte_scope)

        from_ = sg.from_of(sel)
        if from_ is None or not isinstance(from_.this, Expr):
            return NO_FDS
        joins = sg.joins_of(sel)
        if joins:
            return self._join_select(sel, from_.this, joins, cte_scope=local)
        base = self._resolve_source(from_.this, cte_scope=local)
        if base is None:
            return NO_FDS
        carried = base.value.fds
        declared = base.value.declared

        # A dependency is universally quantified over row pairs, and a WHERE only
        # removes pairs, so everything carries; an equality filter additionally pins
        # its column constant, the empty-determinant dependency.
        where = sg.where_of(sel)
        if where is not None and isinstance(where.this, Expr):
            carried = carried | {
                FD(frozenset(), sg.column_name(col).lower())
                for col in sg.equality_literal_columns(where.this)
            }

        star = has_star(sel)
        rename = explicit_rename(sel)

        # A relation unique on K admits one row per K value, so K determines every
        # column this scope reads from it. Minted only over named projections: under
        # a bare star the column universe is unknown.
        for key in base.keys:
            carried = carried | {FD(key, dep) for dep in rename if dep not in key}

        group = sg.group_of(sel)
        group_names: frozenset[str] | None = None
        if group is not None and group.expressions:
            group_names = _group_columns(sel)
            if group_names is None:
                return NO_FDS  # unmodellable group shape: prove nothing
            # Grouping aggregates everything outside the group key away, so only a
            # dependency lying entirely within it still describes the output rows.
            # The group rows' pairs are a subset of the input's, so an instance
            # inside the key keeps its grounding.
            carried = _within(carried, group_names)
            declared = frozenset(
                inst
                for inst in declared
                if inst.fd.determinant | {inst.fd.dependent} <= group_names
            )

        out = carried if star else _remap(carried, rename)
        out_declared = declared if star else _remap_declared(declared, rename)
        if group_names is not None:
            group_out = group_names if star else _remap_columns(group_names, rename)
            if group_out is not None:
                # The group key is a key of the grouped result (one row per group),
                # so it determines every named output.
                out = out | {
                    FD(group_out, name) for name in _named_outputs(sel) if name not in group_out
                }
        return FDSet(out, out_declared)

    def _resolve_source(self, node: Expr, *, cte_scope: Mapping[str, FDSet]) -> _Base | None:
        if isinstance(node, exp.Table):
            if node.name in cte_scope:
                return _Base(cte_scope[node.name])
            return self._base_resolve(node)
        if isinstance(node, exp.Subquery):
            inner = node.this
            if not isinstance(inner, Expr):
                return None
            return _Base(self.scope_fds(inner, cte_scope=cte_scope))
        return None

    def _join_select(
        self,
        sel: exp.Select,
        from_node: Expr,
        joins: list[exp.Join],
        *,
        cte_scope: Mapping[str, FDSet],
    ) -> FDSet:
        """Dependencies a join carries to the projection.

        An FD that holds on a joined relation holds on the join wherever that side's
        rows come through un-padded: two output rows agreeing on its determinant come
        from that relation's rows agreeing on it, and a join only filters or duplicates
        such rows (a duplicate still agrees on the dependent). So each kept side's
        dependencies carry, an inner join's ``ON`` equalities add a mutual determination
        between the joined columns, and an equality filter pins its column. A kept
        side's declared instances carry the same way, since its pairs are still the
        declared world's pairs, at worst duplicated. Everything is tracked qualified
        by source alias (a join can expose two ``country`` columns), then projected
        onto the output names.

        Padding is what breaks an FD: an outer join fills its optional side with NULL
        on unmatched rows, so a padded side's dependencies are dropped (until the NULL
        semantics are worked through) and an ``ON`` equality is minted only while both
        its columns stay on kept sides. The padded sides mirror nullability's taint:
        LEFT pads the joined-in side, RIGHT pads the accumulated left, FULL pads both,
        INNER and CROSS pad nothing (a cross join only duplicates rows). Aggregation
        over a join is deferred (the FD scope a downstream guard would read is not a
        single source), and a candidate-key-derived dependency is not minted across a
        join (sound to omit; the key path stays single-source for now)."""
        group = sg.group_of(sel)
        if group is not None and group.expressions:
            return NO_FDS

        sources: list[tuple[str, _Base]] = []
        for node in (from_node, *(j.this for j in joins)):
            if not isinstance(node, Expr):
                return NO_FDS
            base = self._resolve_source(node, cte_scope=cte_scope)
            if base is None:
                return NO_FDS
            sources.append((node.alias_or_name.lower(), base))

        aliases = [alias for alias, _ in sources]
        padded: set[str] = set()
        for i, j in enumerate(joins, start=1):
            side = sg.join_side_of(j)
            if side is sg.JoinSide.LEFT:
                padded.add(aliases[i])
            elif side is sg.JoinSide.RIGHT:
                padded.update(aliases[:i])
            elif side is sg.JoinSide.FULL:
                padded.add(aliases[i])
                padded.update(aliases[:i])

        qfds: set[tuple[frozenset[_QCol], _QCol]] = set()
        qdeclared: list[tuple[DeclaredFD, frozenset[_QCol], _QCol]] = []
        for alias, base in sources:
            if alias in padded:
                continue
            for fd in base.value.fds:
                qfds.add((frozenset((alias, d) for d in fd.determinant), (alias, fd.dependent)))
            qdeclared.extend(
                (
                    inst,
                    frozenset((alias, d) for d in inst.fd.determinant),
                    (alias, inst.fd.dependent),
                )
                for inst in base.value.declared
            )
        for j in joins:
            if sg.join_side_of(j) is not sg.JoinSide.INNER:
                continue
            on = sg.on_of(j)
            if on is None:
                continue
            for left, right in sg.equality_column_pairs(on):
                ql, qr = _qcol(left), _qcol(right)
                if ql is None or qr is None or ql[0] in padded or qr[0] in padded:
                    continue
                qfds.add((frozenset({ql}), qr))
                qfds.add((frozenset({qr}), ql))
        where = sg.where_of(sel)
        if where is not None and isinstance(where.this, Expr):
            for col in sg.equality_literal_columns(where.this):
                qc = _qcol(col)
                if qc is not None:
                    qfds.add((frozenset(), qc))

        qrename, star = _qualified_rename(sel)
        if star:
            return NO_FDS  # a star over a join leaves the output universe ambiguous
        declared_out: set[DeclaredFD] = set()
        for inst, det, dep in qdeclared:
            fd = _project_one(det, dep, qrename)
            if fd is not None:
                declared_out.add(replace(inst, fd=fd))
        return FDSet(_project_qualified(qfds, qrename), frozenset(declared_out))


def _group_columns(sel: exp.Select) -> frozenset[str] | None:
    """The group key as case-folded input column names, or ``None`` for a shape we cannot name
    (an expression group key).

    Targets are read through :attr:`sg.GroupTarget.grounded_expression`, so ``GROUP BY 1`` names
    the same columns its spelled-out form would. The dependencies minted below say the group key
    determines every output, which is a key claim in FD clothing, so the same fallback applies:
    a binding the AST cannot decide stays at the column name the query wrote.
    """
    out: set[str] = set()
    for target in sg.group_targets(sel):
        g = target.grounded_expression
        if not isinstance(g, exp.Column) or isinstance(g.this, exp.Star):
            return None
        out.add(sg.column_name(g).lower())
    return frozenset(out)


def _union_arms(u: exp.Union) -> list[Expr]:
    """The arms of ``u`` in order, a chain of unions (left-nested by the parser)
    flattened so an instance shared by every arm survives the whole chain in one
    pass. Mixing UNION and UNION ALL is fine: the merged rows are drawn from the
    arms' rows either way."""
    arms: list[Expr] = []
    for side in (u.this, u.expression):
        if isinstance(side, exp.Union):
            arms.extend(_union_arms(side))
        elif isinstance(side, Expr):
            arms.append(side)
    return arms


def _positional_outputs(arm: Expr) -> tuple[str, ...] | None:
    """An arm's output column names in projection order, or ``None`` when they
    cannot be lined up positionally (a star, a duplicated name, not a SELECT)."""
    if not isinstance(arm, exp.Select):
        return None
    names: list[str] = []
    for proj in arm.expressions:
        if isinstance(proj, exp.Star):
            return None
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
            return None
        names.append(proj.alias_or_name.lower())
    if len(set(names)) != len(names):
        return None
    return tuple(names)


def _within(fds: frozenset[FD], columns: frozenset[str]) -> frozenset[FD]:
    """The dependencies mentioning only ``columns``."""
    return frozenset(fd for fd in fds if fd.determinant | {fd.dependent} <= columns)


def _remap(fds: frozenset[FD], rename: Mapping[str, tuple[str, ...]]) -> frozenset[FD]:
    """Rename each dependency onto the projection's output names, dropping any whose
    columns do not all survive."""
    out: set[FD] = set()
    for fd in fds:
        renamed = _remap_one(fd, rename)
        if renamed is not None:
            out.add(renamed)
    return frozenset(out)


def _remap_declared(
    instances: frozenset[DeclaredFD], rename: Mapping[str, tuple[str, ...]]
) -> frozenset[DeclaredFD]:
    """Rename each instance's current dependency by the same rules as
    :func:`_remap`, keeping its grounding; an instance whose columns do not all
    survive drops with them."""
    out: set[DeclaredFD] = set()
    for inst in instances:
        fd = _remap_one(inst.fd, rename)
        if fd is not None:
            out.add(replace(inst, fd=fd))
    return frozenset(out)


def _remap_one(fd: FD, rename: Mapping[str, tuple[str, ...]]) -> FD | None:
    """One dependency renamed onto the projection's output names, or ``None`` when
    a column does not survive. One output name per input column suffices (copies of
    a column carry equal values), picked stably."""
    determinant = _remap_columns(fd.determinant, rename)
    dependent = rename.get(fd.dependent)
    if determinant is None or not dependent:
        return None
    return FD(determinant, min(dependent))


def _remap_columns(
    columns: frozenset[str], rename: Mapping[str, tuple[str, ...]]
) -> frozenset[str] | None:
    out: set[str] = set()
    for col in columns:
        names = rename.get(col)
        if not names:
            return None
        out.add(min(names))
    return frozenset(out)


def _named_outputs(sel: exp.Select) -> frozenset[str]:
    """Every output column the projection names: bare columns and aliases, computed
    projections included (an aggregate's alias is exactly what a group key
    determines). Unnamed shapes contribute nothing."""
    names: set[str] = set()
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias):
            names.add(proj.alias_or_name.lower())
        elif isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
            names.add(sg.column_name(proj).lower())
    return frozenset(names)


# --- qualified projection (the join case) ----------------------------------------
#
# A join can expose two columns of the same name (``payments.country`` and
# ``customers.country``), so dependencies under a join are tracked qualified by source
# alias, ``(alias, column)``, and projected onto bare output names only at the end.

_QCol = tuple[str, str]


def _qcol(col: exp.Column) -> _QCol | None:
    """A column as ``(alias, name)``, or ``None`` when it carries no table qualifier
    (ambiguous under a join, so not attributable to a side)."""
    table = sg.column_table(col)
    if not table:
        return None
    return (table.lower(), sg.column_name(col).lower())


def _qualified_rename(sel: exp.Select) -> tuple[dict[_QCol, tuple[str, ...]], bool]:
    """Map each qualified input column to the output names it appears under, plus a flag
    for a star (which leaves the output universe ambiguous over a join). Computed
    projections carry no source column through and contribute nothing."""
    out: dict[_QCol, list[str]] = {}
    star = False
    for proj in sel.expressions:
        if isinstance(proj, exp.Star):
            star = True
            continue
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Column):
            if isinstance(inner.this, exp.Star):  # ``alias.*``
                star = True
                continue
            qc = _qcol(inner)
            if qc is not None:
                out.setdefault(qc, []).append(proj.alias_or_name.lower())
    return {qc: tuple(names) for qc, names in out.items()}, star


def _project_one(
    determinant: frozenset[_QCol], dependent: _QCol, rename: Mapping[_QCol, tuple[str, ...]]
) -> FD | None:
    """One qualified dependency projected onto the output names, or ``None`` when a
    column does not survive. One output name per column suffices (copies carry
    equal values), picked stably."""
    dep_names = rename.get(dependent)
    if not dep_names:
        return None
    det_names = [rename.get(d) for d in determinant]
    if any(names is None for names in det_names):
        return None
    return FD(frozenset(min(names) for names in det_names if names is not None), min(dep_names))


def _project_qualified(
    qfds: set[tuple[frozenset[_QCol], _QCol]], rename: Mapping[_QCol, tuple[str, ...]]
) -> frozenset[FD]:
    """Rename each qualified dependency onto the projection's output names, dropping any
    whose columns do not all survive."""
    out: set[FD] = set()
    for determinant, dependent in qfds:
        fd = _project_one(determinant, dependent, rename)
        if fd is not None:
            out.add(fd)
    return frozenset(out)


# --- the property ------------------------------------------------------------


def functional_dependency_property(
    ground: Callable[[SourceRef], Annotation[FDSet]],
    *,
    uniqueness: PropertyRef[CandidateKeySet, SourceRef] | None = None,
) -> Property[FDSet, SourceRef]:
    """The relation-scoped functional-dependency property over a caller-supplied
    grounding (synthetic facts in tests; the contract bridge is the eventual
    source). Declared and inferred dependencies both hold, so they compose by meet
    (``reconcile_by_meet``), exactly as uniqueness composes keys. Passing the
    uniqueness property's ref switches on the key-derived source and declares the
    dependency edge the registry orders by."""

    def reduce_(
        deriv: Expr,
        _prop: Property[FDSet, SourceRef],
        recurse: Callable[[SourceRef], Annotation[FDSet]],
        ctx: DepContext,
        _default: Annotation[FDSet],
        _sink: object = None,
    ) -> Annotation[FDSet]:
        provisional = False

        def base_resolve(table: exp.Table) -> _Base:
            nonlocal provisional
            ref = source_ref_meta(table)
            if ref is None:
                return _NOTHING
            ann = recurse(ref)
            provisional = provisional or ann.provisional
            keys: frozenset[Key] = frozenset()
            if uniqueness is not None:
                key_ann = ctx.annotation(uniqueness, ref)
                if key_ann is not None:
                    keys = key_ann.value.keys
            # The bottom sentinel carries no dependencies to walk with; strip it to
            # the plain sets here, exactly as the set-valued walk always has.
            return _Base(FDSet(ann.value.fds, ann.value.declared), keys)

        value = _FdWalk(base_resolve).scope_fds(deriv, cte_scope={})
        opacity = Opacity.CONCRETE if value.fds else Opacity.IMPLICIT
        return Annotation(value, opacity, provisional=provisional)

    return relation_property(
        name="functional_dependency",
        lattice=FUNCTIONAL_DEPENDENCY_LATTICE,
        operators={},
        aggregates={},
        ground=ground,
        depends_on=(uniqueness,) if uniqueness is not None else (),
        reconcile_by_meet=True,
        reducer=reduce_,
    )
