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

from collections.abc import Mapping
from typing import TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.check.findings import CheckFinding, CheckFindingKind, CheckReport
from dblect.lineage.builder import build_manifest_graph, build_relation_graph
from dblect.lineage.facts.model import Annotation, Fact
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import (
    AggregationSite,
    ColumnLineageGraph,
    ColumnRef,
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
    *,
    registry: ContractRegistry | None = None,
    dialect: str | None = "duckdb",
) -> CheckReport:
    """Resolve the registered contracts against ``manifest``, propagate, and return
    the declaration-level findings. ``registry`` defaults to the active one (the
    loader populates a fresh registry the CLI passes in)."""
    reg = registry if registry is not None else active_registry()
    resolved = resolve_contracts(manifest, registry=reg)
    column_graph = build_manifest_graph(manifest, dialect=dialect).graph
    annotations = _propagate(manifest, resolved, column_graph, dialect=dialect)

    findings: list[CheckFinding] = []
    findings.extend(_issue_findings(resolved))
    findings.extend(_contradiction_findings(manifest, annotations))
    findings.extend(_aggregation_findings(manifest, column_graph, annotations))

    return CheckReport(
        findings=tuple(findings),
        load_issues=(),
        contracts_resolved=len(reg.contracts),
        models_propagated=len(manifest.models),
        predicates_collected=len(resolved.predicates),
    )


# --- propagation ----------------------------------------------------------------


def _propagate(
    manifest: Manifest,
    resolved: ResolvedContracts,
    column_graph: ColumnLineageGraph,
    *,
    dialect: str | None,
) -> Mapping[ColumnRef, Annotation[DomainTag]]:
    """Propagate the FD property over the relation graph, then domain type over the
    column graph reading it, exactly as the substrate end-to-end test wires them."""
    fd_prop = functional_dependency_property(
        functional_dependency_grounding(_by_scope(resolved.fd_facts))
    )
    store = AnnotationStore()
    relation_graph = build_relation_graph(manifest, dialect=dialect).graph
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
    column_graph: ColumnLineageGraph,
    annotations: Mapping[ColumnRef, Annotation[DomainTag]],
) -> list[CheckFinding]:
    """One finding per aggregate output whose tag cleared to naked while an operand
    it summed still carried one: a reduction the algebra cannot call well typed."""
    out: list[CheckFinding] = []
    for ref, ann in _sorted(annotations):
        if ann.value != NAKED:
            continue
        derivation = column_graph.derivation(ref)
        if derivation is None or not _is_guarded_aggregate(derivation):
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


def _is_guarded_aggregate(derivation: Expr) -> bool:
    """Whether a projection is a non-windowed aggregate the coherence guard judged
    (the builder stamps an :class:`AggregationSite` on exactly those)."""
    return any(
        isinstance(aggregation_site_meta(agg), AggregationSite)
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
