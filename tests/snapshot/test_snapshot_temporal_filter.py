"""Flag downstream queries on a snapshot that omit a temporal filter (#8).

A dbt snapshot carries SCD-2 validity columns (``dbt_valid_from`` / ``dbt_valid_to``
by default, renamable via ``snapshot_meta_column_names``). A query that references a
snapshot without restricting to the current row (``dbt_valid_to IS NULL``) or a
point-in-time slice (``BETWEEN dbt_valid_from AND dbt_valid_to``) silently fans out
one row per historical version.

Two layers of tests:

* Pure detector, over hand-written SQL, pinning the scope logic: a filter in any
  enclosing scope (the immediate SELECT, a JOIN ON, or an outer query reading the
  snapshot through a CTE or subquery) suppresses, and the validity columns are the
  ones the caller passes (so a renamed snapshot is judged by its own names).
* End to end, over a real dbt-compiled manifest (``tests/fixtures/snapshot_audit``,
  built by ``scripts/refresh_snapshot_audit.sh``). This is what keeps the detector
  honest about how dbt actually compiles snapshot references and where it records
  the validity column names, rather than resting on hand-built assumptions.
"""

from __future__ import annotations

from pathlib import Path

from dblect.adapters import profile_for_adapter
from dblect.audit import run_audit
from dblect.manifest import Manifest
from dblect.snapshot import detect_snapshot_temporal_filter
from dblect.sql import FindingKind, parse_sql

# A default-named snapshot and a snapshot that renamed its validity columns.
_DEFAULT = {"orders_snapshot": ("dbt_valid_from", "dbt_valid_to")}
_RENAMED = {"orders_snapshot_renamed": ("valid_from", "valid_to")}
_DUCKDB = profile_for_adapter("duckdb")


def _kinds(sql: str, snapshots: dict[str, tuple[str, ...]] = _DEFAULT) -> list[FindingKind]:
    findings = detect_snapshot_temporal_filter(parse_sql(sql), snapshots=snapshots)
    return [f.kind for f in findings]


# --- pure detector: scope logic -------------------------------------------------


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


def test_filter_in_the_outer_query_over_a_cte_is_safe() -> None:
    # The snapshot is read in a CTE with no filter of its own; the temporal filter
    # lives in the outer query reading that CTE. A correct, common shape: the
    # detector must look up the enclosing scope chain, not only the immediate SELECT.
    sql = (
        "WITH history AS (SELECT * FROM orders_snapshot) "
        "SELECT h.id FROM history h WHERE h.dbt_valid_to IS NULL"
    )
    assert _kinds(sql) == []


def test_filter_in_the_outer_query_over_a_subquery_is_safe() -> None:
    sql = "SELECT * FROM (SELECT * FROM orders_snapshot) x WHERE x.dbt_valid_to IS NULL"
    assert _kinds(sql) == []


def test_non_snapshot_reference_is_ignored() -> None:
    assert _kinds("SELECT * FROM orders_table JOIN dim d ON orders_table.id = d.id") == []


def test_each_snapshot_reference_flags_once_per_scope() -> None:
    # A snapshot referenced once, with several columns selected, flags exactly once.
    kinds = _kinds(
        "SELECT s.id, s.amount, s.region FROM orders_snapshot s JOIN dim d ON s.id = d.id"
    )
    assert kinds == [FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING]


# --- pure detector: renamed validity columns ------------------------------------


def test_renamed_snapshot_is_safe_when_filtered_on_its_own_column() -> None:
    # The snapshot renamed dbt_valid_to to valid_to; a filter on valid_to is the
    # temporal restriction and must suppress.
    assert _kinds("SELECT * FROM orders_snapshot_renamed WHERE valid_to IS NULL", _RENAMED) == []


def test_renamed_snapshot_is_flagged_when_filtered_on_the_default_name() -> None:
    # dbt_valid_to is not a column of this snapshot (it renamed to valid_to), so a
    # filter on the default name does not restrict it: still a full-history read.
    assert _kinds("SELECT * FROM orders_snapshot_renamed WHERE dbt_valid_to IS NULL", _RENAMED) == [
        FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING
    ]


def test_renamed_snapshot_remedy_names_its_own_columns() -> None:
    findings = detect_snapshot_temporal_filter(
        parse_sql("SELECT * FROM orders_snapshot_renamed"), snapshots=_RENAMED
    )
    [finding] = findings
    assert "valid_to IS NULL" in finding.message
    assert "dbt_valid_to" not in finding.message


# --- end to end, over a real dbt-compiled manifest ------------------------------


def test_run_audit_flags_exactly_the_unsafe_snapshot_consumers(
    snapshot_audit_manifest_path: Path,
) -> None:
    # The fixture's consumer models, compiled by dbt: unsafe_join reads a default
    # snapshot under a JOIN with no filter; renamed_unsafe reads a renamed snapshot
    # with no filter. safe_current filters on dbt_valid_to, safe_outer_filter filters
    # in the outer query over a CTE, and renamed_safe filters on the renamed valid_to.
    # Only the two unsafe reads should flag, exercising the relation-name match, the
    # outer-scope fix, and the renamed-column fix against real compiled SQL.
    manifest = Manifest.from_file(snapshot_audit_manifest_path)
    report = run_audit(manifest, _DUCKDB)
    flagged = {
        lf.model_unique_id
        for lf in report.findings
        if lf.finding.kind is FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING
    }
    assert flagged == {"model.jaffle_shop.unsafe_join", "model.jaffle_shop.renamed_unsafe"}


def test_run_audit_without_any_snapshot_never_flags(jaffle_manifest_path: Path) -> None:
    # The jaffle manifest has no snapshots, so a dbt_valid_to-free query is not about
    # a snapshot and the detector must stay silent.
    manifest = Manifest.from_file(jaffle_manifest_path)
    report = run_audit(manifest, _DUCKDB)
    kinds = {lf.finding.kind for lf in report.findings}
    assert FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING not in kinds
