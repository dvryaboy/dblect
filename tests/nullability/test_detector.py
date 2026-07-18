"""The nullability-consuming detectors: a NULL-sensitive construct on an
inherited-nullable column.

Three detectors share one shape: they fire when a column the nullability property
proved NULLABLE *upstream* sits in a position where the null silently changes the
result (a GROUP BY phantom bucket, a join key that never matches, a NOT IN that goes
empty). They are the cross-model complement to the structural detectors: those need
the outer join in the same SELECT, these fire on nullability inherited from an
upstream model, which the local AST cannot see. They never double-flag the structural
layer, because they read the *upstream* relation's nullability rather than anything a
join in the local scope introduces.

The contract is one parametrized table (each construct fires on the nullable key and
stays silent on the non-null one), so a new construct is a new row rather than a new
pair of near-identical tests. One empirical check materializes the chain in duckdb and
confirms the flagged group key really carries a NULL bucket.
"""

from __future__ import annotations

import duckdb
import pytest

from dblect.adapters import profile_for_adapter
from dblect.lineage.properties.functional_dependency import FD, FDSet
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from dblect.nullability.detector import (
    NullableCause,
    detect_join_on_nullable_key,
    detect_not_exists_on_nullable_key,
    detect_not_in_nullable_subquery,
    make_nullability_detectors,
)
from dblect.sql import Finding, FindingKind, parse_sql

_DUCKDB = profile_for_adapter("duckdb")


def _source(name: str) -> Node:
    return Node(
        unique_id=f"source.shop.raw.{name}",
        name=name,
        resource_type=ResourceType.SOURCE,
        fqn=("shop", name),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _model(name: str, sql: str, *, depends_on: frozenset[str]) -> Node:
    return Node(
        unique_id=f"model.shop.{name}",
        name=name,
        resource_type=ResourceType.MODEL,
        fqn=("shop", name),
        package_name="shop",
        schema="analytics",
        raw_code=sql,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        depends_on=depends_on,
    )


def _not_null(source_name: str, column: str) -> Node:
    target = f"source.shop.raw.{source_name}"
    return Node(
        unique_id=f"test.shop.{source_name}_{column}_not_null",
        name=f"{source_name}_{column}_not_null",
        resource_type=ResourceType.OTHER,
        fqn=("shop", f"{source_name}_{column}_not_null"),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="not_null", kwargs={"column_name": column}),
        attached_node=target,
    )


# stg LEFT JOINs lkp, so stg.tag is the optional side: NULLABLE downstream even though
# lkp.tag is declared not_null. stg.id comes from the required side: NON_NULL.
_STG_SQL = "SELECT a.id AS id, b.tag AS tag FROM base a LEFT JOIN lkp b ON a.fk = b.id"

# stg2.tag is NULLABLE because NULLIF can return NULL, a cause that is *not* an outer
# join. It exercises the honest-degradation branch: the finding names no fabricated cause.
_STG2_SQL = "SELECT a.id AS id, NULLIF(a.fk, 0) AS tag FROM base a"


def _manifest(mart_sql: str) -> Manifest:
    nodes = [
        _source("base"),
        _source("lkp"),
        _source("other"),
        _not_null("base", "id"),
        _not_null("base", "fk"),
        _not_null("lkp", "id"),
        _not_null("lkp", "tag"),
        _not_null("other", "k"),
        _model(
            "stg", _STG_SQL, depends_on=frozenset({"source.shop.raw.base", "source.shop.raw.lkp"})
        ),
        _model("stg2", _STG2_SQL, depends_on=frozenset({"source.shop.raw.base"})),
        _model(
            "mart",
            mart_sql,
            depends_on=frozenset({"model.shop.stg", "model.shop.stg2", "source.shop.raw.other"}),
        ),
    ]
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _join_finding(mart_sql: str) -> Finding:
    """The single ``join_on_nullable_key`` finding ``mart`` raises (asserts exactly one)."""
    detectors = make_nullability_detectors(_manifest(mart_sql), _DUCKDB)
    tree = parse_sql(mart_sql, dialect="duckdb")
    findings = [
        f
        for detector in detectors
        for f in detector(tree)
        if f.kind is FindingKind.JOIN_ON_NULLABLE_KEY
    ]
    assert len(findings) == 1
    return findings[0]


