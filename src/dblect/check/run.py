"""Run the declaration check: resolve contracts, propagate, derive findings.

The pipeline is the substrate-first story made user-facing. Contracts resolve into
the facts the properties ground from, two properties propagate (the
functional-dependency property over the relation graph, then the domain-type
property over the column graph reading it), and the findings fall out of what the
substrate concluded:

* a contract that did not resolve is a finding directly off the bridge;
* a column whose flow value is provisional carries a declared type the inferred
  one contradicts, which is currency creep, and it lands wherever the taint
  reached, declared model or not;
* an aggregate whose tag cleared to naked while an operand it summed still carried
  one is a reduction the algebra cannot call well typed, the mixed-currency sum.

Predicates are collected and counted, not run: executing them needs materialized
data, which belongs to the fixture/PBT loop, so the static check stays static.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.audit.sourcemap import LineMap, SourceSpan, build_line_map
from dblect.audit.suppress import FramedDirectives, apply
from dblect.check.coverage import GroundingCoverage, PropertyGrounding, ResolutionCoverage
from dblect.check.findings import (
    CheckFinding,
    CheckFindingKind,
    CheckReport,
    SuppressedCheckFinding,
    UnbuiltModel,
)
from dblect.lineage.builder import (
    BuildIssue,
    BuildResult,
    RelationBuildResult,
    build_manifest_graph,
    build_relation_graph,
)
from dblect.lineage.facts.model import BASE_WORLD, Annotation, Fact, WorldRef
from dblect.lineage.facts.property import CoherenceClear
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import (
    ColumnLineageGraph,
    ColumnRef,
    SourceKind,
    SourceRef,
)
from dblect.lineage.properties.domain_type import (
    NAKED,
    DomainTag,
    domain_type_display,
    domain_type_grounded_scopes,
    domain_type_grounding,
    domain_type_property,
    join_key_conflicts,
)
from dblect.lineage.properties.functional_dependency import (
    FDSet,
    functional_dependency_grounded_scopes,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.property import propagate, resolved_column_ref
from dblect.manifest import Manifest
from dblect.sql import AggregateBehavior, aggregate_behavior
from dblect.sql import _sqlglot as sg
from dblect.sql.parse import parse_manifest_models
from dblect.types import ContractRegistry, ResolvedContracts, active_registry, resolve_contracts


@dataclass(frozen=True, slots=True)
class CheckGraphs:
    """The world-invariant build: resolved contracts and the two stamped graphs,
    built once and reused across worlds.

    The builds stamp ``SourceRef``/``ColumnRef`` onto the parsed trees in place;
    those stamps encode graph identity, not flag values, and ``propagate`` never
    mutates the trees, so one build is safe to share across every world a run
    enumerates. Do not mutate the graphs or their trees per world."""

    manifest: Manifest
    resolved: ResolvedContracts
    relation_build: RelationBuildResult
    column_build: BuildResult
    contracts_resolved: int
    parsed: Mapping[str, Expr]
    """The stamped statement tree of each model the column build built, parsed once and
    fed to both builds so the SQL is parsed a single time and the column build's
    ``ColumnRef`` stamps land on the trees the check keeps. The join-key check reads
    ON-clause columns off these (a projection derivation alone does not carry the join).
    A model the build skipped (a compilation miss, a build error) is absent, so the check
    never reads an unstamped tree as a clean one."""
    join_key_ground: Callable[[ColumnRef], Annotation[DomainTag]]
    """The declared-grounding fallback the join-key check reads a never-projected key's
    tag through. Folded once here, alongside the graph it is judged against, rather than
    per world: it derives from the world-invariant declared facts, so colocating it with
    the graph build keeps it in step with the graph instead of resting on the assumption
    that successive worlds share one."""


@dataclass(frozen=True, slots=True)
class WorldFacts:
    """The facts that ground one world's propagation. The declared facts are
    world-invariant (shared across worlds); a world's ``CompileValue`` leaves are
    appended by the enumerator. Keeping the two apart means the enumerator only
    adds, never recomputes, the declared facts."""

    world: WorldRef
    fd_facts: tuple[Fact[FDSet, SourceRef], ...]
    tag_facts: tuple[Fact[DomainTag, ColumnRef], ...]


@dataclass(frozen=True, slots=True)
class WorldAnnotations:
    """One world's propagation result, keyed by the world it holds under. A bundle
    rather than a bare mapping so a later property can ride alongside the domain-type
    annotations without changing ``propagate_world``'s signature.

    ``coherence_clears`` are the aggregate-guard clears the domain-type walk emitted in
    this world: the structured reason a sum cleared its tag, which the aggregation
    finding reads instead of re-inferring the event from the cleared output."""

    world: WorldRef
    domain_type: Mapping[ColumnRef, Annotation[DomainTag]]
    coherence_clears: tuple[CoherenceClear[DomainTag], ...] = ()


def build_check_graphs(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    registry: ContractRegistry | None = None,
    trees: Mapping[str, Expr] | None = None,
) -> CheckGraphs:
    """Resolve contracts and build both lineage graphs once. ``profile`` supplies the
    dialect every model parses under; the result is world-invariant and reusable
    across an enumeration.

    ``trees`` lets a caller share already-parsed model trees (``analyze`` parses once and
    feeds both families) so the SQL is parsed a single time; omitted, this parses them."""
    reg = registry if registry is not None else active_registry()
    resolved = resolve_contracts(manifest, registry=reg)
    dialect = profile.sqlglot_dialect
    if trees is None:
        _, trees = parse_manifest_models(manifest, dialect=dialect)
    relation_build = build_relation_graph(manifest, dialect=dialect, parsed=trees)
    column_build = build_manifest_graph(manifest, dialect=dialect, parsed=trees)
    # A model the column build skipped (a compilation miss, a build error) leaves its tree
    # unstamped; drop it so the join-key check reads only stamped trees and its model set
    # matches the build's rather than diverging onto an unvalidated one.
    unbuilt = {issue.model_unique_id for issue in column_build.issues}
    parsed = {uid: tree for uid, tree in trees.items() if uid not in unbuilt}
    return CheckGraphs(
        manifest=manifest,
        resolved=resolved,
        relation_build=relation_build,
        column_build=column_build,
        contracts_resolved=len(reg.contracts),
        parsed=parsed,
        join_key_ground=domain_type_grounding(_by_scope(resolved.tag_facts)),
    )


def base_world_facts(resolved: ResolvedContracts) -> WorldFacts:
    """The single manifest's declared facts under ``BASE_WORLD``: the facts the
    one-world analysis grounds from, with no flag enumeration active."""
    return WorldFacts(world=BASE_WORLD, fd_facts=resolved.fd_facts, tag_facts=resolved.tag_facts)


def propagate_world(graphs: CheckGraphs, facts: WorldFacts) -> WorldAnnotations:
    """Propagate the FD property over the relation graph, then domain type over the
    column graph reading it, grounding from one world's ``facts``. Pure in
    ``graphs``: a fresh ``AnnotationStore`` per call and no mutation of the shared
    build, so worlds re-run independently."""
    fd_prop = functional_dependency_property(
        functional_dependency_grounding(_by_scope(facts.fd_facts))
    )
    store = AnnotationStore()
    for scope, ann in propagate(graphs.relation_build.graph, fd_prop).items():
        store.record(fd_prop.name, scope, ann)

    dt_prop = domain_type_property(
        domain_type_grounding(_by_scope(facts.tag_facts)),
        fd=fd_prop.ref,
    )
    ctx = PropertyRegistry((fd_prop, dt_prop)).dep_context(store)
    clears: list[CoherenceClear[DomainTag]] = []
    domain_type = propagate(graphs.column_build.graph, dt_prop, dep_context=ctx, sink=clears)
    return WorldAnnotations(
        world=facts.world, domain_type=domain_type, coherence_clears=tuple(clears)
    )


def run_check(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    registry: ContractRegistry | None = None,
    resolution_floor: float | None = None,
    graphs: CheckGraphs | None = None,
) -> CheckReport:
    """Resolve the registered contracts against ``manifest``, propagate, and return
    the declaration-level findings. ``profile`` is the run's resolved target, whose
    dialect parses every model. ``registry`` defaults to the active one (the loader
    populates a fresh registry the CLI passes in).

    ``resolution_floor`` is the minimum share of column references the propagator
    must resolve before a clean report is trustworthy; when set and the project
    falls under it, a ``RESOLUTION_BELOW_FLOOR`` finding fires. It keys on
    resolution only, never on grounding.

    The single-world entry: it builds the graphs once and propagates the base world,
    the same sequence the enumerator runs per world.

    This is the declaration-level family alone. A consumer that needs every family's
    findings over a manifest (any multi-world or finding-threading path) calls
    :func:`dblect.analysis.analyze` instead, which carries both families so a family
    is never dropped by being forgotten.

    ``graphs`` lets :func:`dblect.analysis.analyze` pass graphs it already built (and shares
    with the structural audit) so the lineage graphs are built once per run; omitted, this
    builds them."""
    if graphs is None:
        graphs = build_check_graphs(manifest, profile, registry=registry)
    world = propagate_world(graphs, base_world_facts(graphs.resolved))

    resolution = ResolutionCoverage.from_models(graphs.column_build.resolution)
    grounding = _grounding_coverage(graphs.resolved, graphs.relation_build, world.domain_type)

    findings: list[CheckFinding] = []
    findings.extend(_issue_findings(graphs.resolved))
    findings.extend(world_findings(graphs, world))
    findings.extend(_resolution_floor_findings(resolution, resolution_floor))

    active, suppressed = suppress_check_findings(findings, manifest)

    return CheckReport(
        findings=active,
        load_issues=(),
        unbuilt=_unbuilt(graphs.relation_build.issues, graphs.column_build.issues),
        suppressed=suppressed,
        contracts_resolved=graphs.contracts_resolved,
        models_propagated=len(manifest.models),
        predicates_collected=len(graphs.resolved.predicates),
        resolution=resolution,
        grounding=grounding,
    )


def suppress_check_findings(
    findings: Iterable[CheckFinding], manifest: Manifest
) -> tuple[tuple[CheckFinding, ...], tuple[SuppressedCheckFinding, ...]]:
    """Apply ``-- noqa`` directives to declaration-level findings.

    Directives are read per model from both the developer's template and the compiled SQL,
    the same two frames the structural audit reads, and each finding is matched in the
    frame(s) it occupies: a back-mapped finding against the template, and a macro-emitted
    one against the compiled SQL where the macro body's ``-- noqa`` renders. A finding with
    no model or no located line (a contract-resolution issue, a project-wide coverage
    finding) carries no line, so the matcher leaves it active; only the line-located
    domain-type and aggregation findings are silenceable this way."""
    directives_by_model: dict[str, FramedDirectives] = {}
    active: list[CheckFinding] = []
    suppressed: list[SuppressedCheckFinding] = []
    for finding in findings:
        uid = finding.model_unique_id
        node = manifest.models.get(uid) if uid is not None else None
        if uid is None or node is None:
            active.append(finding)
            continue
        if uid not in directives_by_model:
            directives_by_model[uid] = FramedDirectives.for_node(node)
        kept, hidden = apply((finding,), directives_by_model[uid])
        active.extend(kept)
        suppressed.extend(
            SuppressedCheckFinding(
                finding=f, directive_line=d.line, bare=d.kinds is None, directive_in_compiled=ic
            )
            for f, d, ic in hidden
        )
    return tuple(active), tuple(suppressed)


def world_findings(graphs: CheckGraphs, world: WorldAnnotations) -> list[CheckFinding]:
    """The findings that vary by world: the domain-type contradictions and the
    not-well-typed aggregations, read off one world's annotations. The
    contract-resolution and resolution-floor findings are world-invariant and stay
    ``run_check``'s to report once."""
    findings: list[CheckFinding] = []
    # One source-map per model, shared across both finding kinds: a model that produces
    # both a contradiction and an aggregation finding builds its line map once.
    line_maps: dict[str, LineMap] = {}
    findings.extend(
        _contradiction_findings(
            graphs.manifest, world.domain_type, graphs.column_build.graph, line_maps
        )
    )
    findings.extend(
        _aggregation_findings(
            graphs.manifest,
            world.coherence_clears,
            graphs.column_build.graph,
            line_maps,
        )
    )
    findings.extend(
        _join_key_findings(
            graphs.manifest,
            graphs.parsed,
            world.domain_type,
            graphs.join_key_ground,
            line_maps,
        )
    )
    return findings


