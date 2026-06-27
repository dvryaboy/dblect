"""Characterisation of the sqlglot behaviour shared reference resolution rests on.

Detectors read the builder's resolution off their own parsed tree. That works only if
a node's ``.meta`` identity survives the copy-and-qualify the builder performs, so
resolved refs can be written back onto the original nodes (see ``_Walker.stamp_original``
in ``builder.py``). These tests pin exactly that, so a future sqlglot upgrade that broke
the assumption would fail here rather than silently stranding the resolution on the
builder's private copy.
"""

from __future__ import annotations

import sqlglot
import sqlglot.expressions as exp
from sqlglot import Expr
from sqlglot.optimizer.qualify import qualify

_SQL = "with c as (select id, amount from raw) select id, amount from c"
_SCHEMA: dict[str, object] = {"raw": {"id": "INT64", "amount": "FLOAT64"}}


def _tagged_original() -> Expr:
    tree = sqlglot.parse_one(_SQL, read="bigquery")
    for i, col in enumerate(tree.find_all(exp.Column)):
        col.meta["orig_id"] = i
    return tree


def test_copy_preserves_meta() -> None:
    original = _tagged_original()
    tags = {col.meta.get("orig_id") for col in original.copy().find_all(exp.Column)}
    assert tags == {0, 1, 2, 3}


def test_qualify_preserves_meta_on_surviving_columns() -> None:
    # Every column the source carried keeps its identity tag through qualification, so a
    # ref resolved on the qualified copy can be written back to the original node.
    original = _tagged_original()
    qualified = qualify(
        original.copy(),
        dialect="bigquery",
        schema=_SCHEMA,
        validate_qualify_columns=False,
        identify=False,
    )
    tagged = [c.meta.get("orig_id") for c in qualified.find_all(exp.Column)]
    assert all(t is not None for t in tagged)
    assert set(tagged) == {0, 1, 2, 3}


def test_original_tree_is_untouched_by_copy_and_qualify() -> None:
    # The detectors keep matching on the un-qualified original, so qualification of the
    # copy must not leak qualifiers back. (The first column stays bare, not ``c.id``.)
    original = _tagged_original()
    qualify(
        original.copy(),
        dialect="bigquery",
        schema=_SCHEMA,
        validate_qualify_columns=False,
        identify=False,
    )
    first = next(iter(original.find_all(exp.Column)))
    assert first.table == ""
