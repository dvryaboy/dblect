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
from typing import TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.check.coverage import GroundingCoverage, PropertyGrounding, ResolutionCoverage
from dblect.check.findings import CheckFinding, CheckFindingKind, CheckReport, UnbuiltModel
from dblect.lineage.builder import (
    BuildIssue,
    RelationBuildResult,
    build_manifest_graph,
    build_relation_graph,
)
from dblect.lineage.facts.model import Annotation, Fact
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import (
    AggregationSite,
    ColumnLineageGraph,
    ColumnRef,
    RelationLineageGraph,
    SourceKind,
    SourceRef,
    aggregation_site_meta,
)
from dblect.lineage.properties.domain_type import (
    NAKED,
    DomainTag,
    domain_type_grounding,
    domain_type_property,
)
from dblect.lineage.properties.functional_dependency import (
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import Manifest
from dblect.types import ContractRegistry, ResolvedContracts, active_registry, resolve_contracts


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
    resolution only, never on grounding."""
    reg = registry if registry is not None else active_registry()
    resolved = resolve_contracts(manifest, registry=reg)

    relation_build = build_relation_graph(manifest, dialect=profile.sqlglot_dialect)
    column_build = build_manifest_graph(manifest, dialect=profile.sqlglot_dialect)
    annotations = _propagate(resolved, relation_build.graph, column_build.graph)

    resolution = ResolutionCoverage.from_models(column_build.resolution)
    grounding = _grounding_coverage(resolved, relation_build, annotations)

    findings: list[CheckFinding] = []
    findings.extend(_issue_findings(resolved))
    findings.extend(_contradiction_findings(manifest, annotations))
    findings.extend(_aggregation_findings(manifest, annotations, column_build.graph))
    findings.extend(_resolution_floor_findings(resolution, resolution_floor))

    return CheckReport(
        findings=tuple(findings),
        load_issues=(),
        unbuilt=_unbuilt(relation_build.issues, column_build.issues),
        contracts_resolved=len(reg.contracts),
        models_propagated=len(manifest.models),
        predicates_collected=len(resolved.predicates),
        resolution=resolution,
        grounding=grounding,
    )


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


def _propagate(
    resolved: ResolvedContracts,
    relation_graph: RelationLineageGraph,
    column_graph: ColumnLineageGraph,
) -> Mapping[ColumnRef, Annotation[DomainTag]]:
    """Propagate the FD property over the relation graph, then domain type over the
    column graph reading it, exactly as the substrate end-to-end test wires them."""
    fd_prop = functional_dependency_property(
        functional_dependency_grounding(_by_scope(resolved.fd_facts))
    )
    store = AnnotationStore()
    for scope, ann in propagate(relation_graph, fd_prop).items():
        store.record(fd_prop.name, scope, ann)

    dt_prop = domain_type_property(
        domain_type_grounding(_by_scope(resolved.tag_facts)), fd=fd_prop.ref
    )
    ctx = PropertyRegistry((fd_prop, dt_prop)).dep_context(store)
    return propagate(column_graph, dt_prop, dep_context=ctx)


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
    column is genuinely checkable). A scope counts as grounded only when an
    unconditional fact lands on it (a purely conditional fact grounds nothing
    unconditionally, matching ``grounding``). The per-property denominator keeps
    only model-kind columns, since the synthetic CTE and UNION scaffolding is
    internal, not a column a fact would ever ground."""
    resolved_columns = set(annotations)
    model_columns = _model_scopes(resolved_columns)
    relation_subjects = _model_scopes(relation_build.graph.subjects())

    tag_scopes = _grounded_scopes(resolved.tag_facts)
    fd_scopes = _grounded_scopes(resolved.fd_facts)

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


def _grounded_scopes(facts: tuple[Fact[_V, _S], ...]) -> set[_S]:
    """Scopes carrying at least one unconditional fact, the ones that ground."""
    return {f.scope for f in facts if f.condition is None}


def _resolution_floor_findings(
    resolution: ResolutionCoverage, floor: float | None
) -> list[CheckFinding]:
    if floor is None or not resolution.below(floor):
        return []
    frac = resolution.fraction or 0.0
    # Only models that actually fell blind are worth naming; a fully-resolved
    # model is not "lowest" however the ranking sorted it.
    worst = ", ".join(
        f"{m.unique_id} ({m.resolved_refs}/{m.sites})"
        for m in resolution.worst_models
        if m.resolved_refs < m.sites
    )
    tail = f"; lowest: {worst}" if worst else ""
    return [
        CheckFinding(
            kind=CheckFindingKind.RESOLUTION_BELOW_FLOOR,
            message=(
                f"resolved {frac:.0%} of lineage sites "
                f"({resolution.resolved_refs}/{resolution.sites}), below the "
                f"{floor:.0%} floor; analysis covers only what was resolved{tail}"
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
        )
        for issue in resolved.issues
    ]


def _contradiction_findings(
    manifest: Manifest, annotations: Mapping[ColumnRef, Annotation[DomainTag]]
) -> list[CheckFinding]:
    """One finding per column whose flow value is provisional: a declared type the
    inferred one contradicts, reported wherever the taint reached."""
    out: list[CheckFinding] = []
    for ref, ann in _sorted(annotations):
        if not ann.provisional or ann.value == NAKED:
            continue
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
        if derivation is None or not _reduces_a_bare_column(derivation):
            continue
        operands = column_graph.edges.get(ref, frozenset())
        if any(annotations.get(up, _NO_ANN).value != NAKED for up in operands):
            out.append(
                CheckFinding(
                    kind=CheckFindingKind.AGGREGATION_NOT_WELL_TYPED,
                    message=(
                        f"reducing {ref.column!r} mixes a per-row companion that nothing holds "
                        "constant per group; the sum is not well typed"
                    ),
                    model_unique_id=ref.source.unique_id,
                    file_path=_file_of(manifest, ref.source),
                    column=ref.column,
                )
            )
    return out


def _reduces_a_bare_column(derivation: Expr) -> bool:
    """Whether a projection is a non-windowed aggregate (one the coherence guard
    judged) directly over a bare column.

    Restricting to a bare-column operand keeps the finding precise. An aggregate
    over an expression, ``sum(CASE WHEN ... THEN amount ELSE 0 END)`` being the
    common one, already clears to naked at the expression because it mixes a
    magnitude with a dimensionless literal, which is a different concern than a
    companion that is not constant per group. The operand column carrying a tag
    that the reduction cleared is the signal that names a mixed-currency sum.
    """
    return any(
        isinstance(aggregation_site_meta(agg), AggregationSite) and isinstance(agg.this, exp.Column)
        for agg in derivation.find_all(exp.AggFunc)
    )


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