def _unbuilt(*issue_groups: tuple[BuildIssue, ...]) -> tuple[UnbuiltModel, ...]:
    """The models no graph could analyze, one entry per model. A model can fail in
    both the relation and column builds; the first reason seen wins, and the column
    build (the domain-type path) is passed first so its reason is preferred."""
    reasons: dict[str, str] = {}
    for group in issue_groups:
        for issue in group:
            reasons.setdefault(issue.model_unique_id, issue.message)
    return tuple(UnbuiltModel(uid, reason) for uid, reason in sorted(reasons.items()))


# --- propagation ----------------------------------------------------------------


_V = TypeVar("_V")
_S = TypeVar("_S", ColumnRef, SourceRef)


def _by_scope(facts: tuple[Fact[_V, _S], ...]) -> dict[_S, tuple[Fact[_V, _S], ...]]:
    grouped: dict[_S, list[Fact[_V, _S]]] = {}
    for fact in facts:
        grouped.setdefault(fact.scope, []).append(fact)
    return {scope: tuple(items) for scope, items in grouped.items()}


# --- coverage --------------------------------------------------------------------


def _grounding_coverage(
    resolved: ResolvedContracts,
    relation_build: RelationBuildResult,
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
) -> GroundingCoverage:
    """Per-property grounding over the property's resolved subjects, plus the
    contract-scoped slice.

    "Resolved" is the set of columns the propagator actually reached: the keys of
    the domain-type annotation map, which includes source leaves the lineage flows
    from (these are absent from ``graph.subjects()``, yet a contract on a source
    column is genuinely checkable). "Grounded" reads the property's own grounding
    fold (``*_grounded_scopes``), the very fold ``_propagate`` grounds from, so the
    coverage number cannot drift from what was actually grounded. The per-property
    denominator keeps only model-kind columns, since the synthetic CTE and UNION
    scaffolding is internal, not a column a fact would ever ground."""
    resolved_columns = set(annotations)
    model_columns = _model_scopes(resolved_columns)
    relation_subjects = _model_scopes(relation_build.graph.subjects())

    tag_scopes = domain_type_grounded_scopes(_by_scope(resolved.tag_facts))
    fd_scopes = functional_dependency_grounded_scopes(_by_scope(resolved.fd_facts))

    by_property = (
        PropertyGrounding(
            property_name="domain_type",
            grounded=len(tag_scopes & model_columns),
            resolved=len(model_columns),
        ),
        PropertyGrounding(
            property_name="functional_dependency",
            grounded=len(fd_scopes & relation_subjects),
            resolved=len(relation_subjects),
        ),
    )
    # The columns contracts name are the column scopes a contract fact lands on;
    # "checkable" means lineage reached them, so a declared type is actually
    # propagated rather than silently uncheckable on a model that did not build.
    return GroundingCoverage(
        by_property=by_property,
        contract_columns=len(tag_scopes),
        contract_columns_checkable=len(tag_scopes & resolved_columns),
    )


