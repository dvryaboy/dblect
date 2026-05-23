"""Propagate uniqueness facts through SQL operations.

Declarations and top-level structural shapes give us facts about each model.
This module extends that reasoning *through* a model's SQL: a CTE that
pass-throughs a ref'd model carries the model's keys; a join to a
unique-on-key dimension preserves the fact side's keys; a GROUP BY introduces
a new key on the group columns; DISTINCT and UNION (de-dup) introduce a key
on the full projected tuple.

The pass walks each ``Select`` (and ``Union``) in the tree bottom-up,
recording a ``ScopeFacts`` per node. Inside one scope, columns are tracked
qualified by source alias so join-key resolution lines up; at the scope
boundary the qualifiers collapse to bare output names because consumers of
this scope's output only see the projected columns.

Posture: silent when we can't ground a claim. An operation we don't model
(positional ``GROUP BY``, ``OR``-disjunctive join predicate, a projection
expression we can't trace) drops tracked facts for that scope rather than
guessing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg
from dblect.sql._sqlglot import JoinSide
from dblect.uniqueness.facts import UniquenessFact, UniquenessSource

_EMPTY_BARE_LINEAGE: Mapping[frozenset[str], tuple[UniquenessFact, ...]] = MappingProxyType({})
_EMPTY_QUAL_LINEAGE: Mapping[frozenset[tuple[str, str]], tuple[UniquenessFact, ...]] = (
    MappingProxyType({})
)

# Internal column representation: an alias qualifier plus a column name. The
# alias comes from the FROM/JOIN source the column belongs to. We never carry
# bare unqualified columns through the walk, which keeps multi-source scopes
# unambiguous; unqualified references in the SQL itself get attached to the
# FROM alias as a best-effort default.
_QCol = tuple[str, str]
_QKey = frozenset[_QCol]


@dataclass(frozen=True, slots=True)
class ScopeFacts:
    """Uniqueness facts about a scope's *output* rows.

    ``keys`` is a set of candidate keys; each key is a set of bare output
    column names. ``lineage`` records, for each key, the upstream
    ``UniquenessFact`` (if any) that this key inherits from. Empty for keys
    derived purely structurally within the scope (a DISTINCT, GROUP BY, or
    UNION distinct).
    """

    keys: frozenset[frozenset[str]]
    lineage: Mapping[frozenset[str], tuple[UniquenessFact, ...]] = _EMPTY_BARE_LINEAGE

    @staticmethod
    def empty() -> ScopeFacts:
        return ScopeFacts(keys=frozenset(), lineage=_EMPTY_BARE_LINEAGE)


def propagate_facts(
    tree: Expr,
    *,
    model_facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
) -> Mapping[int, ScopeFacts]:
    """Per-scope uniqueness facts derived bottom-up through `tree`.

    The returned mapping is keyed by ``id(node)`` for each ``Select`` and
    ``Union`` encountered. Scope keys are only valid within the lifetime of
    `tree`; callers that consume the map must do so before the parsed tree
    is discarded.
    """
    walker = _Walker(model_facts=model_facts, model_name_to_uid=model_name_to_uid)
    walker.visit(tree, cte_scope={})
    return walker.out


def top_scope_facts(tree: Expr, propagation: Mapping[int, ScopeFacts]) -> ScopeFacts | None:
    """The ``ScopeFacts`` attached to the top-level ``Select``/``Union`` of `tree`.

    Returns ``None`` when the tree's root isn't a shape the propagation pass
    recognizes as a fact-bearing scope (e.g., a bare table reference).
    """
    if not isinstance(tree, exp.Select | exp.Union):
        return None
    return propagation.get(id(tree))


def facts_from_tree(
    model_unique_id: str,
    tree: Expr,
    *,
    model_facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
) -> tuple[UniquenessFact, ...]:
    """Top-level scope's facts from `tree`, materialized as ``UniquenessFact``s.

    Computes the per-scope propagation map and surfaces only the top-level
    scope (the one whose output is the model's output). Detectors that need
    per-scope facts compute their own propagation map and reuse it directly.
    """
    propagation = propagate_facts(
        tree, model_facts=model_facts, model_name_to_uid=model_name_to_uid
    )
    top = top_scope_facts(tree, propagation)
    if top is None:
        return ()
    out: list[UniquenessFact] = []
    for key in top.keys:
        chain = top.lineage.get(key, ())
        source = UniquenessSource.PROPAGATED if chain else UniquenessSource.STRUCTURAL_PROOF
        detail = _summarize_chain(chain) if chain else "structural proof from SQL"
        out.append(
            UniquenessFact(
                model_unique_id=model_unique_id,
                columns=key,
                source=source,
                detail=detail,
                derived_from=chain,
            )
        )
    return tuple(out)


def _summarize_chain(chain: tuple[UniquenessFact, ...]) -> str:
    parts = [f"{f.model_unique_id}:{','.join(sorted(f.columns))}" for f in chain]
    return "propagated from " + "; ".join(parts)


class _Walker:
    def __init__(
        self,
        *,
        model_facts: Mapping[str, tuple[UniquenessFact, ...]],
        model_name_to_uid: Mapping[str, str],
    ) -> None:
        self.model_facts = model_facts
        self.model_name_to_uid = model_name_to_uid
        self.out: dict[int, ScopeFacts] = {}

    def visit(self, node: Expr, *, cte_scope: Mapping[str, ScopeFacts]) -> ScopeFacts:
        if isinstance(node, exp.Select):
            return self._visit_select(node, cte_scope=cte_scope)
        if isinstance(node, exp.Union):
            return self._visit_union(node, cte_scope=cte_scope)
        return ScopeFacts.empty()

    def _visit_select(self, sel: exp.Select, *, cte_scope: Mapping[str, ScopeFacts]) -> ScopeFacts:
        local = dict(cte_scope)
        w = sel.args.get("with_")
        if isinstance(w, exp.With):
            for cte in w.expressions:
                if not isinstance(cte, exp.CTE):
                    continue
                body = cte.this
                if not isinstance(body, Expr):
                    continue
                local[cte.alias_or_name] = self.visit(body, cte_scope=local)
        facts = self._compute_select(sel, cte_scope=local)
        self.out[id(sel)] = facts
        return facts

    def _visit_union(self, u: exp.Union, *, cte_scope: Mapping[str, ScopeFacts]) -> ScopeFacts:
        left = u.this
        right = u.args.get("expression")
        if isinstance(left, Expr):
            self.visit(left, cte_scope=cte_scope)
        if isinstance(right, Expr):
            self.visit(right, cte_scope=cte_scope)
        # UNION ALL doesn't deduplicate; UNION (distinct=True) guarantees the
        # output is unique on the full projected tuple.
        distinct = bool(u.args.get("distinct"))
        if not distinct or not isinstance(left, exp.Select):
            facts = ScopeFacts.empty()
            self.out[id(u)] = facts
            return facts
        names = _projected_output_names(left)
        if not names:
            facts = ScopeFacts.empty()
            self.out[id(u)] = facts
            return facts
        key = frozenset(names)
        facts = ScopeFacts(keys=frozenset({key}), lineage={key: ()})
        self.out[id(u)] = facts
        return facts

    def _compute_select(
        self, sel: exp.Select, *, cte_scope: Mapping[str, ScopeFacts]
    ) -> ScopeFacts:
        from_ = sg.from_of(sel)
        if from_ is None or from_.this is None:
            return ScopeFacts.empty()
        from_resolved = self._resolve_source(from_.this, cte_scope=cte_scope)
        if from_resolved is None:
            return ScopeFacts.empty()
        from_alias, from_facts = from_resolved
        combined = _QualifiedFacts.from_source(from_alias, from_facts)

        for j in sg.joins_of(sel):
            combined = self._apply_join(j, combined, cte_scope=cte_scope)

        # WHERE filters can't add duplicates; keys preserved.

        group = sg.group_of(sel)
        if group is not None and group.expressions:
            # Positional or expression group key: we can't model the output
            # cardinality, so drop tracked keys rather than over-claim.
            new_combined = self._apply_group_by(group, from_alias=from_alias)
            combined = _QualifiedFacts.empty() if new_combined is None else new_combined

        return self._project(sel, combined, from_alias=from_alias)

    def _resolve_source(
        self, node: Expr, *, cte_scope: Mapping[str, ScopeFacts]
    ) -> tuple[str, ScopeFacts] | None:
        if isinstance(node, exp.Table):
            return self._resolve_table(node, cte_scope=cte_scope)
        if isinstance(node, exp.Subquery):
            inner = node.this
            if not isinstance(inner, Expr):
                return None
            facts = self.visit(inner, cte_scope=cte_scope)
            alias = node.alias_or_name
            if not alias:
                return None
            return alias, facts
        return None

    def _resolve_table(
        self, t: exp.Table, *, cte_scope: Mapping[str, ScopeFacts]
    ) -> tuple[str, ScopeFacts]:
        alias = t.alias_or_name
        name = t.name
        if name in cte_scope:
            return alias, cte_scope[name]
        uid = self.model_name_to_uid.get(name)
        if uid is None:
            return alias, ScopeFacts.empty()
        keys: set[frozenset[str]] = set()
        lineage: dict[frozenset[str], tuple[UniquenessFact, ...]] = {}
        for f in self.model_facts.get(uid, ()):
            keys.add(f.columns)
            lineage[f.columns] = (f,)
        return alias, ScopeFacts(keys=frozenset(keys), lineage=lineage)

    def _apply_join(
        self,
        j: exp.Join,
        combined: _QualifiedFacts,
        *,
        cte_scope: Mapping[str, ScopeFacts],
    ) -> _QualifiedFacts:
        side = sg.join_side_of(j)
        if side is JoinSide.CROSS:
            # Cartesian: every left row pairs with every right row, so left's
            # keys are no longer unique on the result. Drop.
            return _QualifiedFacts.empty()
        target = j.this
        if not isinstance(target, Expr):
            return _QualifiedFacts.empty()
        resolved = self._resolve_source(target, cte_scope=cte_scope)
        if resolved is None:
            return _QualifiedFacts.empty()
        r_alias, r_facts = resolved
        if not r_facts.keys:
            # No keys on the joined-in side; can't prove no fanout.
            return _QualifiedFacts.empty()
        on = sg.on_of(j)
        if on is None:
            return _QualifiedFacts.empty()
        right_join_cols = sg.equality_cols_on_alias(on, r_alias)
        if right_join_cols is None:
            return _QualifiedFacts.empty()
        # If any right-side key is covered by the right-side join columns,
        # the joined-in side won't multiply rows from the left side, so the
        # left side's keys carry through.
        if not any(k <= right_join_cols for k in r_facts.keys):
            return _QualifiedFacts.empty()
        return combined

    def _apply_group_by(self, group: exp.Group, *, from_alias: str) -> _QualifiedFacts | None:
        cols: list[_QCol] = []
        for g in group.expressions:
            if not isinstance(g, exp.Column):
                return None
            t = sg.column_table(g) or from_alias
            cols.append((t, sg.column_name(g)))
        key: _QKey = frozenset(cols)
        return _QualifiedFacts(keys=frozenset({key}), lineage={key: ()})

    def _project(
        self,
        sel: exp.Select,
        combined: _QualifiedFacts,
        *,
        from_alias: str,
    ) -> ScopeFacts:
        projection = _Projection.build(sel, from_alias=from_alias)

        out_keys: set[frozenset[str]] = set()
        out_lineage: dict[frozenset[str], tuple[UniquenessFact, ...]] = {}
        for k in combined.keys:
            mapped = projection.map_key(k)
            if mapped is None:
                continue
            out_keys.add(mapped)
            existing = out_lineage.get(mapped)
            new_lineage = combined.lineage.get(k, ())
            if not existing and new_lineage:
                out_lineage[mapped] = new_lineage
            else:
                out_lineage.setdefault(mapped, existing or ())

        if sel.args.get("distinct") is not None:
            names = _projected_output_names(sel)
            if names:
                distinct_key = frozenset(names)
                out_keys.add(distinct_key)
                out_lineage.setdefault(distinct_key, ())

        return ScopeFacts(keys=frozenset(out_keys), lineage=dict(out_lineage))


@dataclass(frozen=True, slots=True)
class _QualifiedFacts:
    """Alias-qualified candidate keys for the current scope's working set.

    Lives only inside one ``Select``'s computation; the qualifier disambiguates
    columns that come from different FROM/JOIN sources within the same scope.
    """

    keys: frozenset[_QKey]
    lineage: Mapping[_QKey, tuple[UniquenessFact, ...]]

    @staticmethod
    def empty() -> _QualifiedFacts:
        return _QualifiedFacts(keys=frozenset(), lineage=_EMPTY_QUAL_LINEAGE)

    @staticmethod
    def from_source(alias: str, facts: ScopeFacts) -> _QualifiedFacts:
        keys: set[_QKey] = set()
        lineage: dict[_QKey, tuple[UniquenessFact, ...]] = {}
        for bare in facts.keys:
            qk: _QKey = frozenset((alias, c) for c in bare)
            keys.add(qk)
            lineage[qk] = facts.lineage.get(bare, ())
        return _QualifiedFacts(keys=frozenset(keys), lineage=lineage)


@dataclass(frozen=True, slots=True)
class _Projection:
    """The output-name mapping a ``SELECT`` projection list induces.

    ``aliased`` maps each input qualified column to the output names it
    appears under in the projection (a column can be projected multiple times
    under different aliases). ``star_aliases`` are aliases whose ``alias.*``
    appears; those let any column on that alias pass through with its base
    name. ``unrestricted_star`` is True when an unqualified ``*`` appears;
    every tracked column passes through with its base name. ``ambiguous``
    collects output names that resolve to more than one input qcol; keys
    using those columns can't be safely projected.
    """

    aliased: Mapping[_QCol, tuple[str, ...]]
    star_aliases: frozenset[str]
    unrestricted_star: bool
    ambiguous: frozenset[str]

    @staticmethod
    def build(sel: exp.Select, *, from_alias: str) -> _Projection:
        aliased: dict[_QCol, list[str]] = {}
        star_aliases: set[str] = set()
        unrestricted_star = False
        seen: dict[str, _QCol] = {}
        ambiguous: set[str] = set()

        def note(name: str, qc: _QCol) -> None:
            prior = seen.get(name)
            if prior is None:
                seen[name] = qc
            elif prior != qc:
                ambiguous.add(name)

        for proj in sel.expressions:
            if isinstance(proj, exp.Star):
                unrestricted_star = True
                continue
            if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                star_aliases.add(proj.table or from_alias)
                continue
            if isinstance(proj, exp.Alias):
                inner = proj.this
                if isinstance(inner, exp.Column):
                    qc: _QCol = (sg.column_table(inner) or from_alias, sg.column_name(inner))
                    name = proj.alias_or_name
                    aliased.setdefault(qc, []).append(name)
                    note(name, qc)
                continue
            if isinstance(proj, exp.Column):
                qc = (sg.column_table(proj) or from_alias, sg.column_name(proj))
                name = sg.column_name(proj)
                aliased.setdefault(qc, []).append(name)
                note(name, qc)
                continue
            # Other projection shapes (computed expressions without an alias,
            # window functions, function calls) don't produce a tractable
            # output column name we can attach a key to. Best-effort: silently
            # skip; the key may still survive if all its columns appear via
            # other projections or a star.

        return _Projection(
            aliased={qc: tuple(names) for qc, names in aliased.items()},
            star_aliases=frozenset(star_aliases),
            unrestricted_star=unrestricted_star,
            ambiguous=frozenset(ambiguous),
        )

    def map_key(self, key: _QKey) -> frozenset[str] | None:
        out: set[str] = set()
        for qc in key:
            names = self._output_names(qc)
            if not names:
                return None
            # A column can be projected under multiple names; one occurrence
            # in the output is enough for the key to hold there. Pick a
            # deterministic representative.
            out.add(min(names))
        return frozenset(out)

    def _output_names(self, qc: _QCol) -> Iterable[str]:
        out: list[str] = []
        if qc in self.aliased:
            out.extend(n for n in self.aliased[qc] if n not in self.ambiguous)
        if self.unrestricted_star or qc[0] in self.star_aliases:
            base = qc[1]
            if base not in self.ambiguous:
                out.append(base)
        return out


def _projected_output_names(sel: exp.Select) -> list[str]:
    """Output column names from `sel`'s projection list.

    Only counts projections that resolve to a named output column: bare
    ``exp.Column`` references and ``exp.Alias`` wrappers. Used to size the
    DISTINCT and UNION (distinct) full-tuple keys.
    """
    names: list[str] = []
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias):
            names.append(proj.alias_or_name)
        elif isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
            names.append(sg.column_name(proj))
    return names
