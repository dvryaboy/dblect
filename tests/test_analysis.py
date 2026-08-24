"""The single analysis door: :func:`dblect.analysis.analyze` surfaces every detector
family's findings, so a consumer that threads findings cannot silently drop one.

The regression this guards is concrete: the incremental check first carried only the
declaration-level family, leaving the SQL-structural detectors (the hazard it exists
to catch) out of its cross-world diff. Pinning that ``analyze`` is exactly the union
of both families keeps a future change from re-opening that gap at the door rather
than discovering it by hand in each consumer.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.analysis import analyze
from dblect.audit import LocatedFinding, run_audit
from dblect.check.findings import CheckFinding
from dblect.check.run import run_check
from dblect.manifest import Manifest, Node
from dblect.sql import FindingKind
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_DUCKDB = profile_for_adapter("duckdb")


def _sql_manifest(compiled_sql: str) -> Manifest:
    node = _node("model.pkg.m", compiled_sql, raw=compiled_sql)
    return _manifest(node)


def _model_node(uid: str, sql: str, *, depends_on: frozenset[str] = frozenset()) -> Node:
    return _node(uid, sql, raw=sql, depends_on=depends_on)


def _multi_model_manifest() -> Manifest:
    # `mart` self-joins upstream `up` (resolving up's columns through the accumulated schema),
    # and its LEFT JOIN feeding a GROUP BY trips a structural detector. Both families reach
    # across more than one model, so the door exercises the shared build the single-model case
    # would miss.
    up = _model_node("model.pkg.up", "select id, country from raw_users")
    mart = _model_node(
        "model.pkg.mart",
        "select u.id, d.country, count(*) as n\n"
        "from up u left join up d on u.id = d.id\n"
        "group by u.id, d.country",
        depends_on=frozenset({"model.pkg.up"}),
    )
    return _manifest(up, mart)


def test_analyze_is_the_union_of_both_detector_families() -> None:
    # The door returns precisely what running each family by hand returns, both directions:
    # nothing dropped, nothing invented. This bites if the shared build ever diverges from the
    # per-family build.
    manifest = _multi_model_manifest()
    report = analyze(manifest, _DUCKDB)
    assert report.findings == (
        *run_check(manifest, _DUCKDB).findings,
        *run_audit(manifest, _DUCKDB).findings,
    )


def test_analyze_carries_the_structural_family_a_check_only_consumer_would_miss() -> None:
    # The shape the incremental check first dropped: a structural finding, located by span,
    # reaching a consumer that reads ``analyze(...).findings``.
    manifest = _multi_model_manifest()
    structural = [f for f in analyze(manifest, _DUCKDB).findings if isinstance(f, LocatedFinding)]
    assert any(f.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN for f in structural)
    assert tuple(structural) == run_audit(manifest, _DUCKDB).findings


def test_analyze_exposes_each_familys_own_report() -> None:
    # Consumers that need the family-specific extras (coverage, suppressed directives)
    # still reach them; the merged ``findings`` is a convenience, not a lossy view.
    manifest = _sql_manifest("select 1 as x")
    report = analyze(manifest, _DUCKDB)

    assert report.check.findings == tuple(f for f in report.findings if isinstance(f, CheckFinding))
    assert report.audit.findings == tuple(
        f for f in report.findings if isinstance(f, LocatedFinding)
    )