def _model_scopes(subjects: Iterable[ColumnRef | SourceRef]) -> set[ColumnRef | SourceRef]:
    """The model-kind subjects among ``subjects``; synthetic scaffolding dropped."""
    out: set[ColumnRef | SourceRef] = set()
    for s in subjects:
        ref = s.source if isinstance(s, ColumnRef) else s
        if ref.kind is SourceKind.MODEL:
            out.add(s)
    return out


def _resolution_floor_findings(
    resolution: ResolutionCoverage, floor: float | None
) -> list[CheckFinding]:
    if floor is None or not resolution.below(floor):
        return []
    frac = resolution.fraction or 0.0
    # Only models that actually fell blind are worth naming; a fully-resolved
    # model is not "lowest" however the ranking sorted it.
    worst = ", ".join(
        f"{m.unique_id} ({m.resolved_columns}/{m.sites})"
        for m in resolution.worst_models
        if m.resolved_columns < m.sites
    )
    tail = f"; lowest: {worst}" if worst else ""
    return [
        CheckFinding(
            kind=CheckFindingKind.RESOLUTION_BELOW_FLOOR,
            message=(
                f"resolved {resolution.resolved_columns}/{resolution.sites} columns "
                f"({frac:.1%}), below the {floor:.1%} floor; "
                f"analysis covers only what was resolved{tail}"
            ),
            model_unique_id=None,
        )
    ]


