"""Join-key type compatibility, the substrate signal (the C2 join concern).

Equating two columns in a join ``ON`` asserts their values mean the same thing, so
joining a ``MoneyUSD`` column against a ``MoneyEUR`` one (or an ISO-2 ``Country`` against
an ISO-3) is a contradiction. :func:`join_key_conflicts` computes that signal: the ON
equalities whose two sides' domain tags meet to ``CONFLICT``. It returns the offending
column pairs rather than a finding, because the user-facing seam diagnostic is a later
build; these tests pin the signal by reading tags through a supplied resolver.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
import sqlglot
from sqlglot import expressions as exp

from dblect.lineage.properties.domain_type import (
    Concrete,
    Dimension,
    DomainTag,
    join_key_conflicts,
    tagged,
)
from dblect.sql import _sqlglot as sg
from dblect.sql.vocab import CastTarget

_USD = tagged(dimension=Dimension.of(Concrete("usd")))
_EUR = tagged(dimension=Dimension.of(Concrete("eur")))
_ISO2 = tagged(nominal={"country": Concrete("iso2")})
_ISO3 = tagged(nominal={"country": Concrete("iso3")})


def _on(sql: str) -> sqlglot.Expr:
    sel = sqlglot.parse_one(sql, dialect="duckdb")
    assert isinstance(sel, exp.Select)
    on = sg.on_of(sg.joins_of(sel)[0])
    assert on is not None
    return on


def _resolver(by_qcol: Mapping[tuple[str, str], DomainTag]):
    def tag_of(col: exp.Column) -> DomainTag | None:
        return by_qcol.get((col.table.lower(), col.name.lower()))

    return tag_of


def test_mixed_currency_join_key_is_a_conflict() -> None:
    on = _on("SELECT 1 FROM a JOIN b ON a.amt = b.amt")
    conflicts = join_key_conflicts(on, _resolver({("a", "amt"): _USD, ("b", "amt"): _EUR}))
    assert len(conflicts) == 1
    # The conflicting tags ride along, so a renderer reuses them rather than re-resolving.
    _left, _right, left_tag, right_tag = conflicts[0]
    assert (left_tag, right_tag) == (_USD, _EUR)


def test_matching_currency_join_key_is_clean() -> None:
    on = _on("SELECT 1 FROM a JOIN b ON a.amt = b.amt")
    assert join_key_conflicts(on, _resolver({("a", "amt"): _USD, ("b", "amt"): _USD})) == ()


def test_incompatible_nominal_join_key_is_a_conflict() -> None:
    on = _on("SELECT 1 FROM a JOIN b ON a.country = b.country")
    conflicts = join_key_conflicts(
        on, _resolver({("a", "country"): _ISO2, ("b", "country"): _ISO3})
    )
    assert len(conflicts) == 1


def test_an_untagged_join_key_does_not_conflict() -> None:
    """A no-claim side is the lenient posture: nothing is asserted about it, so no finding."""
    on = _on("SELECT 1 FROM a JOIN b ON a.amt = b.amt")
    assert join_key_conflicts(on, _resolver({("a", "amt"): _USD})) == ()


def test_only_the_conflicting_conjunct_is_flagged() -> None:
    """A compound ON pins each equality on its own conjunct: the currency mismatch is
    flagged while a matching key alongside it is not."""
    on = _on("SELECT 1 FROM a JOIN b ON a.amt = b.amt AND a.k = b.k")
    tags = {("a", "amt"): _USD, ("b", "amt"): _EUR, ("a", "k"): _USD, ("b", "k"): _USD}
    conflicts = join_key_conflicts(on, _resolver(tags))
    assert len(conflicts) == 1
    left, right, _left_tag, _right_tag = conflicts[0]
    assert (left.name.lower(), right.name.lower()) == ("amt", "amt")


_WRAPPERS = ("CAST({} AS {})", "TRY_CAST({} AS {})", "({}::{})", "(CAST({} AS {}))")
_TARGET_SQL = {CastTarget.NUMERIC: "BIGINT", CastTarget.TEXT: "VARCHAR", CastTarget.OTHER: "DATE"}
_CAST_SIDES = ("left", "right", "both")


def _cast_on(wrapper: str, target: CastTarget, sides: str) -> sqlglot.Expr:
    def side(col: str, wrapped: bool) -> str:
        return wrapper.format(col, _TARGET_SQL[target]) if wrapped else col

    left = side("a.k", sides in ("left", "both"))
    right = side("b.k", sides in ("right", "both"))
    return _on(f"SELECT 1 FROM a JOIN b ON {left} = {right}")


@pytest.mark.parametrize("sides", _CAST_SIDES)
@pytest.mark.parametrize("target", list(CastTarget))
@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_identifier_conflict_through_a_cast_fires_unless_target_is_other(
    wrapper: str, target: CastTarget, sides: str
) -> None:
    """An identifier tag survives numeric and text casts, so a cast key still conflicts;
    a cast to any other type makes no claim, so it stays silent."""
    on = _cast_on(wrapper, target, sides)
    conflicts = join_key_conflicts(on, _resolver({("a", "k"): _ISO2, ("b", "k"): _ISO3}))
    assert len(conflicts) == (0 if target is CastTarget.OTHER else 1)
    for left, right, _lt, _rt in conflicts:
        assert (left.table, right.table) == ("a", "b")


@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_magnitude_cast_to_text_does_not_fire(wrapper: str) -> None:
    on = _cast_on(wrapper, CastTarget.TEXT, "both")
    assert join_key_conflicts(on, _resolver({("a", "k"): _USD, ("b", "k"): _EUR})) == ()


@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_magnitude_cast_to_numeric_still_conflicts(wrapper: str) -> None:
    on = _cast_on(wrapper, CastTarget.NUMERIC, "both")
    assert len(join_key_conflicts(on, _resolver({("a", "k"): _USD, ("b", "k"): _EUR}))) == 1


def test_key_matching_still_ignores_cast_wrapped_keys() -> None:
    """Uniqueness and orphan detectors read bare keys only: a cast need not preserve them."""
    on = _on("SELECT 1 FROM a JOIN b ON CAST(a.k AS BIGINT) = b.k")
    assert sg.equality_column_pairs(on) == ()
