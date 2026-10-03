"""Finding text renders key sets in a canonical order, whatever the set's iteration order (#312)."""

from __future__ import annotations

import subprocess
import sys
import textwrap

from dblect.lineage.properties.scope_closure import render_key, render_keys


def test_render_sorts_columns_then_keys() -> None:
    assert render_key(frozenset({"b", "a"})) == "a, b"
    keys = frozenset({frozenset({"d"}), frozenset({"b", "a"}), frozenset({"c"})})
    assert render_keys(keys) == "(a, b); (c); (d)"


# Set iteration order only varies between processes (string hashing is seeded per
# process), so the contract is checked across hash seeds.
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


def test_join_fanout_message_is_identical_across_hash_seeds() -> None:
    messages = {
        subprocess.run(
            [sys.executable, "-c", _PROBE],
            env={"PYTHONHASHSEED": str(seed), "PATH": ""},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(1, 6)
    }
    assert len(messages) == 1
    assert "(known: (a); (b); (c); (d))" in messages.pop()