# --- finding derivation ---------------------------------------------------------


def _issue_findings(resolved: ResolvedContracts) -> list[CheckFinding]:
    return [
        CheckFinding(
            kind=CheckFindingKind.CONTRACT_ISSUE,
            message=issue.message,
            model_unique_id=None,
            contract=issue.contract,
            column=issue.field,
            code=issue.code,
        )
        for issue in resolved.issues
    ]


def _contradiction_findings(
    manifest: Manifest,
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
    column_graph: ColumnLineageGraph,
    line_maps: dict[str, LineMap],
) -> list[CheckFinding]:
    """One finding per column whose flow value is provisional: a declared type the
    inferred one contradicts, reported wherever the taint reached."""
    out: list[CheckFinding] = []
    for ref, ann in _sorted(annotations):
        if not ann.provisional or ann.value == NAKED:
            continue
        line_start, line_end = _span_of(column_graph.derivation(ref))
        uid = ref.source.unique_id
        out.append(
            CheckFinding(
                kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
                message=(
                    f"declared domain type for {ref.column!r} is contradicted by the type "
                    "that flows in from upstream"
                ),
                model_unique_id=uid,
                file_path=_file_of(manifest, ref.source),
                column=ref.column,
                line_start=line_start,
                line_end=line_end,
                source_span=_source_span(manifest, uid, line_start, line_end, line_maps),
            )
        )
    return out


