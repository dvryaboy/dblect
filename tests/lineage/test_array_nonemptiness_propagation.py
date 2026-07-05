"""Cross-model array-non-emptiness propagation, end to end through the substrate.

These pin the property at its contract boundary: build a manifest of sources and
models, propagate over the column graph, and read each model output column's
value. The rules under test are the sound ones the walk can justify, including the
worked example from the broadening of the inner-flatten issue: a raw source array
stays UNKNOWN, an array rebuilt by ARRAY_AGG under a GROUP BY is NON_EMPTY, and that
guarantee carries across a model boundary.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.array_nonemptiness import ArrayNonEmpty, array_nonemptiness
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Node, ResourceType

_BQ = profile_for_adapter("bigquery")


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


def _values(*nodes: Node) -> Mapping[ColumnRef, ArrayNonEmpty]:
    manifest = Manifest(
        schema_version="v12",
        adapter_type="bigquery",
        nodes={n.unique_id: n for n in nodes},
    )
    graph = build_manifest_graph(manifest, dialect=_BQ.sqlglot_dialect).graph
    anns = propagate(graph, array_nonemptiness)
    return {ref: ann.value for ref, ann in anns.items()}


def _col(uid: str, column: str) -> ColumnRef:
    return ColumnRef(SourceRef(SourceKind.MODEL, uid), column)


def test_array_agg_under_group_by_is_non_empty() -> None:
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.stg",
            "SELECT event_id, ARRAY_AGG(STRUCT(tag, weight)) AS tags FROM events GROUP BY event_id",
        ),
    )
    assert values[_col("model.app.stg", "tags")] is ArrayNonEmpty.NON_EMPTY


def test_array_agg_without_group_by_is_unknown() -> None:
    # A whole-relation ARRAY_AGG returns NULL over zero rows, so it cannot be claimed
    # non-empty.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model("model.app.allrows", "SELECT ARRAY_AGG(tag) AS tags FROM events"),
    )
    assert values[_col("model.app.allrows", "tags")] is ArrayNonEmpty.UNKNOWN


def test_array_literal_is_non_empty() -> None:
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.pivot",
            "SELECT event_id, ARRAY[STRUCT('clicks' AS k, clicks AS v)] AS metrics FROM events",
        ),
    )
    assert values[_col("model.app.pivot", "metrics")] is ArrayNonEmpty.NON_EMPTY


def test_generator_over_literal_bounds_is_non_empty() -> None:
    # A GENERATE_ARRAY over literal bounds is an intrinsic constructor, the same kind of
    # provably-non-empty array as ARRAY[...]; it clears through the column graph, so a
    # downstream UNNEST of the column drops no row.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.spine",
            "SELECT event_id, GENERATE_ARRAY(0, 23) AS hours FROM events",
        ),
    )
    assert values[_col("model.app.spine", "hours")] is ArrayNonEmpty.NON_EMPTY


def test_date_generator_over_literal_bounds_is_non_empty() -> None:
    # A calendar spine over literal date bounds is an intrinsic constructor too; it clears
    # through the column graph like the numeric generator and the literal array.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.calendar",
            "SELECT event_id, GENERATE_DATE_ARRAY(DATE '2020-01-01', DATE '2020-12-31') AS days "
            "FROM events",
        ),
    )
    assert values[_col("model.app.calendar", "days")] is ArrayNonEmpty.NON_EMPTY


def test_generator_over_column_bounds_is_unknown() -> None:
    # A column upper bound can be a count of 0, giving an empty range; not provable, so the
    # column carries no non-emptiness guarantee.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.counted",
            "SELECT event_id, GENERATE_SERIES(1, cnt) AS ns FROM events",
        ),
    )
    assert values[_col("model.app.counted", "ns")] is ArrayNonEmpty.UNKNOWN


def test_array_of_filtered_set_subqueries_is_unknown() -> None:
    # ARRAY((SELECT AS STRUCT ... FROM unnest(...) WHERE ...)) is set-returning: the filter can
    # match nothing, so the array can be empty even though it is built inline. Concatenating two
    # such arrays does not change that. (The real-world shape that exposed the bug: a column an
    # UNNEST later reads, which must stay flagged.)
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.filtered",
            "SELECT event_id, ARRAY_CONCAT("
            "  ARRAY((SELECT AS STRUCT name, value FROM UNNEST(raw_metrics) WHERE name IN ('a'))),"
            "  ARRAY((SELECT AS STRUCT name, value FROM UNNEST(raw_metrics) WHERE name IN ('b')))"
            ") AS metrics FROM events",
        ),
    )
    assert values[_col("model.app.filtered", "metrics")] is ArrayNonEmpty.UNKNOWN


def test_raw_source_array_stays_unknown() -> None:
    # The raw array column's emptiness is an ingestion fact we cannot see; a pure
    # passthrough must not claim non-emptiness (the (A) side of the worked example).
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model("model.app.passthrough", "SELECT event_id, tags FROM events"),
    )
    assert values[_col("model.app.passthrough", "tags")] is ArrayNonEmpty.UNKNOWN


def test_array_agg_ignore_nulls_of_struct_is_non_empty() -> None:
    # A STRUCT(...) is never null, so IGNORE NULLS cannot empty the group.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.stg",
            "SELECT event_id, ARRAY_AGG(STRUCT(tag, weight) IGNORE NULLS) AS tags "
            "FROM events GROUP BY event_id",
        ),
    )
    assert values[_col("model.app.stg", "tags")] is ArrayNonEmpty.NON_EMPTY


def test_array_agg_ignore_nulls_of_scalar_is_unknown() -> None:
    # An all-NULL group collapses to [] under IGNORE NULLS when the value can be null.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.stg",
            "SELECT event_id, ARRAY_AGG(tag IGNORE NULLS) AS tags FROM events GROUP BY event_id",
        ),
    )
    assert values[_col("model.app.stg", "tags")] is ArrayNonEmpty.UNKNOWN


def test_array_agg_under_empty_grouping_set_is_unknown() -> None:
    # GROUP BY () is the grand-total grouping set: it folds the whole relation into one
    # group, so over zero input rows the single output row's ARRAY_AGG is NULL/empty,
    # exactly like a whole-relation ARRAY_AGG. It must not be read as a per-group fold.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model("model.app.grand", "SELECT ARRAY_AGG(tag) AS tags FROM events GROUP BY ()"),
    )
    assert values[_col("model.app.grand", "tags")] is ArrayNonEmpty.UNKNOWN


def test_array_agg_with_filter_is_unknown() -> None:
    # ARRAY_AGG(x) FILTER (WHERE cond) keeps only matching rows, so a group whose rows
    # all fail the filter collapses to an empty array, even under a real GROUP BY.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.stg",
            "SELECT event_id, ARRAY_AGG(tag) FILTER (WHERE weight > 0) AS tags "
            "FROM events GROUP BY event_id",
        ),
    )
    assert values[_col("model.app.stg", "tags")] is ArrayNonEmpty.UNKNOWN


def test_array_agg_ignore_nulls_of_ordered_struct_is_non_empty() -> None:
    # ARRAY_AGG(STRUCT(...) ORDER BY ...) parses with an exp.Order wrapping the argument.
    # The order clause changes element order, never presence, so a STRUCT under it is still
    # provably non-null and the IGNORE NULLS group cannot empty out. This is the canonical
    # rebuilt-array idiom, so it must clear.
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.stg",
            "SELECT event_id, ARRAY_AGG(STRUCT(tag, weight) ORDER BY weight IGNORE NULLS) AS tags "
            "FROM events GROUP BY event_id",
        ),
    )
    assert values[_col("model.app.stg", "tags")] is ArrayNonEmpty.NON_EMPTY


def test_non_emptiness_carries_across_a_model_boundary() -> None:
    # The rebuilt array stays NON_EMPTY one model downstream (the (B) side).
    src = _source("source.app.raw.events")
    values = _values(
        src,
        _model(
            "model.app.stg",
            "SELECT event_id, ARRAY_AGG(STRUCT(tag, weight)) AS tags FROM events GROUP BY event_id",
        ),
        _model("model.app.mart", "SELECT event_id, tags FROM stg"),
    )
    assert values[_col("model.app.mart", "tags")] is ArrayNonEmpty.NON_EMPTY


def test_union_all_is_non_empty_only_when_every_arm_is() -> None:
    src = _source("source.app.raw.events")
    mixed = _values(
        src,
        _model(
            "model.app.u",
            "SELECT ARRAY[1] AS xs FROM events UNION ALL SELECT xs FROM events",
        ),
    )
    assert mixed[_col("model.app.u", "xs")] is ArrayNonEmpty.UNKNOWN

    both = _values(
        src,
        _model(
            "model.app.u2",
            "SELECT ARRAY[1] AS xs FROM events UNION ALL SELECT ARRAY[2, 3] AS xs FROM events",
        ),
    )
    assert both[_col("model.app.u2", "xs")] is ArrayNonEmpty.NON_EMPTY