def _kinds(mart_sql: str) -> list[FindingKind]:
    """Every nullability finding the detectors raise on ``mart``."""
    detectors = make_nullability_detectors(_manifest(mart_sql), _DUCKDB)
    tree = parse_sql(mart_sql, dialect="duckdb")
    return [f.kind for detector in detectors for f in detector(tree)]


# ``stg.tag`` is nullable upstream (the optional side of stg's LEFT JOIN); ``stg.id`` and
# ``other.k`` are NON_NULL. Each detector fires on the nullable key and stays silent on the
# non-null one. The local-outer-join GROUP BY is left to the structural detector, since
# this layer reasons only about single-source, join-free scopes.
_CASES: list[tuple[str, str, bool]] = [
    ("group-by/nullable", "SELECT tag, count(*) AS n FROM stg GROUP BY tag", True),
    # An ordinal names the projection, so the inherited-nullable key is grouped on just the
    # same and the detector has to read through the position rather than past it.
    ("group-by/nullable-ordinal", "SELECT tag, count(*) AS n FROM stg GROUP BY 1", True),
    ("group-by/non-null", "SELECT id, count(*) AS n FROM stg GROUP BY id", False),
    (
        "group-by/local-join",
        "SELECT b.tag AS tag, count(*) AS n FROM base a LEFT JOIN lkp b ON a.fk = b.id GROUP BY b.tag",
        False,
    ),
    ("join/nullable", "SELECT s.id FROM other o JOIN stg s ON o.k = s.tag", True),
    ("join/non-null", "SELECT s.id FROM other o JOIN stg s ON o.k = s.id", False),
    ("not-in/nullable", "SELECT id FROM stg WHERE id NOT IN (SELECT tag FROM stg)", True),
    ("not-in/non-null", "SELECT id FROM stg WHERE id NOT IN (SELECT id FROM stg)", False),
]
_KIND_OF = {
    "group-by": FindingKind.NULL_GROUP_ON_NULLABLE_KEY,
    "join": FindingKind.JOIN_ON_NULLABLE_KEY,
    "not-in": FindingKind.NOT_IN_NULLABLE_SUBQUERY,
}


@pytest.mark.parametrize(
    ("sql", "kind", "fires"),
    [(sql, _KIND_OF[name.split("/")[0]], fires) for name, sql, fires in _CASES],
    ids=[name for name, _sql, _fires in _CASES],
)
def test_detector_fires_only_on_an_inherited_nullable_key(
    sql: str, kind: FindingKind, fires: bool
) -> None:
    assert (kind in _kinds(sql)) is fires


def test_flagged_group_key_really_carries_a_null_bucket() -> None:
    # Empirical: materialize base -> stg -> mart with an unmatched row, confirm the
    # finding fires and the grouped output genuinely has a NULL key.
    mart_sql = "SELECT tag, count(*) AS n FROM stg GROUP BY tag"
    assert FindingKind.NULL_GROUP_ON_NULLABLE_KEY in _kinds(mart_sql)

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE base (id INTEGER, fk INTEGER)")
        con.execute("CREATE TABLE lkp (id INTEGER, tag INTEGER)")
        con.executemany("INSERT INTO base VALUES (?, ?)", [[1, 99]])  # fk 99 has no lkp match
        con.executemany("INSERT INTO lkp VALUES (?, ?)", [[1, 7]])
        con.execute(f"CREATE TABLE stg AS {_STG_SQL}")
        con.execute(f"CREATE TABLE mart AS {mart_sql}")
        null_groups = con.execute("SELECT count(*) FROM mart WHERE tag IS NULL").fetchone()
        assert null_groups is not None
        assert null_groups[0] == 1  # the unmatched row formed a phantom NULL group
    finally:
        con.close()


# The durable guard is the upstream not_null test: it turns silent row loss into a loud
# test failure on the producing model. The finding recommends it while keeping the local
# filter/COALESCE options, so the reader sees both the cheapest durable guard and the
# in-place fixes.
def test_join_finding_recommends_an_upstream_not_null_test_and_keeps_local_fixes() -> None:
    msg = _join_finding("SELECT s.id FROM other o JOIN stg s ON o.k = s.tag").message
    assert "not_null" in msg  # the durable upstream guard
    assert "stg" in msg  # named on the producing model
    lowered = msg.lower()
    assert "filter" in lowered  # local filter still offered
    assert "coalesce" in lowered  # local COALESCE still offered


