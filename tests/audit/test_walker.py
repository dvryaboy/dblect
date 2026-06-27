"""Tests for the audit walker over a parsed manifest."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dblect.adapters import AdapterProfile, profile_for_adapter
from dblect.audit import (
    DEFAULT_DETECTORS,
    AuditReport,
    SpanBasis,
    run_audit,
)
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import FindingKind

_DUCKDB = profile_for_adapter("duckdb")


@pytest.fixture(scope="module")
def jaffle(jaffle_manifest_path: Path) -> Manifest:
    return Manifest.from_file(jaffle_manifest_path)


@pytest.fixture(scope="module")
def jaffle_report(jaffle: Manifest) -> AuditReport:
    return run_audit(jaffle, _DUCKDB)


def test_audit_scans_every_model_with_compiled_code(
    jaffle: Manifest, jaffle_report: AuditReport
) -> None:
    # Every jaffle model carries compiled_code from `dbt compile`, so none are
    # skipped.
    assert jaffle_report.models_scanned == len(jaffle.models)
    assert jaffle_report.skipped == ()


def test_audit_locates_customers_null_group(jaffle_report: AuditReport) -> None:
    hits = [
        lf
        for lf in jaffle_report.findings
        if lf.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
        and lf.model_unique_id == "model.jaffle_shop.customers"
    ]
    assert len(hits) == 1
    [hit] = hits
    assert hit.file_path == "models/customers.sql"
    assert hit.finding.line_start > 0
    assert hit.finding.line_start <= hit.finding.line_end


def test_audit_skips_models_without_sql(jaffle: Manifest) -> None:
    # Drop both raw_code and compiled_code on one model to simulate the
    # source/seed shape.
    [a_model_uid] = list(jaffle.models)[:1]
    drained = dict(jaffle.nodes)
    drained[a_model_uid] = _without_sql(drained[a_model_uid])
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes=drained,
    )
    report = run_audit(altered, _DUCKDB)
    assert any(
        s.unique_id == a_model_uid and s.reason.startswith("no compiled SQL")
        for s in report.skipped
    )
    assert report.models_scanned == len(jaffle.models) - 1


def test_audit_records_parse_errors_without_blowing_up(jaffle: Manifest) -> None:
    # Replace one model's SQL with something sqlglot can't parse.
    [a_model_uid] = list(jaffle.models)[:1]
    drained = dict(jaffle.nodes)
    drained[a_model_uid] = _with_compiled_code(drained[a_model_uid], "select from where")
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes=drained,
    )
    report = run_audit(altered, _DUCKDB)
    assert any(
        s.unique_id == a_model_uid and s.reason.startswith("parse error") for s in report.skipped
    )
    # Other models still got scanned.
    assert report.models_scanned == len(jaffle.models) - 1


def test_empty_detector_list_gates_the_structural_detectors(jaffle: Manifest) -> None:
    # The `detectors` arg gates the structural detectors: an empty tuple drops them,
    # so jaffle's structural findings disappear, while the scan loop still runs over
    # every model. The context-bound detectors (non-determinism, uniqueness,
    # nullability) are built in run_audit and not gated by this arg.
    structural = {
        FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
        FindingKind.COALESCE_ON_JOIN_KEY,
        FindingKind.UNORDERED_RANKING_WINDOW,
        FindingKind.UNORDERED_AGGREGATE,
        FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE,
    }
    full_kinds = {lf.finding.kind for lf in run_audit(jaffle, _DUCKDB).findings}
    assert full_kinds & structural, "jaffle should exercise at least one structural detector"

    gated = run_audit(jaffle, _DUCKDB, detectors=())
    assert gated.models_scanned == len(jaffle.models)
    assert {lf.finding.kind for lf in gated.findings}.isdisjoint(structural)


def test_counts_by_kind_matches_findings(jaffle_report: AuditReport) -> None:
    actual = jaffle_report.counts_by_kind
    expected: dict[FindingKind, int] = {}
    for lf in jaffle_report.findings:
        expected[lf.finding.kind] = expected.get(lf.finding.kind, 0) + 1
    assert actual == expected


def test_default_detectors_includes_every_sql_detector_exactly_once() -> None:
    # The audit layer must wire in every structural `detect_*` the SQL layer
    # exports. If a new one lands in dblect.sql but the walker forgets to add it to
    # DEFAULT_DETECTORS, audits would silently skip it; this test catches that
    # without pinning the count. Context-bound detectors are `make_*` factories
    # (e.g. make_non_determinism_detector), built per run in run_audit, so they sit
    # outside this guard by design.
    import dblect.sql as sql_module

    # `detect_inner_flatten_row_drop` is exported structurally (for standalone scans) but
    # run in run_audit only through its fact-grounded factory, which threads the
    # cross-model array-non-emptiness map in; wiring it into DEFAULT_DETECTORS too would
    # double-report it. So it is deliberately context-bound, like the `make_*` factories.
    context_bound = {sql_module.detect_inner_flatten_row_drop}
    sql_detectors = {
        getattr(sql_module, name)
        for name in dir(sql_module)
        if name.startswith("detect_") and callable(getattr(sql_module, name))
    }
    default_set = set(DEFAULT_DETECTORS)
    assert sql_detectors - context_bound == default_set
    assert not (context_bound & default_set), (
        "a context-bound detector is also in DEFAULT_DETECTORS"
    )
    assert len(DEFAULT_DETECTORS) == len(default_set), "DEFAULT_DETECTORS contains duplicates"


def test_located_finding_carries_file_path_for_every_scanned_model(
    jaffle: Manifest, jaffle_report: AuditReport
) -> None:
    expected_paths = {n.unique_id: n.original_file_path for n in jaffle.models.values()}
    for lf in jaffle_report.findings:
        assert lf.file_path == expected_paths[lf.model_unique_id]


def test_suppression_silences_matching_finding_end_to_end(jaffle: Manifest) -> None:
    # A code-specific `-- noqa` on the GROUP BY line silences the null-group finding there.
    # This model's finding is compiled-relative (the back-map declines), so the directive
    # is matched in compiled space. A developer edits the model source and `dbt compile`
    # renders the comment into the compiled SQL on the same construct line, so the fixture
    # carries it in both texts.
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.raw_code is not None
    assert customers.compiled_code is not None
    coded = "group by orders.customer_id -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN"
    suppressed_raw = customers.raw_code.replace("group by orders.customer_id", coded)
    suppressed_compiled = customers.compiled_code.replace("group by orders.customer_id", coded)
    assert suppressed_raw != customers.raw_code, "test setup failed to find target line"
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes={
            **jaffle.nodes,
            customers.unique_id: replace(
                customers, raw_code=suppressed_raw, compiled_code=suppressed_compiled
            ),
        },
    )
    report = run_audit(altered, _DUCKDB)
    # The customers null-group finding is gone from active findings.
    null_group_hits = [
        lf
        for lf in report.findings
        if lf.model_unique_id == customers.unique_id
        and lf.finding.kind.value == "null_group_after_outer_join"
    ]
    assert null_group_hits == []
    # And it shows up in suppressed, recorded as a code-specific (not bare) directive.
    suppressed_hits = [
        s for s in report.suppressed if s.located.model_unique_id == customers.unique_id
    ]
    assert any(
        not s.bare and s.located.finding.kind.value == "null_group_after_outer_join"
        for s in suppressed_hits
    )


def test_bare_noqa_silences_all_kinds_on_its_line(jaffle: Manifest) -> None:
    # A bare `-- noqa` on the GROUP BY line silences every kind there, recorded as bare.
    # The finding is compiled-relative, so the directive rides into the compiled SQL the
    # same way `dbt compile` would carry the developer's comment.
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.raw_code is not None
    assert customers.compiled_code is not None
    bare = "group by orders.customer_id -- noqa"
    suppressed_raw = customers.raw_code.replace("group by orders.customer_id", bare)
    suppressed_compiled = customers.compiled_code.replace("group by orders.customer_id", bare)
    assert suppressed_raw != customers.raw_code, "test setup failed to find target line"
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes={
            **jaffle.nodes,
            customers.unique_id: replace(
                customers, raw_code=suppressed_raw, compiled_code=suppressed_compiled
            ),
        },
    )
    report = run_audit(altered, _DUCKDB)
    null_group_hits = [
        lf
        for lf in report.findings
        if lf.model_unique_id == customers.unique_id
        and lf.finding.kind.value == "null_group_after_outer_join"
    ]
    assert null_group_hits == []
    suppressed_hits = [
        s for s in report.suppressed if s.located.model_unique_id == customers.unique_id
    ]
    assert any(s.bare for s in suppressed_hits)


def test_unsuppressed_findings_in_other_models_still_fire(jaffle: Manifest) -> None:
    # Suppressing in customers.sql doesn't affect detection elsewhere.
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.raw_code is not None
    blanket = "-- noqa\n" + customers.raw_code
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes={
            **jaffle.nodes,
            customers.unique_id: replace(customers, raw_code=blanket),
        },
    )
    report = run_audit(altered, _DUCKDB)
    # The blanket suppression at line 1 only covers line 1-2, not the GROUP BY.
    # So the customers null-group finding still surfaces.
    still_there = [lf for lf in report.findings if lf.model_unique_id == customers.unique_id]
    assert still_there, "A line-1 directive should not blanket-suppress findings on far-down lines"
    # No SuppressedFinding objects were produced from customers (the directive
    # didn't actually match anything).
    assert not any(s.located.model_unique_id == customers.unique_id for s in report.suppressed), (
        "Line-1 directive matched nothing, so suppressed should be empty for customers"
    )


def test_run_audit_parses_each_model_once(
    jaffle: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The facts pre-pass and the per-model detector loop used to each parse
    # every model's compiled SQL, which was twice the work. The walker now
    # parses once and shares the trees. Lock that in by counting calls to
    # sqlglot.parse (the multi-statement door the parser uses to split a
    # compiled script into its result statement) against each model's compiled SQL.
    from typing import Any

    import sqlglot

    model_sqls = {m.compiled_code for m in jaffle.models.values() if m.compiled_code is not None}
    counts: dict[str, int] = {}
    real_parse = sqlglot.parse

    def counting_parse(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql in model_sqls:
            counts[sql] = counts.get(sql, 0) + 1
        return real_parse(sql, *args, **kwargs)  # pyright: ignore[reportUnknownVariableType]

    monkeypatch.setattr(sqlglot, "parse", counting_parse)
    run_audit(jaffle, _DUCKDB)
    assert set(counts) == model_sqls, "every model's compiled SQL should be parsed"
    repeated = {n for n in counts.values() if n > 1}
    assert not repeated, f"each model's SQL should parse exactly once; counts: {counts.values()}"


def _user_country(raw: str, compiled: str) -> Manifest:
    """A one-model manifest for `model.pkg.user_country`, varying only the raw template
    and its compiled SQL: the two inputs the back-map tests turn."""
    node = Node(
        unique_id="model.pkg.user_country",
        name="user_country",
        resource_type=ResourceType.MODEL,
        fqn=("pkg", "user_country"),
        package_name="pkg",
        schema=None,
        raw_code=raw,
        compiled_code=compiled,
        original_file_path="models/user_country.sql",
        columns={},
    )
    return Manifest(schema_version="x", adapter_type="duckdb", nodes={node.unique_id: node})


def test_macro_emitted_join_visible_in_compiled_code() -> None:
    # Regression guard for "we analyze compiled_code". A model whose LEFT
    # JOIN comes from a macro call: in the on-disk template the join is
    # invisible (`{{ join_country(u) }}`), but the compiled SQL has the
    # join expanded inline, so the null-group-after-outer-join detector
    # sees it.
    compiled_sql = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "left join dim_country d on u.country_code = d.code\n"
        "group by u.user_id, d.country"
    )
    manifest = _user_country(
        raw=(
            "select u.user_id, d.country, count(*) as n\n"
            "from users u\n"
            "{{ join_country(u) }}\n"
            "group by u.user_id, d.country"
        ),
        compiled=compiled_sql,
    )
    report = run_audit(manifest, _DUCKDB)
    assert any(
        lf.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN for lf in report.findings
    ), "compiled path should see the macro-emitted LEFT JOIN and flag the GROUP BY"


def test_finding_back_maps_compiled_span_to_the_source_line() -> None:
    # A macro expands to two compiled lines, pushing the GROUP BY from source line 4
    # to compiled line 5. The finding's compiled span is what the parser saw; its
    # reported span is back-mapped onto the on-disk template, so the user is pointed
    # at the line they wrote.
    raw = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "{{ join_country(u) }}\n"
        "group by u.user_id, d.country"
    )
    compiled = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "left join dim_country d\n"
        "  on u.country_code = d.code\n"
        "group by u.user_id, d.country"
    )
    manifest = _user_country(raw, compiled)
    [hit] = [
        lf
        for lf in run_audit(manifest, _DUCKDB).findings
        if lf.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    ]
    # Compiled span is preserved on the raw finding; the located span is back-mapped.
    assert hit.finding.line_start == 5
    span = hit.located_span
    assert span.basis is SpanBasis.SOURCE
    assert (span.line_start, span.line_end) == (4, 4)


def test_noqa_on_the_source_line_suppresses_a_macro_shifted_finding() -> None:
    # A compiled prelude plus a macro-expanded join push the GROUP BY from source line 4
    # to compiled line 6 (a two-line offset, past the one-line "directive above" slack).
    # A `-- noqa` on the source line the developer wrote (and that the report shows)
    # silences the finding, because suppression matches the back-mapped source span. The
    # directive rides through compilation verbatim, so it sits on the same construct line
    # in both texts.
    raw = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "{{ join_country(u) }}\n"
        "group by u.user_id, d.country  -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN"
    )
    compiled = (
        "-- compiled by dbt\n"
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "left join dim_country d\n"
        "  on u.country_code = d.code\n"
        "group by u.user_id, d.country  -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN"
    )
    manifest = _user_country(raw, compiled)
    report = run_audit(manifest, _DUCKDB)

    active = [
        f for f in report.findings if f.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    ]
    assert active == [], "the noqa on the source GROUP BY line should silence the finding"
    [hidden] = [
        s for s in report.suppressed if s.located.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    ]
    # The directive sits on the source line (4); the finding's compiled line (6) is two
    # past it, so only matching the back-mapped source span suppresses it.
    assert hidden.directive_line == 4
    assert hidden.located.finding.line_start == 6
    span = hidden.located.located_span
    assert span.basis is SpanBasis.SOURCE
    assert (span.line_start, span.line_end) == (4, 4)


def test_noqa_on_the_macro_call_line_suppresses_a_macro_emitted_finding() -> None:
    # The GROUP BY that trips the detector is itself emitted by `{{ country_rollup(u) }}`,
    # so it has no source line of its own: its compiled line (5) back-maps to the macro
    # call site (source line 3), not to a verbatim source line. A `-- noqa` the developer
    # placed on that call line silences it. This is the case the prior cut left
    # unsuppressable: a finding living entirely in macro-generated SQL.
    raw = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "{{ country_rollup(u) }}  -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN"
    )
    compiled = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "left join dim_country d\n"
        "  on u.country_code = d.code\n"
        "group by u.user_id, d.country"
    )
    manifest = _user_country(raw, compiled)
    report = run_audit(manifest, _DUCKDB)

    active = [
        f for f in report.findings if f.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    ]
    assert active == [], "the noqa on the macro call line should silence the emitted finding"
    [hidden] = [
        s for s in report.suppressed if s.located.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    ]
    # The construct lives at compiled line 5; it anchors to the `{{ country_rollup(u) }}`
    # call at source line 3, where the directive sits.
    assert hidden.directive_line == 3
    assert hidden.located.finding.line_start == 5
    span = hidden.located.located_span
    assert span.basis is SpanBasis.MACRO_CALL
    assert (span.line_start, span.line_end) == (3, 3)


def test_noqa_in_the_macro_body_suppresses_a_macro_emitted_finding() -> None:
    # The construct that trips the detector is emitted entirely by a macro, and the
    # `-- noqa` that guards it lives in the macro body. It renders into the compiled SQL
    # adjacent to the construct but has no line in the calling model's template. Two
    # `{{ ... }}` call sites (a config header and the macro call) leave the emitted span
    # with no single source anchor, so it stays compiled-relative; the directive is matched
    # in compiled space, where the macro body's comment sits next to the construct. One
    # comment in the shared macro then speaks for every model that calls it.
    raw = "{{ config(materialized='table') }}\n{{ country_rollup(u) }}"
    compiled = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "left join dim_country d\n"
        "  on u.country_code = d.code\n"
        "group by u.user_id, d.country  -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN"
    )
    manifest = _user_country(raw, compiled)
    report = run_audit(manifest, _DUCKDB)

    active = [
        f for f in report.findings if f.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    ]
    assert active == [], "the noqa in the macro body should silence the emitted finding"
    [hidden] = [
        s for s in report.suppressed if s.located.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    ]
    # The construct and its guarding comment share compiled line 5; the span stayed
    # compiled-relative because two call sites left it without a single source anchor, so
    # the directive matched in compiled space.
    assert hidden.directive_line == 5
    assert hidden.located.finding.line_start == 5
    span = hidden.located.located_span
    assert span.basis is SpanBasis.COMPILED
    assert (span.line_start, span.line_end) == (5, 5)


def test_resolved_adapter_reaches_non_determinism_detector(jaffle: Manifest) -> None:
    # A DuckDB-only nondeterministic builtin in a load-bearing position fires under the
    # duckdb profile and stays silent under another, proving the audit's resolved
    # adapter's `non_deterministic_builtins` reach the non-determinism detector.
    # txid_current() parses as exp.Anonymous in every dialect, so the resolved
    # adapter's name set alone decides whether it fires.
    [uid] = list(jaffle.models)[:1]
    nodes = dict(jaffle.nodes)
    nodes[uid] = _with_compiled_code(nodes[uid], "select * from a join b on a.k = txid_current()")
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes=nodes,
    )

    def nondet_hits(profile: AdapterProfile) -> int:
        report = run_audit(altered, profile)
        return sum(
            lf.model_unique_id == uid and lf.finding.kind is FindingKind.NON_DETERMINISTIC_FUNCTION
            for lf in report.findings
        )

    assert nondet_hits(profile_for_adapter("duckdb")) == 1
    assert nondet_hits(profile_for_adapter("snowflake")) == 0


def test_bigquery_jaffle_audits_end_to_end(jaffle_bigquery_manifest_path: Path) -> None:
    # Real bigquery-compiled SQL (backtick identifiers, adapter_type "bigquery") flows
    # through the audit under the bigquery profile: every model parses and is scanned,
    # and the LEFT JOIN + GROUP BY in `customers` is flagged. This is the end-to-end
    # basis for treating bigquery as a validated adapter.
    manifest = Manifest.from_file(jaffle_bigquery_manifest_path)
    assert manifest.adapter_type == "bigquery"
    report = run_audit(manifest, profile_for_adapter("bigquery"))
    assert report.models_scanned == len(manifest.models)
    assert report.skipped == ()
    assert any(
        lf.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN for lf in report.findings
    ), "the bigquery-compiled LEFT JOIN + GROUP BY should be flagged"


def _without_sql(node: Node) -> Node:
    return replace(node, raw_code=None, compiled_code=None)


def _with_compiled_code(node: Node, sql: str) -> Node:
    return replace(node, compiled_code=sql)
