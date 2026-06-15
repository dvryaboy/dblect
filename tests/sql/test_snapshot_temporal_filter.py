"""Flag downstream queries on a snapshot that omit a temporal filter (#8).

A dbt snapshot carries SCD-2 validity columns (`dbt_valid_from`, `dbt_valid_to`).
A query that references a snapshot without restricting to the current row
(`dbt_valid_to IS NULL`) or a point-in-time slice (`BETWEEN dbt_valid_from AND
dbt_valid_to`) silently fans out one row per historical version. The detector is
manifest-driven: it flags a reference only to a relation the manifest says is a
snapshot, and stays silent when the enclosing query filters on the validity
columns.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.audit import run_audit
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import parse_sql
from dblect.sql.patterns import FindingKind, detect_snapshot_temporal_filter

_SNAP = frozenset({"orders_snapshot"})
_DUCKDB = profile_for_adapter("duckdb")


def _kinds(sql: str, names: frozenset[str] = _SNAP) -> list[FindingKind]:
    findings = detect_snapshot_temporal_filter(parse_sql(sql), snapshot_names=names)
    return [f.kind for f in findings]


# --- pure detector --------------------------------------------------------------


def test_snapshot_in_join_without_a_filter_is_flagged() -> None:
    kinds = _kinds("SELECT s.id, d.region FROM orders_snapshot s JOIN dim d ON s.id = d.id")
    assert kinds == [FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING]


def test_plain_select_of_a_snapshot_without_a_filter_is_flagged() -> None:
    assert _kinds("SELECT * FROM orders_snapshot") == [FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING]


def test_current_row_filter_in_where_is_safe() -> None:
    assert _kinds("SELECT * FROM orders_snapshot WHERE dbt_valid_to IS NULL") == []


def test_point_in_time_between_is_safe() -> None:
    assert (
        _kinds(
            "SELECT * FROM orders_snapshot WHERE order_ts BETWEEN dbt_valid_from AND dbt_valid_to"
        )
        == []
    )


def test_filter_in_the_join_on_is_safe() -> None:
    assert (
        _kinds(
            "SELECT s.id FROM dim d "
            "JOIN orders_snapshot s ON s.id = d.id AND s.dbt_valid_to IS NULL"
        )
        == []
    )


def test_filter_inside_a_cte_is_safe() -> None:
    # The snapshot is referenced in the CTE, which carries the filter; the outer
    # join is over the already-current rows.
    sql = (
        "WITH current_orders AS ("
        "  SELECT * FROM orders_snapshot WHERE dbt_valid_to IS NULL"
        ") SELECT c.id, d.region FROM current_orders c JOIN dim d ON c.id = d.id"
    )
    assert _kinds(sql) == []


def test_non_snapshot_reference_is_ignored() -> None:
    assert _kinds("SELECT * FROM orders_table JOIN dim d ON orders_table.id = d.id") == []


def test_each_snapshot_reference_flags_once_per_scope() -> None:
    # A snapshot referenced once, with several columns selected, flags exactly once.
    kinds = _kinds(
        "SELECT s.id, s.amount, s.region FROM orders_snapshot s JOIN dim d ON s.id = d.id"
    )
    assert kinds == [FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING]


# --- end to end, manifest-driven ------------------------------------------------


def _model(uid: str, sql: str) -> Node:
    return _node(uid, kind=ResourceType.MODEL, sql=sql)


def _node(uid: str, *, kind: ResourceType, sql: str | None) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=f"models/{uid.split('.')[-1]}.sql",
        columns={},
    )


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _audit_kinds(report: object) -> list[FindingKind]:
    assert hasattr(report, "findings")
    return [lf.finding.kind for lf in report.findings]  # type: ignore[attr-defined]


def test_run_audit_flags_an_unsafe_snapshot_consumer() -> None:
    snap = _node("snapshot.shop.orders_snapshot", kind=ResourceType.SNAPSHOT, sql=None)
    consumer = _model(
        "model.shop.enriched",
        "SELECT s.id, d.region FROM orders_snapshot s JOIN dim d ON s.id = d.id",
    )
    report = run_audit(_manifest(snap, consumer), _DUCKDB)
    assert FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING in _audit_kinds(report)


def test_run_audit_is_silent_on_a_safe_snapshot_consumer() -> None:
    snap = _node("snapshot.shop.orders_snapshot", kind=ResourceType.SNAPSHOT, sql=None)
    consumer = _model(
        "model.shop.current",
        "SELECT s.id FROM orders_snapshot s WHERE s.dbt_valid_to IS NULL",
    )
    report = run_audit(_manifest(snap, consumer), _DUCKDB)
    assert FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING not in _audit_kinds(report)


def test_run_audit_without_any_snapshot_never_flags() -> None:
    # No snapshot in the manifest: a `dbt_valid_to`-free query is not about a
    # snapshot, so the detector must stay silent.
    consumer = _model("model.shop.m", "SELECT * FROM orders_table")
    report = run_audit(_manifest(consumer), _DUCKDB)
    assert FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING not in _audit_kinds(report)