def _aggregation_findings(
    manifest: Manifest,
    clears: tuple[CoherenceClear[DomainTag], ...],
    column_graph: ColumnLineageGraph,
    line_maps: dict[str, LineMap],
) -> list[CheckFinding]:
    """One finding per combining aggregate the coherence guard cleared: a reduction
    over a value whose per-row companion nothing holds constant per group.

    The guard already decided this and recorded *why* in the clear, so the check reads
    the event rather than re-inferring it from an ambiguous ``output == NAKED`` (which
    cannot tell a cleared live tag from an operand that was naked before the reduction).
    A ``SELECT`` clear (``min``/``max`` widening a tag-blind selection) is not this
    finding, so only the combining class is rendered. A clear whose site is unstamped
    (a windowed aggregate) is the deferred windowed obligation, left for later; an opaque
    group shape (a positional or computed GROUP BY, ``group_refs is None``) is surfaced,
    since the companion is no more held by a group the builder cannot enumerate than by a
    resolved one that omits it, and the guard recorded the clear rather than guessing."""
    if not clears:
        return []
    owners = _aggregate_owners(column_graph)
    out: list[CheckFinding] = []
    for clear in clears:
        agg = clear.aggregate
        if aggregate_behavior(agg) is not AggregateBehavior.COMBINE:
            continue
        site = clear.site
        if site is None:
            continue
        owner = owners.get(id(agg))
        if owner is None:
            continue
        # The aggregate node pins the line; the projection derivation is the fallback so
        # the finding still lands near the right place when the aggregate carries no
        # stamped line (a literal-only shape).
        line_start, line_end = _span_of(agg, column_graph.derivation(owner))
        uid = owner.source.unique_id
        out.append(
            CheckFinding(
                kind=CheckFindingKind.AGGREGATION_NOT_WELL_TYPED,
                message=_aggregation_message(owner, clear),
                model_unique_id=uid,
                file_path=_file_of(manifest, owner.source),
                column=owner.column,
                line_start=line_start,
                line_end=line_end,
                source_span=_source_span(manifest, uid, line_start, line_end, line_maps),
            )
        )
    # The clear order follows the propagation walk; sort so the report is deterministic.
    out.sort(key=lambda f: (f.model_unique_id or "", f.column or "", f.line_start))
    return out


