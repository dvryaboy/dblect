"""Cross-model inner-flatten: an UNNEST of an array a model rebuilt non-empty upstream.

The structural detector cannot tell a raw source array (whose emptiness is an ingestion
fact) from one a staging model rebuilt with ARRAY_AGG under a GROUP BY. The fact-grounded
factory propagates ``array_nonemptiness`` across the manifest and clears the rebuilt case.

These pin the worked example from the broadening of the issue: the unnest of a *raw source*
array fires, the unnest of the *rebuilt* array one model downstream stays silent.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.audit.walker import run_audit
from dblect.flatten.detector import make_array_nonemptiness_detectors
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import Finding, FindingKind, parse_sql

_BQ = profile_for_adapter("bigquery")

_RAW = "source.app.raw.raw_events"
_STG = "model.app.stg_event_tags"
_MART = "model.app.mart_event_tags"

# Staging unnests a raw source array (A), regroups, and rebuilds the array with ARRAY_AGG.
_STG_SQL = (
    "WITH exploded AS ("
    "  SELECT e.event_id, e.region, t.tag, t.weight"
    "  FROM raw_events e CROSS JOIN UNNEST(e.tags) AS t"
    "  GROUP BY 1, 2, 3, 4"
    "), regrouped AS ("
    "  SELECT event_id, region,"
    "    ARRAY_AGG(STRUCT(tag, SUM(weight) AS weight)) AS tags"
    "  FROM exploded GROUP BY event_id, region"
    ") SELECT * FROM regrouped"
)
# The mart unnests the rebuilt array (B).
_MART_SQL = (
    "SELECT s.event_id, x.tag, x.weight FROM stg_event_tags s CROSS JOIN UNNEST(s.tags) AS x"
)


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="app",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
    )


def _source(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="app",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _manifest() -> Manifest:
    nodes = [_source(_RAW), _model(_STG, _STG_SQL), _model(_MART, _MART_SQL)]
    return Manifest(
        schema_version="v12", adapter_type="bigquery", nodes={n.unique_id: n for n in nodes}
    )


def _findings(manifest: Manifest, consumer_uid: str) -> tuple[Finding, ...]:
    # Mirror the audit: the factory stamps the shared trees with resolved refs, and the
    # detector scans the same tree object, so the two see one resolution (as run_audit
    # shares one `trees` dict between the factory and the scan loop).
    trees = {
        uid: parse_sql(n.compiled_code, dialect="bigquery")
        for uid, n in manifest.nodes.items()
        if n.compiled_code is not None
    }
    detectors = make_array_nonemptiness_detectors(manifest, _BQ, parsed=trees)
    return tuple(f for detect in detectors for f in detect(trees[consumer_uid]))


def test_unnest_of_raw_source_array_fires() -> None:
    findings = _findings(_manifest(), _STG)
    assert [f.kind for f in findings] == [FindingKind.INNER_FLATTEN_ROW_DROP]


def test_unnest_of_rebuilt_array_is_silent_across_the_model_boundary() -> None:
    assert _findings(_manifest(), _MART) == ()


def test_unnest_of_array_rebuilt_in_a_cte_is_silent() -> None:
    # The CTE-aware payoff: a CTE rebuilds the array with ARRAY_AGG under GROUP BY, and a
    # later scope in the same model unnests that CTE column. Resolution follows the unnest
    # argument through the CTE to the rebuilt (NON_EMPTY) column, so it stays quiet.
    sql = (
        "WITH built AS ("
        "  SELECT event_id, ARRAY_AGG(STRUCT(tag, weight)) AS tags"
        "  FROM raw_events GROUP BY event_id"
        ") SELECT b.event_id, x.tag FROM built b CROSS JOIN UNNEST(b.tags) AS x"
    )
    nodes = [_source(_RAW), _model("model.app.in_model_rebuild", sql)]
    manifest = Manifest(
        schema_version="v12", adapter_type="bigquery", nodes={n.unique_id: n for n in nodes}
    )
    assert _findings(manifest, "model.app.in_model_rebuild") == ()


def test_run_audit_reports_the_flatten_finding_exactly_once() -> None:
    # End to end: the structural form is no longer in DEFAULT_DETECTORS, so the finding
    # comes solely through the fact-grounded factory. The raw-source unnest in staging
    # fires once; the mart's rebuilt-array unnest stays silent.
    report = run_audit(_manifest(), _BQ)
    flatten = [lf for lf in report.findings if lf.kind is FindingKind.INNER_FLATTEN_ROW_DROP]
    assert [lf.model_unique_id for lf in flatten] == [_STG]
