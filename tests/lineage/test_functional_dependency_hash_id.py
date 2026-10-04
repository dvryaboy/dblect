"""A hash id determines the columns it hashes, in the functional-dependency property.

The recognizer's contract (which hashes and encodings are injective) is pinned in
``tests/sql/test_hash_id.py``. These tests pin what the scope-closure engine does with a
recognized hash projection: mint ``id -> inputs`` for each input that is an output column,
nothing for a shape the recognizer declines, and nothing it cannot attribute to one input
row. A data oracle then checks the claimed dependencies against duckdb on adversarial values.
"""

from __future__ import annotations

from collections.abc import Sequence

import duckdb
from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.lineage.properties.functional_dependency import FDSet, determines
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source
from tests.lineage._fd_propagation import propagate_fds

_SOURCE = "source.shop.raw.visits"


def _field(x: str) -> str:
    return (
        f"(case when {x} is null then 'N' else "
        f"('V' || replace(replace(cast({x} as TEXT), '%', '%25'), '|', '%7C')) end)"
    )


def _hash(*xs: str) -> str:
    return "md5(" + " || '|' || ".join(_field(x) for x in xs) + ")"


_SURROGATE_KEY = (
    "md5(cast(coalesce(cast(a as text), '_dbt_utils_surrogate_key_null_') || '-' || "
    "coalesce(cast(b as text), '_dbt_utils_surrogate_key_null_') as text))"
)


def _model_fds(sql: str) -> FDSet:
    out = propagate_fds(
        {},
        _source(_SOURCE),
        _node("model.shop.m", sql),
    )
    return out["model.shop.m"]


def test_a_hash_id_determines_each_projected_input() -> None:
    fds = _model_fds(f"SELECT {_hash('a', 'b')} AS id, a, b, c FROM visits")
    assert determines(fds, frozenset({"id"}), "a")
    assert determines(fds, frozenset({"id"}), "b")
    assert not determines(fds, frozenset({"id"}), "c")


def test_the_inputs_do_not_determine_back_without_being_a_key() -> None:
    fds = _model_fds(f"SELECT {_hash('a', 'b')} AS id, a, b FROM visits")
    assert not determines(fds, frozenset({"a"}), "id")
    assert determines(fds, frozenset({"a", "b"}), "id")


def test_a_literal_field_is_a_constant_and_leaves_the_columns_determined() -> None:
    fds = _model_fds(f"SELECT {_hash(chr(39) + 'kind' + chr(39), 'a')} AS id, a FROM visits")
    assert determines(fds, frozenset({"id"}), "a")


def test_a_hash_input_that_is_not_projected_mints_nothing() -> None:
    fds = _model_fds(f"SELECT {_hash('a', 'b')} AS id, a FROM visits")
    assert determines(fds, frozenset({"id"}), "a")
    assert not determines(fds, frozenset({"id"}), "b")


def test_the_dependency_survives_a_downstream_rename_and_group_by() -> None:
    out = propagate_fds(
        {},
        _source(_SOURCE),
        _node("model.shop.ids", f"SELECT {_hash('a', 'b')} AS id, a, b FROM visits"),
        _node(
            "model.shop.m",
            "SELECT id AS visit_id, a AS acct, COUNT(*) AS n FROM ids GROUP BY id, a",
        ),
    )
    assert determines(out["model.shop.m"], frozenset({"visit_id"}), "acct")


def test_group_by_over_the_inputs_still_determines_the_hash_and_back() -> None:
    fds = _model_fds(
        f"SELECT {_hash('a', 'b')} AS id, a, b, COUNT(*) AS n FROM visits GROUP BY a, b"
    )
    assert determines(fds, frozenset({"id"}), "a")
    assert determines(fds, frozenset({"id"}), "b")


def test_a_generate_surrogate_key_hash_determines_its_inputs() -> None:
    """Under the hashed-ids-are-injective assumption the encoding does not matter."""
    fds = _model_fds(f"SELECT {_SURROGATE_KEY} AS id, a, b FROM visits")
    assert determines(fds, frozenset({"id"}), "a")
    assert determines(fds, frozenset({"id"}), "b")