def _aggregate_owners(column_graph: ColumnLineageGraph) -> dict[int, ColumnRef]:
    """Map each aggregate call's identity to the output column whose derivation holds
    it, so a clear (which carries the call, not the output) lands on its column and line.

    The propagator walked these very derivations, so the ``AggFunc`` in a clear is the
    same object found here; keyed on ``id`` because two distinct calls can be equal by
    value. The first owner wins, which is unambiguous since each projection subtree is a
    distinct output column's own."""
    owners: dict[int, ColumnRef] = {}
    for ref in column_graph.subjects():
        derivation = column_graph.derivation(ref)
        if derivation is None:
            continue
        for agg in derivation.find_all(exp.AggFunc):
            owners.setdefault(id(agg), ref)
    return owners


def _aggregation_message(output: ColumnRef, clear: CoherenceClear[DomainTag]) -> str:
    """Name what the coherence guard reasoned about: the aggregate and what it reduced,
    the per-row companion that is not held constant, and the grouping that fails to hold
    it. Built from the clear the guard recorded, so it describes the decision rather than
    re-deriving it, and reads the operand tag through the property's display hook."""
    agg = clear.aggregate
    func = agg.key  # the lowercased aggregate name: "sum", "avg", ...
    operand = _operand_label(agg)
    descriptor = domain_type_display(clear.cleared_value).name

    companions = sorted({u.companion.column for u in clear.undischarged})
    companion_list = ", ".join(repr(c) for c in companions)
    one = len(companions) == 1
    word, verb = ("companion", "is") if one else ("companions", "are")

    group_refs = clear.site.group_refs if clear.site is not None else None
    if group_refs:
        groups = ", ".join(repr(g.column) for g in sorted(group_refs, key=lambda r: r.column))
        tail = f"grouping on {groups}"
    elif group_refs == frozenset():
        tail = "the whole-relation reduction"
    else:
        tail = "the grouping, which does not resolve to columns,"
    return (
        f"reducing {output.column!r} with {func}({operand}): {descriptor}, whose per-row "
        f"{word} {companion_list} {verb} not held constant by {tail}; "
        "the aggregation is not well typed"
    )


def _operand_label(agg: exp.AggFunc) -> str:
    """A short label for what the aggregate reduced: a bare column by name, any other
    expression by its rendered SQL (``amount * rate``)."""
    this = agg.this
    return this.name if isinstance(this, exp.Column) else sg.render_sql(this)


