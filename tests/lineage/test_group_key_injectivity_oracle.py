"""Data-as-judge test for the group-key verdict on value-mapping wrappers.

``key_verdict_reader`` decides from a companion's declared members whether ``upper``,
``lower``, ``trim`` or a text cast of it keeps two members in separate groups. This asks
duckdb instead of re-deriving the rule: group the members by the wrapped key and look for
a group holding two distinct members. Over printable ASCII the verdict is exact, so the
two must agree in both directions, not just on soundness.
"""

from __future__ import annotations

import duckdb
from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.lineage.facts.property import KeyVerdict
from dblect.lineage.graph import ColumnRef, GroupKey, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import CompanionFacts, key_verdict_reader
from dblect.sql.vocab import KeyShape

_COMPANION = ColumnRef(SourceRef(SourceKind.SOURCE, "source.shop.raw.payments"), "currency")
_WRAPPERS = {
    KeyShape.UPPER: "upper(currency)",
    KeyShape.LOWER: "lower(currency)",
    KeyShape.TRIM: "trim(currency)",
    KeyShape.TEXT_CAST: "cast(currency AS varchar)",
}
# Spaces, both cases and two letters: small enough that collisions are common.
# An empty set is a deliberate no-claim, pinned in the coherence tests, so it is not sampled.
_MEMBERS = st.frozensets(st.text(alphabet=" aAbB", max_size=3), min_size=1, max_size=6)


@settings(max_examples=150, deadline=None)
@given(shape=st.sampled_from(sorted(_WRAPPERS, key=lambda s: s.name)), members=_MEMBERS)
def test_wrapper_holds_the_companion_iff_the_warehouse_keeps_members_apart(
    oracle_con: duckdb.DuckDBPyConnection, shape: KeyShape, members: frozenset[str]
) -> None:
    facts = CompanionFacts(members=lambda _ref: members)
    key = GroupKey(shape, _COMPANION, _WRAPPERS[shape])
    holds = key_verdict_reader(facts)(key, _COMPANION) is KeyVerdict.HOLDS

    con = oracle_con
    con.execute("CREATE OR REPLACE TABLE members (currency VARCHAR)")
    for m in sorted(members):
        con.execute("INSERT INTO members VALUES (?)", [m])
    try:
        row = con.execute(
            f"SELECT coalesce(max(n), 0) FROM (SELECT count(DISTINCT currency) AS n "
            f"FROM members GROUP BY {_WRAPPERS[shape]})"
        ).fetchone()
    finally:
        con.execute("DROP TABLE IF EXISTS members")
    assert row is not None
    assert holds == (row[0] <= 1)