# When the substrate attributes the upstream nullability to an outer join, the finding
# names that cause so the reader does not have to rediscover it. ``stg.tag`` is the
# optional side of stg's LEFT JOIN.
def test_join_finding_names_outer_join_cause_when_derivable() -> None:
    msg = _join_finding("SELECT s.id FROM other o JOIN stg s ON o.k = s.tag").message
    # The cause names *how the column was produced*, distinct from the boilerplate that
    # explains how the local join treats a NULL key. "produced via" is the cause marker.
    assert "produced via" in msg.lower()
    assert "left join" in msg.lower()


# ``stg2.tag`` is NULLABLE via NULLIF, not an outer join. The finding must not fabricate
# an outer-join cause it cannot support from the substrate (the silent-when-unsure posture).
def test_join_finding_does_not_fabricate_a_cause_when_not_derivable() -> None:
    msg = _join_finding("SELECT s.id FROM other o JOIN stg2 s ON o.k = s.tag").message
    assert "stg2" in msg
    assert "not_null" in msg  # the durable guard is still recommended
    assert "left join" not in msg.lower()
    assert "produced via" not in msg.lower()


# The detector reasons about join-side preservation and per-join collapse purely from the
# AST plus the per-relation nullable index, so these contracts are pinned at that boundary
# rather than through the manifest machinery the integration cases above exercise.
def _join_keys(sql: str, nullable: dict[str, frozenset[str]]) -> tuple[Finding, ...]:
    return detect_join_on_nullable_key(parse_sql(sql, dialect="duckdb"), nullable_by_name=nullable)


_STG_NULLABLE = {"stg": frozenset({"tag"})}


# A nullable key on the non-preserved side of an outer join is benign: those rows not
# joining is the outer join's defining semantics, not a silent hazard. Here ``other`` is
# the preserved left side and ``stg`` the optional right side, so the detector stays quiet.
def test_join_does_not_flag_non_preserved_side_of_outer_join() -> None:
    sql = "SELECT o.k FROM other o LEFT JOIN stg s ON o.k = s.tag"
    assert _join_keys(sql, _STG_NULLABLE) == ()


# The preserved side of an outer join is the case worth flagging: those rows survive, but a
# NULL key silently never matches. Here ``stg`` is the preserved left side. The message
# frames a silent non-match, not row loss, because no preserved row is dropped.
def test_join_flags_preserved_side_of_outer_join_without_row_loss_framing() -> None:
    sql = "SELECT s.id FROM stg s LEFT JOIN other o ON s.tag = o.k"
    findings = _join_keys(sql, _STG_NULLABLE)
    assert len(findings) == 1
    lowered = findings[0].message.lower()
    assert "drop" not in lowered  # the preserved row is kept, not dropped
    assert "match" in lowered  # the hazard is the silent non-match


# On an inner join a NULL key really is dropped from the result, so that finding keeps the
# row-loss framing. Pinning both framings guards the inner/outer distinction.
def test_inner_join_keeps_row_loss_framing() -> None:
    sql = "SELECT s.id FROM other o JOIN stg s ON o.k = s.tag"
    findings = _join_keys(sql, _STG_NULLABLE)
    assert len(findings) == 1
    assert "drop" in findings[0].message.lower()


# A composite-key join is one decision to look at. The detector emits one finding listing
# the spanned key columns rather than one finding per column.
def test_composite_key_join_yields_one_finding_listing_all_columns() -> None:
    nullable = {"dim": frozenset({"k1", "k2", "k3", "k4"})}
    sql = (
        "SELECT f.v FROM fact f JOIN dim d "
        "ON f.k1 = d.k1 AND f.k2 = d.k2 AND f.k3 = d.k3 AND f.k4 = d.k4"
    )
    findings = _join_keys(sql, nullable)
    assert len(findings) == 1
    msg = findings[0].message
    assert all(col in msg for col in ("k1", "k2", "k3", "k4"))


# Preservation is gated per join, not per SELECT. An outer join earlier in the same scope
# leaves ``stg`` optional, but the later inner join on ``s.tag`` still silently drops the
# NULL-key rows, so that genuine row loss must still be flagged with row-loss framing.
def test_inner_join_still_flagged_when_an_unrelated_outer_join_made_the_side_optional() -> None:
    sql = "SELECT o.k FROM other o LEFT JOIN stg s ON o.k = s.id JOIN third t ON s.tag = t.k"
    findings = _join_keys(sql, _STG_NULLABLE)
    assert len(findings) == 1
    assert "drop" in findings[0].message.lower()