def _join_key_findings(
    manifest: Manifest,
    parsed: Mapping[str, Expr],
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
    ground: Callable[[ColumnRef], Annotation[DomainTag]],
    line_maps: dict[str, LineMap],
) -> list[CheckFinding]:
    """One finding per ON-clause equality whose two columns carry conflicting domain
    types: equating a ``MoneyUSD`` key against a ``MoneyEUR`` one, or two incompatible
    nominal tags, joins values that cannot mean the same thing.

    The ON columns are read off the stamped statement tree, since a projection
    derivation alone does not carry the join. A column's tag is its propagated value
    where the lineage reached it, falling back to its declared grounding for a join key
    that is never projected, so a key that appears only in the ON clause is still typed.
    A no-claim side never conflicts (the lenient posture ``join_key_conflicts`` keeps)."""

    def tag_of(col: exp.Column) -> DomainTag | None:
        ref = resolved_column_ref(col)
        if ref is None:
            return None
        ann = annotations.get(ref)
        return ann.value if ann is not None else ground(ref).value

    out: list[CheckFinding] = []
    for uid, tree in parsed.items():
        source = SourceRef(SourceKind.MODEL, uid)
        for join in tree.find_all(exp.Join):
            on = join.args.get("on")
            if not isinstance(on, Expr):
                continue
            for left, right, left_tag, right_tag in join_key_conflicts(on, tag_of):
                line_start, line_end = _span_of(left, right, on)
                out.append(
                    CheckFinding(
                        kind=CheckFindingKind.JOIN_KEY_TYPE_MISMATCH,
                        message=_join_key_message(left, right, left_tag, right_tag),
                        model_unique_id=uid,
                        file_path=_file_of(manifest, source),
                        column=left.name or None,
                        line_start=line_start,
                        line_end=line_end,
                        source_span=_source_span(manifest, uid, line_start, line_end, line_maps),
                    )
                )
    out.sort(key=lambda f: (f.model_unique_id or "", f.line_start, f.column or ""))
    return out


def _join_key_message(
    left: exp.Column,
    right: exp.Column,
    left_tag: DomainTag,
    right_tag: DomainTag,
) -> str:
    """Name the two keys and the domain types being equated, each read through the
    property's display hook so the reader sees the conflict the meet found. A conflict
    carries a tag on each side by construction (a no-claim side never conflicts), so both
    render through the hook rather than an untyped fallback."""
    left_disp = domain_type_display(left_tag).name
    right_disp = domain_type_display(right_tag).name
    return (
        f"join key {_qualified(left)} = {_qualified(right)} equates {left_disp} with "
        f"{right_disp}; the two domain types conflict, so the equated values cannot mean "
        "the same thing"
    )


def _qualified(col: exp.Column) -> str:
    """A column rendered with its table qualifier when it has one (``p.amount``)."""
    return f"{col.table}.{col.name}" if col.table else col.name


# --- helpers --------------------------------------------------------------------


def _sorted(
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
) -> list[tuple[ColumnRef, Annotation[DomainTag]]]:
    """Annotations in a stable order (by model then column) so the report is
    deterministic."""
    return sorted(annotations.items(), key=lambda kv: (kv[0].source.unique_id, kv[0].column))


def _file_of(manifest: Manifest, source: SourceRef) -> str | None:
    node = manifest.nodes.get(source.unique_id)
    return node.original_file_path if node is not None else None


def _source_span(
    manifest: Manifest,
    uid: str,
    line_start: int,
    line_end: int,
    cache: dict[str, LineMap],
) -> SourceSpan:
    """Back-map a compiled span onto the model's source template (see
    :mod:`dblect.audit.sourcemap`), reusing one line map per model across the world's
    findings."""
    # The "no line" sentinel has no source position; skip building the map for a model
    # whose findings are all unlocated.
    if line_start == 0:
        return SourceSpan.compiled(line_start, line_end)
    line_map = cache.get(uid)
    if line_map is None:
        node = manifest.nodes.get(uid)
        compiled = node.analysis_sql if node is not None else None
        raw = node.raw_code if node is not None else None
        line_map = build_line_map(compiled, raw)
        cache[uid] = line_map
    return line_map.map_span(line_start, line_end)


def _span_of(*nodes: Expr | None) -> tuple[int, int]:
    """The 1-indexed source-line span of the first ``nodes`` entry sqlglot stamped with
    a usable line, falling back through the rest. ``(0, 0)`` when none carry one, the
    convention a finding with no locatable line uses (never line-suppressible).

    The span is in the compiled SQL's line space. The located finding kinds carry it
    on ``line_start``/``line_end`` and additionally back-map it onto ``raw_code`` via
    :func:`_source_span`, so the report can point at the source line the developer
    wrote when the construct passes through verbatim."""
    for node in nodes:
        if node is None:
            continue
        span = sg.line_range(node)
        if span is not None:
            return span
    return (0, 0)
