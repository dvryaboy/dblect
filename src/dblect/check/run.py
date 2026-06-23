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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.audit.suppress import SuppressionDirective, apply, parse_directives
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
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import (
    AggregationSite,
    ColumnLineageGraph,
    ColumnRef,
    SourceKind,
    SourceRef,
    aggregation_site_meta,
)
from dblect.lineage.properties.domain_type import (
    NAKED,
    DomainTag,
    companion_columns,
    domain_type_grounded_scopes,
    domain_type_grounding,
    domain_type_property,
)
from dblect.lineage.properties.functional_dependency import (
    FDSet,
    functional_dependency_grounded_scopes,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.property import COLUMNREF_META_KEY, propagate
from dblect.manifest import Manifest
from dblect.sql import AggregateBehavior, aggregate_behavior
from dblect.sql import _sqlglot as sg
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
    annotations without changing ``propagate_world``'s signature."""

    world: WorldRef
    domain_type: Mapping[ColumnRef, Annotation[DomainTag]]


def build_check_graphs(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    registry: ContractRegistry | None = None,
) -> CheckGraphs:
    """Resolve contracts and build both lineage graphs once. ``profile`` supplies the
    dialect every model parses under; the result is world-invariant and reusable
    across an enumeration."""
    reg = registry if registry is not None else active_registry()
    resolved = resolve_contracts(manifest, registry=reg)
    relation_build = build_relation_graph(manifest, dialect=profile.sqlglot_dialect)
    column_build = build_manifest_graph(manifest, dialect=profile.sqlglot_dialect)
    return CheckGraphs(
        manifest=manifest,
        resolved=resolved,
        relation_build=relation_build,
        column_build=column_build,
        contracts_resolved=len(reg.contracts),
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
    domain_type = propagate(graphs.column_build.graph, dt_prop, dep_context=ctx)
    return WorldAnnotations(world=facts.world, domain_type=domain_type)


def run_check(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    registry: ContractRegistry | None = None,
    resolution_floor: float | None = None,
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
    is never dropped by being forgotten."""
    graphs = build_check_graphs(manifest, profile, registry=registry)
    world = propagate_world(graphs, base_world_facts(graphs.resolved))

    resolution = ResolutionCoverage.from_models(graphs.column_build.resolution)
    grounding = _grounding_coverage(graphs.resolved, graphs.relation_build, world.domain_type)

    findings: list[CheckFinding] = []
    findings.extend(_issue_findings(graphs.resolved))
    findings.extend(world_findings(graphs, world))
    findings.extend(_resolution_floor_findings(resolution, resolution_floor))

    active, suppressed = _suppress(findings, manifest)

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


def _suppress(
    findings: Iterable[CheckFinding], manifest: Manifest
) -> tuple[tuple[CheckFinding, ...], tuple[SuppressedCheckFinding, ...]]:
    """Apply ``-- noqa`` directives to declaration-level findings.

    Directives are read per model from the source the developer wrote (``raw_code``,
    falling back to the compiled SQL), the same place the structural audit reads them,
    and matched against the finding's line provenance. A finding with no model or no
    located line (a contract-resolution issue, a project-wide coverage finding) carries
    no line, so the matcher leaves it active; only the line-located domain-type and
    aggregation findings are silenceable this way."""
    directives_by_model: dict[str, tuple[SuppressionDirective, ...]] = {}
    active: list[CheckFinding] = []
    suppressed: list[SuppressedCheckFinding] = []
    for finding in findings:
        uid = finding.model_unique_id
        node = manifest.models.get(uid) if uid is not None else None
        if uid is None or node is None:
            active.append(finding)
            continue
        if uid not in directives_by_model:
            directives_by_model[uid] = parse_directives(node.raw_code or node.analysis_sql or "")
        kept, hidden = apply((finding,), directives_by_model[uid])
        active.extend(kept)
        suppressed.extend(
            SuppressedCheckFinding(finding=f, directive_line=d.line, bare=d.kinds is None)
            for f, d in hidden
        )
    return tuple(active), tuple(suppressed)


def world_findings(graphs: CheckGraphs, world: WorldAnnotations) -> list[CheckFinding]:
    """The findings that vary by world: the domain-type contradictions and the
    not-well-typed aggregations, read off one world's annotations. The
    contract-resolution and resolution-floor findings are world-invariant and stay
    ``run_check``'s to report once."""
    findings: list[CheckFinding] = []
    findings.extend(
        _contradiction_findings(graphs.manifest, world.domain_type, graphs.column_build.graph)
    )
    findings.extend(
        _aggregation_findings(
            graphs.manifest,
            world.domain_type,
            graphs.column_build.graph,
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
) -> list[CheckFinding]:
    """One finding per column whose flow value is provisional: a declared type the
    inferred one contradicts, reported wherever the taint reached."""
    out: list[CheckFinding] = []
    for ref, ann in _sorted(annotations):
        if not ann.provisional or ann.value == NAKED:
            continue
        line_start, line_end = _span_of(column_graph.derivation(ref))
        out.append(
            CheckFinding(
                kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
                message=(
                    f"declared domain type for {ref.column!r} is contradicted by the type "
                    "that flows in from upstream"
                ),
                model_unique_id=ref.source.unique_id,
                file_path=_file_of(manifest, ref.source),
                column=ref.column,
                line_start=line_start,
                line_end=line_end,
            )
        )
    return out


def _aggregation_findings(
    manifest: Manifest,
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
    column_graph: ColumnLineageGraph,
) -> list[CheckFinding]:
    """One finding per aggregate output whose tag cleared to naked while an operand
    it summed still carried one: a reduction the algebra cannot call well typed."""
    out: list[CheckFinding] = []
    for ref, ann in _sorted(annotations):
        if ann.value != NAKED:
            continue
        derivation = column_graph.derivation(ref)
        if derivation is None:
            continue
        operands = column_graph.edges.get(ref, frozenset())
        tagged = frozenset(up for up in operands if annotations.get(up, _NO_ANN).value != NAKED)
        agg = _culprit_aggregate(derivation, tagged) if tagged else None
        if agg is None:
            continue
        # The aggregate node pins the line; the projection derivation is the fallback
        # so the finding still lands near the right place when the aggregate carries no
        # stamped identifier (a literal-only shape).
        line_start, line_end = _span_of(agg, derivation)
        out.append(
            CheckFinding(
                kind=CheckFindingKind.AGGREGATION_NOT_WELL_TYPED,
                message=_aggregation_message(ref, agg, annotations),
                model_unique_id=ref.source.unique_id,
                file_path=_file_of(manifest, ref.source),
                column=ref.column,
                line_start=line_start,
                line_end=line_end,
            )
        )
    return out


_GENERIC_AGG_MESSAGE = (
    "reducing {col!r} mixes a per-row companion that nothing holds constant per group; "
    "the aggregation is not well typed"
)


def _aggregation_message(
    output: ColumnRef,
    agg: exp.AggFunc,
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
) -> str:
    """Name what the coherence guard reasoned about: the aggregate and the column it
    reduced, the per-row companion that is not held constant, and the grouping that
    fails to hold it.

    Faithful by construction: it reads the same ``AggregationSite`` the builder
    stamped (and the guard judged) and the same ``companion_columns`` the guard
    called, so the message describes the decision rather than re-deriving it. When the
    reduced operand carries no tag or the companion cannot be pinpointed (an
    unmodelled shape), it falls back to the generic wording rather than guess."""
    operand = agg.this.meta.get(COLUMNREF_META_KEY)
    if not isinstance(operand, ColumnRef) or operand not in annotations:
        return _GENERIC_AGG_MESSAGE.format(col=output.column)
    site = aggregation_site_meta(agg)
    pinned: frozenset[ColumnRef] = site.pinned if site is not None else frozenset()
    group_refs = site.group_refs if site is not None else None

    companions = companion_columns(annotations[operand].value)
    # A companion in the group key or pinned by the scope's WHERE is held constant;
    # the rest are what the grouping leaves varying. If subtracting empties the set
    # (an exotic multi-companion shape), name the companions rather than nothing.
    held: frozenset[ColumnRef] = pinned | (group_refs or frozenset())
    varying = frozenset(c for c in companions if c not in held) or companions
    if not varying:
        return _GENERIC_AGG_MESSAGE.format(col=output.column)

    func = agg.key  # the lowercased aggregate name: "sum", "avg", ...
    companion_list = ", ".join(repr(c.column) for c in sorted(varying, key=lambda r: r.column))
    one = len(varying) == 1
    word, verb = ("companion", "is") if one else ("companions", "are")
    if group_refs:
        groups = ", ".join(repr(g.column) for g in sorted(group_refs, key=lambda r: r.column))
        tail = f"grouping on {groups}"
    else:
        tail = "the grouping, which does not resolve to columns,"
    return (
        f"reducing {output.column!r} with {func}({operand.column}): its per-row {word} "
        f"{companion_list} {verb} not held constant by {tail}; the aggregation is not well typed"
    )


def _culprit_aggregate(
    derivation: Expr,
    tagged: frozenset[ColumnRef],
) -> exp.AggFunc | None:
    """The bare-column **combining** aggregate the finding is about: a non-windowed
    ``COMBINE`` aggregate over a single column in ``tagged``, carrying the stamped site
    the coherence guard judged.

    Only combining aggregates carry the obligation (the classification lives in
    :mod:`dblect.sql.aggregates`): a ``SELECT`` like ``min``/``max`` returns a real value
    and merely widens its tag, and a ``COUNT`` is a tag-free cardinality, so neither is a
    not-well-typed reduction. Restricting to a bare-column operand keeps it precise: an
    aggregate over an expression (``sum(CASE WHEN ... THEN amount ELSE 0 END)``) already
    clears to naked at the expression by mixing a magnitude with a dimensionless literal,
    a different concern than a companion that is not constant per group."""
    for agg in derivation.find_all(exp.AggFunc):
        if aggregate_behavior(agg) is not AggregateBehavior.COMBINE:
            continue
        if not isinstance(agg.this, exp.Column):
            continue
        if not isinstance(aggregation_site_meta(agg), AggregationSite):
            continue
        operand = agg.this.meta.get(COLUMNREF_META_KEY)
        if isinstance(operand, ColumnRef) and operand in tagged:
            return agg
    return None


# --- helpers --------------------------------------------------------------------

_NO_ANN: Annotation[DomainTag] = Annotation(NAKED)


def _sorted(
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
) -> list[tuple[ColumnRef, Annotation[DomainTag]]]:
    """Annotations in a stable order (by model then column) so the report is
    deterministic."""
    return sorted(annotations.items(), key=lambda kv: (kv[0].source.unique_id, kv[0].column))


def _file_of(manifest: Manifest, source: SourceRef) -> str | None:
    node = manifest.nodes.get(source.unique_id)
    return node.original_file_path if node is not None else None


def _span_of(*nodes: Expr | None) -> tuple[int, int]:
    """The 1-indexed source-line span of the first ``nodes`` entry sqlglot stamped with
    a usable line, falling back through the rest. ``(0, 0)`` when none carry one, the
    convention a finding with no locatable line uses (never line-suppressible). The
    line space is the parsed SQL's, matching where the ``-- noqa`` scanner
    reads directives."""
    for node in nodes:
        if node is None:
            continue
        span = sg.line_range(node)
        if span is not None:
            return span
    return (0, 0)