# A semi join filters its left rows down to those with a match, so a NULL key silently drops
# the left row just as an inner join would, on either side: the left key never matches, or the
# right key that would have matched is invisible. The detector flags it with the SEMI label
# and row-loss framing.
def test_semi_join_flags_either_side_with_row_loss_framing() -> None:
    right_nullable = "SELECT a.x FROM other a LEFT SEMI JOIN stg s ON a.k = s.tag"
    left_nullable = "SELECT a.x FROM stg a LEFT SEMI JOIN other o ON a.tag = o.k"
    for sql in (right_nullable, left_nullable):
        findings = _join_keys(sql, _STG_NULLABLE)
        assert len(findings) == 1
        lowered = findings[0].message.lower()
        assert "semi join" in lowered
        assert "drop" in lowered  # the left row is silently dropped on a NULL key


# An anti join inverts the hazard, and the two sides split. A NULL on the MATCHED side simply
# fails to match, which is the native anti-join's null-safe semantics (unlike NOT IN), so a
# nullable matched key is benign here and stays silent.
def test_anti_join_matched_side_null_is_silent() -> None:
    # probe is ``other`` (no nullable key); the matched ``stg.tag`` is the nullable one.
    right_nullable = "SELECT a.x FROM other a LEFT ANTI JOIN stg s ON a.k = s.tag"
    assert _join_keys(right_nullable, _STG_NULLABLE) == ()


# A NULL on the PROBE side is the hazard: it matches nothing, so the anti-join keeps the row as
# a spurious non-match rather than excluding it. The finding takes the inverted framing (kept,
# not dropped) and names the anti-join.
def test_anti_join_probe_side_null_fires_with_kept_framing() -> None:
    # probe is ``stg`` (nullable ``tag``); the matched ``other`` has no nullable key.
    left_nullable = "SELECT a.x FROM stg a LEFT ANTI JOIN other o ON a.tag = o.k"
    findings = _join_keys(left_nullable, _STG_NULLABLE)
    assert len(findings) == 1
    lowered = findings[0].message.lower()
    assert "anti join" in lowered
    assert "kept" in lowered  # the row survives as a spurious non-match
    assert "drop" not in lowered  # it is not row loss


# NOT EXISTS is the same anti-join operator written as a correlated subquery, and carries the
# same probe-side hazard: a NULL correlation key on the probe matches nothing, so NOT EXISTS is
# true and the row is kept as a spurious non-match. The dedicated detector names it "NOT EXISTS".
def test_not_exists_probe_side_null_fires_with_kept_framing() -> None:
    sql = "SELECT s.id FROM stg s WHERE NOT EXISTS (SELECT 1 FROM other o WHERE o.k = s.tag)"
    findings = detect_not_exists_on_nullable_key(
        parse_sql(sql, dialect="duckdb"), nullable_by_name=_STG_NULLABLE
    )
    assert len(findings) == 1
    lowered = findings[0].message.lower()
    assert "not exists" in lowered
    assert "kept" in lowered
    assert "drop" not in lowered


def test_not_exists_matched_side_null_is_silent() -> None:
    """A NULL on the matched (inner subquery) side simply fails to match, the null-safe
    semantics that make NOT EXISTS the recommended replacement for NOT IN; so it stays silent."""
    sql = "SELECT o.x FROM other o WHERE NOT EXISTS (SELECT 1 FROM stg s WHERE s.tag = o.k)"
    findings = detect_not_exists_on_nullable_key(
        parse_sql(sql, dialect="duckdb"), nullable_by_name=_STG_NULLABLE
    )
    assert findings == ()


# The NOT IN "result silently empty" hazard is sound only where NOT IN is a top-level WHERE
# conjunct. The detector shares the anti-join classifier's scope for exactly that reason: a NULL
# in the subquery makes the predicate never true, which empties the result only when nothing else
# in the WHERE can admit a row. Under an OR the other disjunct still admits rows, so the NOT IN is
# a dead disjunct rather than an emptied result, a different hazard the detector does not claim.
def _not_in_keys(sql: str) -> tuple[Finding, ...]:
    return detect_not_in_nullable_subquery(
        parse_sql(sql, dialect="duckdb"), nullable_by_name=_STG_NULLABLE
    )


