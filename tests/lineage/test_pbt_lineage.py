"""Property-based tests for lineage end-to-end.

The generator builds small dbt-shaped scenarios: sources, seeds, and
models with explicit projections, joined or chained. For each scenario
the test builds a real ``Manifest``, runs ``build_manifest_graph`` plus
``propagate`` for where-provenance, and asserts that every model column's
annotation equals the leaf-level closure computed structurally from the
scenario.

The generator deliberately stresses:

* Leaves with empty ``columns`` metadata (undocumented seeds).
* Multi-upstream JOINs whose projections mix columns from both sides.
* Same-column-reused-in-an-expression (``a.x + a.x``).
* Mixed-case identifiers.
* Column-name collisions across leaves.
* Aggregates over arbitrary projections.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.lineage import propagate
from dblect.lineage.builder import build_manifest_graph, build_model_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import where_provenance
from dblect.manifest import Column, Manifest, Node, ResourceType


@dataclass(frozen=True)
class LeafSpec:
    """A source or seed with a fixed column list.

    ``document_columns`` toggles whether the manifest's ``columns`` mapping is
    populated. ``False`` mirrors the real-world case of an undocumented seed
    or source: the SQL still references the columns, but the manifest knows
    nothing about them.
    """

    kind: ResourceType
    name: str
    columns: tuple[str, ...]
    document_columns: bool


@dataclass(frozen=True)
class Projection:
    """One output column on a model.

    ``sources`` is a tuple of ``(relation_name, column_name)`` pairs the
    projection draws from. Empty denotes a literal projection. Repeats in
    ``sources`` are deliberate and exercise ``a.x + a.x``-style expressions.
    ``aggregate`` wraps the expression in ``SUM`` when ``True`` (where-provenance
    of a SUM is the union of inputs, so ground truth doesn't change, but the
    aggregate dispatch path in the propagator is forced to fire).
    """

    out: str
    sources: tuple[tuple[str, str], ...]
    aggregate: bool


@dataclass(frozen=True)
class ModelSpec:
    name: str
    # Tuples of (alias, relation_name). A single-upstream model has one entry;
    # a JOIN model has two with distinct aliases.
    aliases: tuple[tuple[str, str], ...]
    projections: tuple[Projection, ...]


@dataclass(frozen=True)
class Scenario:
    leaves: tuple[LeafSpec, ...]
    models: tuple[ModelSpec, ...]


_COLUMN_NAMES = ("c0", "C1", "c_2")  # mixed-case to exercise case folding


@st.composite
def lineage_scenario(draw: st.DrawFn) -> Scenario:
    n_leaves = draw(st.integers(min_value=1, max_value=4))
    leaves: list[LeafSpec] = []
    for i in range(n_leaves):
        kind = draw(st.sampled_from([ResourceType.SOURCE, ResourceType.SEED]))
        n_cols = draw(st.integers(min_value=1, max_value=3))
        cols = _COLUMN_NAMES[:n_cols]
        leaves.append(
            LeafSpec(
                kind=kind,
                name=f"leaf_{i}",
                columns=cols,
                document_columns=draw(st.booleans()),
            )
        )

    n_models = draw(st.integers(min_value=1, max_value=4))
    models: list[ModelSpec] = []
    relations: list[tuple[str, tuple[str, ...]]] = [(le.name, le.columns) for le in leaves]

    for i in range(n_models):
        # Single-upstream or two-upstream join. Force single when there's only
        # one relation available.
        join_allowed = len(relations) >= 2
        shape = draw(st.sampled_from(("single", "join"))) if join_allowed else "single"

        if shape == "single":
            up_name, up_cols = draw(st.sampled_from(relations))
            aliases = (("u", up_name),)
            cols_per_alias = {"u": up_cols}
        else:
            idxs = draw(
                st.lists(
                    st.integers(min_value=0, max_value=len(relations) - 1),
                    min_size=2,
                    max_size=2,
                    unique=True,
                )
            )
            a_name, a_cols = relations[idxs[0]]
            b_name, b_cols = relations[idxs[1]]
            aliases = (("a", a_name), ("b", b_name))
            cols_per_alias = {"a": a_cols, "b": b_cols}

        # Flat list of (alias, col) the projection can draw from.
        available: list[tuple[str, str]] = [
            (alias, col) for alias, cols in cols_per_alias.items() for col in cols
        ]

        n_proj = draw(st.integers(min_value=1, max_value=4))
        projections: list[Projection] = []
        for k in range(n_proj):
            kind = draw(st.sampled_from(("passthrough", "expr", "literal")))
            aggregate = draw(st.booleans()) if kind != "literal" else False
            if kind == "literal":
                projections.append(Projection(out=f"o{k}", sources=(), aggregate=aggregate))
                continue
            if kind == "passthrough":
                alias, col = draw(st.sampled_from(available))
                relation = next(rn for a, rn in aliases if a == alias)
                projections.append(
                    Projection(out=f"o{k}", sources=((relation, col),), aggregate=aggregate)
                )
                continue
            # ``expr`` mixes two source columns (possibly with repeats and
            # possibly across both join sides).
            picks = draw(st.lists(st.sampled_from(available), min_size=2, max_size=3))
            srcs = tuple((next(rn for a, rn in aliases if a == alias), col) for alias, col in picks)
            projections.append(Projection(out=f"o{k}", sources=srcs, aggregate=aggregate))

        m_name = f"m_{i}"
        models.append(ModelSpec(name=m_name, aliases=aliases, projections=tuple(projections)))
        # Models are available as upstream relations for later models. Their
        # exposed column list is the set of output names.
        m_cols = tuple(dict.fromkeys(p.out for p in projections))
        relations.append((m_name, m_cols))

    return Scenario(leaves=tuple(leaves), models=tuple(models))


def _leaf_uid(le: LeafSpec) -> str:
    if le.kind is ResourceType.SOURCE:
        return f"source.test.raw.{le.name}"
    return f"seed.test.{le.name}"


def _model_uid(name: str) -> str:
    return f"model.test.{name}"


def _build_sql(m: ModelSpec) -> str:
    # Map relation name -> alias for the FROM/JOIN clause.
    alias_for: dict[str, str] = {rel: alias for alias, rel in m.aliases}
    parts: list[str] = []
    for p in m.projections:
        if not p.sources:
            parts.append(f"42 AS {p.out}")
            continue
        terms = [f"{alias_for[rel]}.{col}" for rel, col in p.sources]
        expr = " + ".join(terms) if len(terms) > 1 else terms[0]
        if p.aggregate:
            expr = f"SUM({expr})"
        parts.append(f"{expr} AS {p.out}")
    if len(m.aliases) == 1:
        alias, rel = m.aliases[0]
        from_clause = f"{rel} AS {alias}"
    else:
        (a_alias, a_rel), (b_alias, b_rel) = m.aliases
        from_clause = f"{a_rel} AS {a_alias} CROSS JOIN {b_rel} AS {b_alias}"
    return f"SELECT {', '.join(parts)} FROM {from_clause}"


def _leaf_node(le: LeafSpec) -> Node:
    columns: dict[str, Column] = {}
    if le.document_columns:
        for c in le.columns:
            columns[c] = Column(name=c, data_type=None, description=None)
    return Node(
        unique_id=_leaf_uid(le),
        name=le.name,
        resource_type=le.kind,
        fqn=("test", le.name),
        package_name="test",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns=columns,
        depends_on=frozenset(),
    )


def _model_node(m: ModelSpec, upstream_uids: frozenset[str]) -> Node:
    sql = _build_sql(m)
    return Node(
        unique_id=_model_uid(m.name),
        name=m.name,
        resource_type=ResourceType.MODEL,
        fqn=("test", m.name),
        package_name="test",
        schema=None,
        raw_code=sql,
        compiled_code=sql,
        original_file_path=None,
        columns={
            p.out: Column(name=p.out, data_type=None, description=None) for p in m.projections
        },
        depends_on=upstream_uids,
    )


def _build_manifest(scenario: Scenario) -> Manifest:
    nodes: dict[str, Node] = {}
    name_to_uid: dict[str, str] = {}
    for le in scenario.leaves:
        n = _leaf_node(le)
        nodes[n.unique_id] = n
        name_to_uid[le.name] = n.unique_id
    for m in scenario.models:
        upstream_uids = frozenset(name_to_uid[rel] for _, rel in m.aliases)
        n = _model_node(m, upstream_uids=upstream_uids)
        nodes[n.unique_id] = n
        name_to_uid[m.name] = n.unique_id
    return Manifest(schema_version="x", adapter_type="duckdb", nodes=nodes)


def _ground_truth(scenario: Scenario) -> dict[tuple[str, str], frozenset[tuple[str, str]]]:
    """Per relation column (case-folded), the set of leaf-level
    ``(leaf_name, column)`` tuples it pulls from.

    Folds to lowercase to match the graph's case-folding behaviour.
    """
    closure: dict[tuple[str, str], frozenset[tuple[str, str]]] = {}
    for le in scenario.leaves:
        for c in le.columns:
            closure[(le.name, c.lower())] = frozenset({(le.name, c.lower())})
    for m in scenario.models:
        for p in m.projections:
            srcs: frozenset[tuple[str, str]] = frozenset()
            for rel, col in p.sources:
                srcs = srcs | closure[(rel, col.lower())]
            closure[(m.name, p.out.lower())] = srcs
    return closure


def _leaf_source_ref(scenario: Scenario, leaf_name: str) -> SourceRef:
    le = next(le for le in scenario.leaves if le.name == leaf_name)
    kind = SourceKind.SOURCE if le.kind is ResourceType.SOURCE else SourceKind.SEED
    return SourceRef(kind=kind, unique_id=_leaf_uid(le))


@given(lineage_scenario())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_pbt_propagator_matches_ground_truth(scenario: Scenario) -> None:
    """The propagator's annotation must equal the structurally-defined closure.

    Walks every model column the generator produced and compares the
    propagator's union-semiring annotation against the closure computed
    directly from the scenario's projection lists. The two should agree by
    construction: where-provenance is just set union over leaf identities,
    and the scenario already records exactly which leaf columns feed each
    output.
    """
    manifest = _build_manifest(scenario)
    result = build_manifest_graph(manifest)
    assert result.issues == (), [(i.model_unique_id, i.message) for i in result.issues]

    anns = propagate(result.graph, where_provenance)
    gt = _ground_truth(scenario)

    for m in scenario.models:
        model_src = SourceRef(kind=SourceKind.MODEL, unique_id=_model_uid(m.name))
        for p in m.projections:
            col = ColumnRef(source=model_src, column=p.out.lower())
            expected = frozenset(
                ColumnRef(source=_leaf_source_ref(scenario, leaf_name), column=leaf_col)
                for leaf_name, leaf_col in gt[(m.name, p.out.lower())]
            )
            assert anns[col].value == expected, (
                f"mismatch on {m.name}.{p.out}: sql={_build_sql(m)!r} "
                f"expected={sorted(repr(c) for c in expected)} "
                f"got={sorted(repr(c) for c in anns[col].value)}"
            )


@given(lineage_scenario())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_pbt_edges_are_immediate_upstream(scenario: Scenario) -> None:
    """Per-model edges must land on the immediate upstream relation(s) only.

    For each model output column the recorded ``edges`` set must contain
    exactly one entry per distinct ``(upstream_relation, column)`` pair the
    projection draws from. This pins the documented invariant that ``edges``
    is the one-step relation, with the propagator doing all transitive
    stitching.
    """
    manifest = _build_manifest(scenario)
    result = build_manifest_graph(manifest)
    assert result.issues == ()

    leaf_names = {le.name for le in scenario.leaves}

    for m in scenario.models:
        model_src = SourceRef(kind=SourceKind.MODEL, unique_id=_model_uid(m.name))
        for p in m.projections:
            col = ColumnRef(source=model_src, column=p.out.lower())
            edges = result.graph.edges[col]
            expected: set[ColumnRef] = set()
            for rel, src_col in p.sources:
                if rel in leaf_names:
                    expected.add(
                        ColumnRef(source=_leaf_source_ref(scenario, rel), column=src_col.lower())
                    )
                else:
                    expected.add(
                        ColumnRef(
                            source=SourceRef(kind=SourceKind.MODEL, unique_id=_model_uid(rel)),
                            column=src_col.lower(),
                        )
                    )
            assert edges == frozenset(expected), (
                f"edge mismatch on {m.name}.{p.out}: sql={_build_sql(m)!r} "
                f"expected={sorted(repr(c) for c in expected)} "
                f"got={sorted(repr(c) for c in edges)}"
            )


# --- CTE-shaped models ---


@dataclass(frozen=True)
class CTEScenario:
    """A single-leaf scenario whose only model uses a CTE.

    ``wrap`` decides whether each intermediate is bare or wrapped in a
    structural operator (``coalesce``, ``case``, ``SUM``). All wrappings
    preserve set-of-leaves for where-provenance — they stress the CTE
    materialisation on expression shapes downstream properties (nullability,
    aggregate detection) will care about.
    """

    leaf: LeafSpec
    intermediates: tuple[Projection, ...]
    outer: tuple[Projection, ...]
    wrap: str  # "none" | "coalesce" | "case" | "aggregate"


@st.composite
def _cte_scenario(
    draw: st.DrawFn, *, multi_source: bool, wrap_choices: tuple[str, ...]
) -> CTEScenario:
    """Generator for CTE scenarios.

    ``multi_source=False`` restricts CTE intermediates to single-column
    references. ``multi_source=True`` forces at least one intermediate to
    combine two or more upstream columns.

    ``wrap_choices`` is the set of structural wrappings the generator
    samples from for the CTE's intermediate expressions. Pass
    ``("none",)`` to keep intermediates bare; pass the full set to stress
    the substrate on coalesce / case / aggregate-wrapped intermediates.
    """
    n_cols = draw(st.integers(min_value=2, max_value=3))
    leaf = LeafSpec(
        kind=ResourceType.SOURCE,
        name="leaf_0",
        columns=_COLUMN_NAMES[:n_cols],
        document_columns=True,
    )
    n_inter = draw(st.integers(min_value=1, max_value=3))
    intermediates: list[Projection] = []
    seen_names: set[str] = set()
    for k in range(n_inter):
        # Stable, unique names for intermediates.
        name = f"i{k}"
        seen_names.add(name)
        if multi_source and len(leaf.columns) >= 2:
            picks = draw(st.lists(st.sampled_from(leaf.columns), min_size=2, max_size=3))
            srcs = tuple(("leaf_0", c) for c in picks)
        else:
            col = draw(st.sampled_from(leaf.columns))
            srcs = (("leaf_0", col),)
        intermediates.append(Projection(out=name, sources=srcs, aggregate=False))

    inter_names = tuple(p.out for p in intermediates)
    n_outer = draw(st.integers(min_value=1, max_value=3))
    outer: list[Projection] = []
    for k in range(n_outer):
        col = draw(st.sampled_from(inter_names))
        outer.append(Projection(out=f"o{k}", sources=(("__cte__", col),), aggregate=False))

    wrap = draw(st.sampled_from(wrap_choices))
    return CTEScenario(leaf=leaf, intermediates=tuple(intermediates), outer=tuple(outer), wrap=wrap)


def _wrap_inner_expr(terms: list[str], wrap: str) -> str:
    """Apply the chosen structural wrapping to a CTE intermediate's expression.

    ``case`` falls back to the bare expression when only one source term
    is present (a CASE needs at least one WHEN plus an ELSE). All
    wrappings preserve set-of-leaves: every original source term still
    appears as a Column reference inside the wrapped expression, so
    where-provenance stays unchanged regardless of duplicates in ``terms``.
    """
    bare = " + ".join(terms) if len(terms) > 1 else terms[0]
    if wrap == "coalesce":
        return f"COALESCE({', '.join(terms)})"
    if wrap == "case" and len(terms) >= 2:
        # Chain a WHEN per term-except-last and put the last in the ELSE.
        # Every term appears at least once in the produced expression, so
        # the leaf-union ground truth holds even when ``terms`` repeats
        # the same column (the generator does this on purpose).
        when_clauses = " ".join(f"WHEN {t} > 0 THEN {t}" for t in terms[:-1])
        return f"CASE {when_clauses} ELSE {terms[-1]} END"
    if wrap == "aggregate":
        return f"SUM({bare})"
    return bare


def _build_cte_sql(s: CTEScenario) -> str:
    inner_parts: list[str] = []
    for p in s.intermediates:
        terms = [f"a.{col}" for _, col in p.sources]
        expr = _wrap_inner_expr(terms, s.wrap)
        inner_parts.append(f"{expr} AS {p.out}")
    inner = f"SELECT {', '.join(inner_parts)} FROM {s.leaf.name} AS a"
    # When intermediates are aggregated, SQL requires a GROUP BY for any
    # non-aggregated columns. Aggregating every intermediate avoids the
    # mixed-aggregation-without-grouping error: the CTE becomes a fully
    # aggregated query that returns one row, which is well-formed.
    outer_parts: list[str] = []
    for p in s.outer:
        col = p.sources[0][1]
        outer_parts.append(f"r.{col} AS {p.out}")
    outer = f"SELECT {', '.join(outer_parts)} FROM r"
    return f"WITH r AS ({inner}) {outer}"


def _cte_ground_truth(s: CTEScenario) -> dict[str, frozenset[ColumnRef]]:
    """Per outer column name, the leaf-level ColumnRef closure it pulls from."""
    leaf_src = SourceRef(
        kind=SourceKind.SOURCE,
        unique_id=f"source.test.raw.{s.leaf.name}",
    )
    inter_closure: dict[str, frozenset[ColumnRef]] = {}
    for p in s.intermediates:
        inter_closure[p.out] = frozenset(
            ColumnRef(source=leaf_src, column=col.lower()) for _, col in p.sources
        )
    return {p.out: inter_closure[p.sources[0][1]] for p in s.outer}


def _run_cte(s: CTEScenario) -> dict[str, frozenset[ColumnRef]]:
    sql = _build_cte_sql(s)
    leaf_src = SourceRef(kind=SourceKind.SOURCE, unique_id=f"source.test.raw.{s.leaf.name}")
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={s.leaf.name: leaf_src},
        schema={s.leaf.name: dict.fromkeys(s.leaf.columns, "INT")},
    )
    anns = propagate(graph, where_provenance)
    model_src = SourceRef(kind=SourceKind.MODEL, unique_id="model.test.m")
    return {p.out: anns[ColumnRef(source=model_src, column=p.out.lower())].value for p in s.outer}


_WRAP_BARE = ("none",)
_WRAP_ALL = ("none", "coalesce", "case", "aggregate")


@given(_cte_scenario(multi_source=False, wrap_choices=_WRAP_ALL))
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_pbt_cte_single_source_intermediates_match_ground_truth(s: CTEScenario) -> None:
    """Single-column-reference CTE intermediates, with structural wrappings.

    Whether the intermediate is bare, coalesce-wrapped, case-wrapped, or
    aggregate-wrapped, the outer column's where-provenance must equal the
    singleton leaf set the intermediate names. The wrapping changes the
    expression structure (which downstream properties care about) but not
    the set of source columns reachable from it (which where-provenance
    pins).
    """
    got = _run_cte(s)
    gt = _cte_ground_truth(s)
    for out_name, expected in gt.items():
        assert got[out_name] == expected, (
            f"{out_name}: sql={_build_cte_sql(s)!r} "
            f"expected={sorted(repr(c) for c in expected)} "
            f"got={sorted(repr(c) for c in got[out_name])}"
        )


@given(_cte_scenario(multi_source=True, wrap_choices=_WRAP_ALL))
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_pbt_cte_multi_source_intermediates_match_ground_truth(s: CTEScenario) -> None:
    """Multi-column CTE intermediates, with structural wrappings.

    A CTE intermediate like ``COALESCE(a.x, a.y)`` or ``SUM(a.x + a.y)``
    references several upstream columns; the outer projection only sees
    one ``exp.Column`` pointing at the intermediate. The intermediate is
    its own graph entry whose expression carries the wrapping, so the
    propagator walks the wrapping and recurses through the stamps,
    recovering every leaf.
    """
    got = _run_cte(s)
    gt = _cte_ground_truth(s)
    for out_name, expected in gt.items():
        assert got[out_name] == expected, (
            f"{out_name}: sql={_build_cte_sql(s)!r} "
            f"expected={sorted(repr(c) for c in expected)} "
            f"got={sorted(repr(c) for c in got[out_name])}"
        )


# --- UNION ALL scenarios ---


@dataclass(frozen=True)
class UnionScenario:
    """Two-arm UNION ALL between two single-leaf SELECTs, wrapped in a
    subquery the outer model SELECTs from.

    Both arms project the same column names off two different leaves.
    Outer where-provenance for each output column is exactly the
    union of the two arms' leaf columns.
    """

    n_columns: int


@st.composite
def _union_scenario(draw: st.DrawFn) -> UnionScenario:
    return UnionScenario(n_columns=draw(st.integers(min_value=1, max_value=3)))


_UNION_LEAVES = ("leaf_a", "leaf_b")
_UNION_COLS = ("c0", "c1", "c2")


def _build_union_sql(s: UnionScenario) -> str:
    out_names = tuple(f"out{i}" for i in range(s.n_columns))
    arm_a = ", ".join(f"a.{_UNION_COLS[i]} AS {out_names[i]}" for i in range(s.n_columns))
    arm_b = ", ".join(f"b.{_UNION_COLS[i]} AS {out_names[i]}" for i in range(s.n_columns))
    inner = f"SELECT {arm_a} FROM leaf_a a UNION ALL SELECT {arm_b} FROM leaf_b b"
    outer = ", ".join(f"u.{c} AS {c}" for c in out_names)
    return f"SELECT {outer} FROM ({inner}) u"


def _union_ground_truth(s: UnionScenario) -> dict[str, frozenset[ColumnRef]]:
    leaf_a = SourceRef(SourceKind.SOURCE, "source.test.raw.leaf_a")
    leaf_b = SourceRef(SourceKind.SOURCE, "source.test.raw.leaf_b")
    out: dict[str, frozenset[ColumnRef]] = {}
    for i in range(s.n_columns):
        out[f"out{i}"] = frozenset(
            {ColumnRef(leaf_a, _UNION_COLS[i]), ColumnRef(leaf_b, _UNION_COLS[i])}
        )
    return out


@given(_union_scenario())
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_pbt_union_all_arms_match_ground_truth(s: UnionScenario) -> None:
    """A UNION ALL between two arms unions their leaves at the outer column.

    The union's combined output is a synthetic ``UnionConfluence`` node
    plus-folded by the propagator. For where-provenance ``plus`` is set
    union, so the outer column equals the union of each arm's leaves.
    """
    sql = _build_union_sql(s)
    leaf_a = SourceRef(SourceKind.SOURCE, "source.test.raw.leaf_a")
    leaf_b = SourceRef(SourceKind.SOURCE, "source.test.raw.leaf_b")
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"leaf_a": leaf_a, "leaf_b": leaf_b},
        schema={
            "leaf_a": dict.fromkeys(_UNION_COLS[: s.n_columns], "INT"),
            "leaf_b": dict.fromkeys(_UNION_COLS[: s.n_columns], "INT"),
        },
    )
    anns = propagate(graph, where_provenance)
    model_src = SourceRef(SourceKind.MODEL, "model.test.m")
    gt = _union_ground_truth(s)
    for out_name, expected in gt.items():
        got = anns[ColumnRef(model_src, out_name)].value
        assert got == expected, (
            f"{out_name}: sql={sql!r} "
            f"expected={sorted(repr(c) for c in expected)} "
            f"got={sorted(repr(c) for c in got)}"
        )


# Public aliases for scenario helpers reused by sibling tests
# (test_pbt_nullability_monotone).
build_manifest = _build_manifest
leaf_source_ref = _leaf_source_ref
cte_scenario = _cte_scenario
build_cte_sql = _build_cte_sql
