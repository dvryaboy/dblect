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
    left, right = conflicts[0]
    assert (left.name.lower(), right.name.lower()) == ("amt", "amt")