def test_not_in_conjoined_with_another_predicate_still_fires() -> None:
    # AND keeps NOT IN a top-level conjunct: a NULL in the subquery still empties the result. The
    # bare top-level-conjunct case is the parametrized ``not-in/nullable`` scenario above; this
    # adds the AND scope that the shared classifier newly threads through.
    sql = "SELECT id FROM stg WHERE id > 0 AND id NOT IN (SELECT tag FROM stg)"
    assert len(_not_in_keys(sql)) == 1
    assert _not_in_keys(sql)[0].kind is FindingKind.NOT_IN_NULLABLE_SUBQUERY


def test_not_in_under_or_is_silent() -> None:
    # Under OR the other disjunct still admits rows on a subquery NULL, so the result is not
    # silently empty and the finding must not claim it is.
    sql = "SELECT id FROM stg WHERE id > 0 OR id NOT IN (SELECT tag FROM stg)"
    assert _not_in_keys(sql) == ()


def test_not_in_with_expression_left_side_still_fires() -> None:
    # The empty-result footgun lives on the subquery (matched) side, so the left side need not be
    # a bare column: a NULL in the projected subquery column empties the result all the same.
    sql = "SELECT id FROM stg WHERE coalesce(id, 0) NOT IN (SELECT tag FROM stg)"
    assert len(_not_in_keys(sql)) == 1


# A FULL join drops no rows: both sides survive NULL-padded, so a NULL key is the silent
# non-match hazard on either side. The detector flags it with the kept-row outer framing,
# not row loss.
def test_full_join_flags_with_outer_framing_because_both_sides_survive() -> None:
    sql = "SELECT s.id FROM stg s FULL JOIN other o ON s.tag = o.k"
    findings = _join_keys(sql, _STG_NULLABLE)
    assert len(findings) == 1
    lowered = findings[0].message.lower()
    assert "full outer join" in lowered
    assert "drop" not in lowered  # both sides are kept, padded


# A mixed-cause composite key stays one finding, but each column carries its own cause: the
# attributed column names its provenance inline, the unattributed one names none, so neither
# borrows the other's. This is the per-column precision the single-clause shape could not give.
def test_composite_key_attributes_each_column_cause_independently() -> None:
    nullable = {"dim": frozenset({"k1", "k2"})}
    cause = {"dim": {"k1": NullableCause.LEFT_JOIN}}  # k2 has no attributed cause
    sql = "SELECT f.v FROM fact f JOIN dim d ON f.k1 = d.k1 AND f.k2 = d.k2"
    findings = detect_join_on_nullable_key(
        parse_sql(sql, dialect="duckdb"), nullable_by_name=nullable, cause_by_name=cause
    )
    assert len(findings) == 1
    msg = findings[0].message
    assert "k1 (produced via a left join" in msg  # k1 names its own cause inline
    assert "k2 (produced" not in msg  # k2 does not borrow it
    assert msg.lower().count("produced via") == 1  # named once, for k1 only


# When every spanned column shares one cause, the listing stays clean and the clause trails
# once for the whole key rather than repeating inline on each column.
def test_composite_key_names_the_shared_cause_once() -> None:
    nullable = {"dim": frozenset({"k1", "k2"})}
    cause = {"dim": {"k1": NullableCause.LEFT_JOIN, "k2": NullableCause.LEFT_JOIN}}
    sql = "SELECT f.v FROM fact f JOIN dim d ON f.k1 = d.k1 AND f.k2 = d.k2"
    findings = detect_join_on_nullable_key(
        parse_sql(sql, dialect="duckdb"), nullable_by_name=nullable, cause_by_name=cause
    )
    assert len(findings) == 1
    msg = findings[0].message
    assert "k1, k2, which are nullable upstream" in msg  # clean listing, no inline clause
    assert msg.lower().count("produced via a left join") == 1  # trailing clause, once


# Two columns with genuinely different upstream causes each name their own inline, in one
# finding, so a left-join-padded key and a right-join-padded key are not conflated.
def test_composite_key_with_distinct_causes_names_each_inline() -> None:
    nullable = {"dim": frozenset({"k1", "k2"})}
    cause = {"dim": {"k1": NullableCause.LEFT_JOIN, "k2": NullableCause.RIGHT_JOIN}}
    sql = "SELECT f.v FROM fact f JOIN dim d ON f.k1 = d.k1 AND f.k2 = d.k2"
    findings = detect_join_on_nullable_key(
        parse_sql(sql, dialect="duckdb"), nullable_by_name=nullable, cause_by_name=cause
    )
    assert len(findings) == 1
    msg = findings[0].message
    assert "k1 (produced via a left join" in msg
    assert "k2 (produced via a right join" in msg


