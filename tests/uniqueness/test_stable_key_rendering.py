"""Finding text renders key sets in a canonical order, whatever the set's iteration order (#312)."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.properties.scope_closure import Key, render_key, render_keys
from dblect.sql import parse_sql
from dblect.uniqueness.detector import detect_join_fanout

_KEYS = st.frozensets(st.frozensets(st.sampled_from("abcde"), min_size=1), min_size=1, max_size=5)


@given(_KEYS, st.data())
def test_render_keys_ignores_construction_order(keys: frozenset[Key], data: st.DataObject) -> None:
    shuffled = data.draw(st.permutations([data.draw(st.permutations(sorted(k))) for k in keys]))
    assert render_keys(frozenset(frozenset(m) for m in shuffled)) == render_keys(keys)


def test_render_shape_sorts_columns_then_keys() -> None:
    assert render_key(frozenset({"b", "a"})) == "a, b"
    keys = frozenset({frozenset({"d"}), frozenset({"b", "a"}), frozenset({"c"})})
    assert render_keys(keys) == "(a, b); (c); (d)"


_PROBE = textwrap.dedent(
    """
    from dblect.sql import parse_sql
    from dblect.uniqueness.detector import detect_join_fanout

    tree = parse_sql(
        "select sum(c.credit) from customers c join src s on s.customer_id = c.customer_id",
        dialect="duckdb",
    )
    keys = {
        "customers": frozenset({frozenset({"customer_id"})}),
        "src": frozenset(frozenset({c}) for c in "abcd"),
    }
    print(detect_join_fanout(tree, model_keys=keys, declared_keys=frozenset())[0].message)
    """
)


def _message_under(seed: int) -> str:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env={"PYTHONHASHSEED": str(seed), "PATH": ""},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


@pytest.mark.parametrize("seed", range(1, 6))
def test_join_fanout_message_is_identical_across_hash_seeds(seed: int) -> None:
    assert "(known: (a); (b); (c); (d))" in _message_under(seed)


def test_join_fanout_in_process_lists_known_keys_sorted() -> None:
    tree = parse_sql(
        "select sum(c.credit) from customers c join src s on s.customer_id = c.customer_id",
        dialect="duckdb",
    )
    findings = detect_join_fanout(
        tree,
        model_keys={
            "customers": frozenset({frozenset({"customer_id"})}),
            "src": frozenset(frozenset({c}) for c in "dbac"),
        },
        declared_keys=frozenset(),
    )
    assert "(known: (a); (b); (c); (d))" in findings[0].message