def test_a_64_bit_hash_determines_its_input() -> None:
    fds = _model_fds("SELECT hash(a) AS id, a FROM visits")
    assert determines(fds, frozenset({"id"}), "a")


def test_a_reduced_hash_is_a_bucket_and_determines_nothing() -> None:
    fds = _model_fds("SELECT hash(a) % 4 AS bucket, a FROM visits")
    assert not determines(fds, frozenset({"bucket"}), "a")


def test_a_non_deterministic_pre_image_determines_nothing() -> None:
    fds = _model_fds("SELECT md5(a || random()) AS id, a FROM visits")
    assert not determines(fds, frozenset({"id"}), "a")


def test_an_unqualified_input_in_a_join_is_not_attributed_to_either_side() -> None:
    out = propagate_fds(
        {},
        _source(_SOURCE),
        _source("source.shop.raw.accounts"),
        _node(
            "model.shop.m",
            f"SELECT {_hash('a')} AS id, v.a AS va FROM visits v "
            "JOIN accounts x ON v.acct = x.acct",
        ),
    )
    assert not determines(out["model.shop.m"], frozenset({"id"}), "va")


def test_a_qualified_input_in_a_join_is_attributed_to_its_side() -> None:
    out = propagate_fds(
        {},
        _source(_SOURCE),
        _source("source.shop.raw.accounts"),
        _node(
            "model.shop.m",
            f"SELECT {_hash('v.a', 'x.region')} AS id, v.a AS va, x.region AS r "
            "FROM visits v JOIN accounts x ON v.acct = x.acct",
        ),
    )
    assert determines(out["model.shop.m"], frozenset({"id"}), "va")
    assert determines(out["model.shop.m"], frozenset({"id"}), "r")


def test_a_hash_over_the_nullable_side_of_a_left_join_still_determines_its_inputs() -> None:
    """The tag encodes NULL, so a padded row's NULLs are values like any other."""
    out = propagate_fds(
        {},
        _source(_SOURCE),
        _source("source.shop.raw.accounts"),
        _node(
            "model.shop.m",
            f"SELECT {_hash('x.region')} AS id, x.region AS r "
            "FROM visits v LEFT JOIN accounts x ON v.acct = x.acct",
        ),
    )
    assert determines(out["model.shop.m"], frozenset({"id"}), "r")


# --- the claimed dependency holds on adversarial data -----------------------------------

_CELL = st.one_of(st.none(), st.text(alphabet="ab|%N V275C-_", max_size=3))


@settings(max_examples=40, deadline=None)
@given(rows=st.lists(st.tuples(_CELL, _CELL), min_size=1, max_size=30))
def test_every_claimed_hash_dependency_holds_on_the_data(
    rows: Sequence[tuple[str | None, str | None]],
) -> None:
    sql = f"SELECT {_hash('a', 'b')} AS id, a, b FROM visits"
    claimed = _model_fds(sql)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE visits (a VARCHAR, b VARCHAR)")
    con.executemany("INSERT INTO visits VALUES (?, ?)", [list(r) for r in rows])
    con.execute(f"CREATE TABLE _m AS {sql}")
    for dependent in ("a", "b"):
        assert determines(claimed, frozenset({"id"}), dependent)
        violating = con.execute(
            f"SELECT count(*) FROM (SELECT id FROM _m GROUP BY id "
            f"HAVING count(DISTINCT {dependent}) > 1 "
            f"OR (count({dependent}) > 0 AND count(*) FILTER (WHERE {dependent} IS NULL) > 0))"
        ).fetchone()
        assert violating == (0,)


def test_a_hash_over_a_column_outside_the_group_by_mints_nothing() -> None:
    """A permissive engine (MySQL without ONLY_FULL_GROUP_BY) accepts a bare non-group
    column; the hash is then not a function of the group, so nothing is claimed."""
    fds = _model_fds(f"SELECT {_hash('a', 'b')} AS id, a FROM visits GROUP BY a")
    assert not determines(fds, frozenset({"id"}), "a")