# --- grounding on declared determines --------------------------------------------
#
# A denormalized join over a declared hierarchy (a fact table keyed on store_id, region_id,
# country_id where store_id determines the rest) is one logical key, not three co-equal
# suspicious columns. Given the declared ``determines`` chain, the detector reduces the
# spanned columns to the chain's root, reports the declared key as one unit, and points at
# dropping the redundant equalities. It stays one finding and never goes silent: the folded
# columns are still nullable, so a genuine null-non-match risk remains. Silence is only ever
# a non-null key's job.

# The retail hierarchy: store_id -> region_id -> country_id, all nullable in ``dim``.
_HIERARCHY_SQL = (
    "SELECT f.v FROM fact f JOIN dim d "
    "ON f.store_id = d.store_id AND f.region_id = d.region_id AND f.country_id = d.country_id"
)
_HIERARCHY_NULLABLE = {"dim": frozenset({"store_id", "region_id", "country_id"})}
_HIERARCHY_FDS = {
    "dim": FDSet.of(
        FD(frozenset({"store_id"}), "region_id"), FD(frozenset({"region_id"}), "country_id")
    )
}


def _join_keys_fd(
    sql: str, nullable: dict[str, frozenset[str]], fd_by_name: dict[str, FDSet]
) -> tuple[Finding, ...]:
    return detect_join_on_nullable_key(
        parse_sql(sql, dialect="duckdb"), nullable_by_name=nullable, fd_by_name=fd_by_name
    )


def test_declared_determines_chain_consolidates_to_the_root_key() -> None:
    findings = _join_keys_fd(_HIERARCHY_SQL, _HIERARCHY_NULLABLE, _HIERARCHY_FDS)
    assert len(findings) == 1
    msg = findings[0].message
    # The key reports as the declared root, singular, not three co-equal columns.
    assert "keys on store_id, which is nullable upstream" in msg
    # The determined members are named as redundant, with the drop-the-conditions remedy.
    assert "region_id" in msg
    assert "country_id" in msg
    assert "functionally determined by the declared key store_id" in msg
    assert "redundant" in msg.lower()
    # The not_null guard targets the key that remains, not the folded members.
    assert "not_null test on store_id in 'dim'" in msg


def test_undeclared_hierarchy_lists_every_column_as_before() -> None:
    """With no ``determines`` known nothing folds, so the join reads exactly as it did before
    the grounding: all spanned columns listed, no redundancy clause."""
    findings = _join_keys_fd(_HIERARCHY_SQL, _HIERARCHY_NULLABLE, {})
    assert len(findings) == 1
    msg = findings[0].message
    assert all(col in msg for col in ("store_id", "region_id", "country_id"))
    assert "redundant" not in msg.lower()


def test_partial_chain_folds_only_the_determined_column() -> None:
    """Only ``store_id -> region_id`` is declared; ``country_id`` stands on its own. The cover
    keeps both irreducible columns and folds only ``region_id``."""
    fds = {"dim": FDSet.of(FD(frozenset({"store_id"}), "region_id"))}
    findings = _join_keys_fd(_HIERARCHY_SQL, _HIERARCHY_NULLABLE, fds)
    assert len(findings) == 1
    msg = findings[0].message
    assert "keys on country_id, store_id, which are nullable upstream" in msg
    # The folded column is named as determined by the reported key (the cover determines every
    # column it folded), so a partial chain keeps both irreducible columns as the key.
    assert (
        "region_id in 'dim' (functionally determined by the declared key country_id, store_id)"
        in msg
    )


def test_determines_consolidates_but_never_silences() -> None:
    """A non-null determinant does not license silence: the determined columns are still
    nullable, so the null-non-match risk is real and the finding still fires, consolidated onto
    the nullable root of the chain. Here ``store_id`` is non-null (absent from the index), so the
    nullable key is ``region_id -> country_id``, reducing to ``region_id``."""
    nullable = {"dim": frozenset({"region_id", "country_id"})}
    findings = _join_keys_fd(_HIERARCHY_SQL, nullable, _HIERARCHY_FDS)
    assert len(findings) == 1
    msg = findings[0].message
    assert "keys on region_id, which is nullable upstream" in msg
    assert "country_id in 'dim' (functionally determined by the declared key region_id)" in msg
