"""The shared anti-join classifier.

An anti-join keeps a probe-side row only when it has no match on the other side, so it
removes rows and never multiplies them. dblect writes that idiom four ways, and
:func:`anti_joins_of` recognises all four as one operator, "rows of L with no matching row
in R on predicate P", carrying the probe relation L, the matched relation R, and the
predicate columns.

``test_the_four_surface_forms_classify_to_one_operator`` is the contract: the same anti-join
written as a native ``ANTI JOIN``, ``NOT EXISTS``, ``NOT IN``, and ``LEFT JOIN ... IS NULL``
all decode to the same (probe, matched, columns) triple. The standalone tests pin the
soundness edges: a ``LEFT JOIN ... IS NULL`` on a non-key column is not recognised (a matched
row can carry a NULL there and leak through the filter, so the idiom is not structurally an
anti-join), a ``SEMI`` join is the dual and not an anti-join, and a non-equality predicate
does not decode clean columns.
"""

from __future__ import annotations

import sqlglot

from dblect.sql.anti_join import AntiJoin, AntiJoinForm, anti_joins_of


def _only(sql: str) -> AntiJoin:
    sel = sqlglot.parse_one(sql)
    joins = anti_joins_of(sel)  # type: ignore[arg-type]
    assert len(joins) == 1, f"expected exactly one anti-join, got {joins}"
    return joins[0]


def _triple(a: AntiJoin) -> tuple[str, frozenset[str], str | None, frozenset[str]]:
    """The semantic identity of an anti-join, independent of the surface form it was written in."""
    return (a.probe_alias, a.probe_cols, a.matched_name, a.matched_cols)


# --- the contract: one operator, four surface forms -------------------------------

_NATIVE = "SELECT l.a FROM l ANTI JOIN r ON l.k = r.k"
_NOT_EXISTS = "SELECT l.a FROM l WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.k = l.k)"
_NOT_IN = "SELECT l.a FROM l WHERE l.k NOT IN (SELECT k FROM r)"
_LEFT_IS_NULL = "SELECT l.a FROM l LEFT JOIN r ON l.k = r.k WHERE r.k IS NULL"


def test_the_four_surface_forms_classify_to_one_operator() -> None:
    native = _only(_NATIVE)
    not_exists = _only(_NOT_EXISTS)
    not_in = _only(_NOT_IN)
    left_is_null = _only(_LEFT_IS_NULL)

    assert native.form is AntiJoinForm.NATIVE
    assert not_exists.form is AntiJoinForm.NOT_EXISTS
    assert not_in.form is AntiJoinForm.NOT_IN
    assert left_is_null.form is AntiJoinForm.LEFT_IS_NULL

    expected = ("l", frozenset({"k"}), "r", frozenset({"k"}))
    for a in (native, not_exists, not_in, left_is_null):
        assert _triple(a) == expected, a.form

    # The join arm is exposed for the forms that have one, so the uniqueness and fan-out
    # reducers can key their per-join decision on it; the predicate forms carry no join.
    assert native.join is not None
    assert left_is_null.join is not None
    assert not_exists.join is None
    assert not_in.join is None


def test_multi_column_correlation_carries_every_predicate_column() -> None:
    a = _only("SELECT l.a FROM l WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.k = l.k AND r.j = l.m)")
    assert _triple(a) == ("l", frozenset({"k", "m"}), "r", frozenset({"j", "k"}))


# --- soundness edges --------------------------------------------------------------


def test_left_is_null_on_a_non_key_column_is_not_recognised() -> None:
    """The join-key column is where the idiom is sound, and this is the foil that bounds it.

    On a join-key column (``_LEFT_IS_NULL`` above) the ``IS NULL`` keeps exactly the unmatched
    rows: a matched row has ``l.k = r.k`` so ``r.k`` is non-NULL there, and the filter excludes
    it. That holds with no nullability oracle, because the equality match itself proves the
    column non-NULL. On a column absent from the join key the argument evaporates: a matched row
    may carry a NULL there, survive the filter, and the result is not the anti-join. The
    classifier stays oracle-free and does not claim it; deciding it needs the nullability
    substrate."""
    sel = sqlglot.parse_one("SELECT l.a FROM l LEFT JOIN r ON l.k = r.k WHERE r.attr IS NULL")
    assert anti_joins_of(sel) == ()  # type: ignore[arg-type]


def test_left_join_without_is_null_is_not_an_anti_join() -> None:
    """A plain LEFT JOIN keeps matched and unmatched rows alike, so it is no anti-join."""
    sel = sqlglot.parse_one("SELECT l.a FROM l LEFT JOIN r ON l.k = r.k")
    assert anti_joins_of(sel) == ()  # type: ignore[arg-type]


def test_semi_join_is_not_an_anti_join() -> None:
    """SEMI keeps the rows that DO match: the dual of an anti-join, not an anti-join."""
    sel = sqlglot.parse_one("SELECT l.a FROM l SEMI JOIN r ON l.k = r.k")
    assert anti_joins_of(sel) == ()  # type: ignore[arg-type]


def test_native_anti_with_non_equality_predicate_decodes_no_clean_columns() -> None:
    """A native anti-join with a non-equality ON is still an anti-join (it filters), but its
    predicate columns do not decode to a clean key. The form is recognised; the columns are
    empty so a column-level consumer knows not to trust a key here."""
    a = _only("SELECT l.a FROM l ANTI JOIN r ON l.k > r.k")
    assert a.form is AntiJoinForm.NATIVE
    assert a.probe_cols == frozenset()
    assert a.matched_cols == frozenset()


def test_in_without_not_is_not_an_anti_join() -> None:
    """A bare ``IN`` is a semi-join; only ``NOT IN`` is the anti-join."""
    sel = sqlglot.parse_one("SELECT l.a FROM l WHERE l.k IN (SELECT k FROM r)")
    assert anti_joins_of(sel) == ()  # type: ignore[arg-type]


def test_not_in_under_or_is_not_a_scope_conjunct() -> None:
    """``anti_joins_of`` reads only a SELECT's top-level WHERE conjuncts, so a NOT IN buried in
    an OR is not returned: under OR the other disjunct still admits rows, so it is not the
    "rows of L absent from R" the top-level anti-join denotes."""
    sel = sqlglot.parse_one("SELECT l.a FROM l WHERE l.j > 0 OR l.k NOT IN (SELECT k FROM r)")
    assert anti_joins_of(sel) == ()  # type: ignore[arg-type]


def test_not_in_with_non_column_left_side_decodes_the_matched_side_without_a_probe() -> None:
    """A NOT IN whose left side is an expression is still the anti-join operator (rows of L whose
    value is absent from R); the probe columns just do not decode, exactly as a native anti-join
    with a non-equality predicate. The matched side still decodes, which is what the NOT IN
    empty-result hazard reads."""
    a = _only("SELECT l.a FROM l WHERE coalesce(l.k, 0) NOT IN (SELECT k FROM r)")
    assert a.form is AntiJoinForm.NOT_IN
    assert a.probe_cols == frozenset()
    assert a.matched_name == "r"
    assert a.matched_cols == frozenset({"k"})
