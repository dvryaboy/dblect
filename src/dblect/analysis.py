"""One analysis door: run every detector family over a manifest, return every
finding under one sealed type.

Two families surface findings today. :func:`~dblect.check.run.run_check` reports
the declaration-level ones (contract resolution, domain-type contradictions across
the DAG, not-well-typed aggregations), located by model, column, and contract.
:func:`~dblect.audit.run_audit` reports the SQL-structural ones (join fan-out,
window order, the nullability hazards, the rest), located by a span in one compiled
statement. The two stay distinct in representation and altitude; issue #107 weighs
whether to merge the representations as well.

What this module removes is the obligation to *know* there are two families. Asking
for "the findings of a manifest" should not require remembering to call both
producers and merge them: a consumer that threads findings (the incremental-worlds
cross-world diff, and the world axes still to come) calls :func:`analyze` once and
gets them all. Adding a third family is a change here, not in every consumer, and
:data:`AnalysisFinding` is sealed so a ``match`` over it with ``assert_never`` turns
a forgotten family into a type error rather than a silent coverage gap. That gap is
not hypothetical: the incremental check first threaded only the declaration-level
family, so the structural detectors (the very hazard it exists to catch) were absent
until the omission was noticed by hand.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from typing import assert_never

from dblect.adapters import AdapterProfile
from dblect.audit import AuditReport, LocatedFinding, run_audit
from dblect.check.findings import CheckFinding, CheckReport
from dblect.check.run import run_check
from dblect.manifest import Manifest
from dblect.types import ContractRegistry, resolve_contracts

# The sealed set of findings any analysis surfaces: one member per detector family.
# A ``match`` over this union closed by ``assert_never`` is exhaustiveness-checked,
# so adding a family without handling it everywhere is a type error, not a quiet
# blind spot.
AnalysisFinding = CheckFinding | LocatedFinding

# A finding's identity across two compilations of the same project: enough to say
# "the same issue in both worlds" while ignoring what drifts between compiled SQLs.
FindingIdentity = tuple[Hashable, ...]


def cross_world_identity(finding: AnalysisFinding) -> FindingIdentity:
    """The stable cross-world identity of ``finding``: where it lands and what it is,
    without the message or line span that differ between two compilations. A
    declaration-level finding keys on kind/model/column/contract; a structural one on
    kind/model and the rendered offending snippet. A snippet present in one world only
    (a steady-state-only join) has no match in the other, so it surfaces as varying.
    """
    match finding:
        case CheckFinding():
            return (
                "check",
                finding.kind,
                finding.model_unique_id,
                finding.column,
                finding.contract,
            )
        case LocatedFinding():
            inner = finding.finding
            return ("audit", inner.kind, finding.model_unique_id, inner.sql_snippet)
    assert_never(finding)


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """Every finding for one manifest, from every detector family, plus each family's
    own report so a caller that needs the family-specific extras (coverage blocks,
    suppressed directives) still has them. ``findings`` is the merged, sealed view the
    finding-threading consumers read; the rest is there when an altitude matters."""

    findings: tuple[AnalysisFinding, ...]
    check: CheckReport
    audit: AuditReport


def analyze(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    registry: ContractRegistry | None = None,
    resolution_floor: float | None = None,
) -> AnalysisReport:
    """Run every detector family over ``manifest`` and return every finding.

    ``profile`` is the resolved target whose dialect parses every model, and
    ``registry`` the contracts to resolve (defaulting to the active one), the same
    inputs :func:`~dblect.check.run.run_check` takes. ``resolution_floor`` is forwarded
    to it unchanged. The merged ``findings`` carry both families so a consumer never
    has to enumerate the families itself.

    The resolved ``determines`` facts are threaded into the structural audit so join-fanout
    grounds key coverage through functional dependencies (a declared ``wiki_id determines
    wiki_name`` lets a join on the determinant cover a key carrying the dependent). The
    declaration family already reads these; this hands the same facts to the structural one.
    """
    fd_facts = resolve_contracts(manifest, registry=registry).fd_facts
    check = run_check(manifest, profile, registry=registry, resolution_floor=resolution_floor)
    audit = run_audit(manifest, profile, fd_facts=fd_facts)
    return AnalysisReport(
        findings=(*check.findings, *audit.findings),
        check=check,
        audit=audit,
    )
