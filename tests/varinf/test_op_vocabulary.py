"""Drift guards for the operator vocabularies the walker classifies on.

The walker maps jinja2's arithmetic nodes and comparison op-names onto our own
``ArithOp`` / ``ComparisonOp`` enums. A jinja2 upgrade that adds an operator, or an
enum member added without wiring it in, would silently drop usages to the
``Unknown`` fallback rather than classifying them. Completeness is held regardless
(the walker property tests prove no usage is lost); these tests guard the
classification fidelity by pinning the mapping total against the enums and aligned
with jinja2's own vocabulary, so the drift surfaces here instead of in the field.
"""

# A white-box guard: inspecting the walker's internal tables is the whole point.
# pyright: reportPrivateUsage=false

from __future__ import annotations

# ``_compare_operators`` is jinja2's own compare-op set. It is a private symbol, so a
# jinja2 release that renames it breaks this import; the fix is to re-pin against the
# new name, the same drift signal these tests exist to surface.
from jinja2.parser import _compare_operators

from dblect.varinf.usage import ArithOp, ComparisonOp
from dblect.varinf.walker import _ARITH_OPS, _COMPARISON_OP_VALUES, _FLIPPED_COMPARISON


def test_every_arith_op_is_reachable_and_none_orphaned() -> None:
    # Each ArithOp is produced by exactly one jinja2 node, and no member is stranded
    # without a node that yields it.
    assert set(_ARITH_OPS.values()) == set(ArithOp)
    assert len(_ARITH_OPS) == len(ArithOp)


def test_arith_enum_values_track_jinja_node_names() -> None:
    # The walker keys on node type; the enum value mirrors the jinja2 class name
    # lowercased, which keeps the enum self-documenting against the source nodes.
    assert {node.__name__.lower() for node in _ARITH_OPS} == {op.value for op in ArithOp}


def test_comparison_vocabulary_matches_jinja() -> None:
    # Comparison covers jinja2's whole compare-operator vocabulary (in/notin are a
    # different shape, classified as InSet). If jinja2 adds a comparison operator,
    # this fails until we classify or consciously skip it.
    jinja_compare_ops = set(_compare_operators)
    assert jinja_compare_ops == _COMPARISON_OP_VALUES


def test_flip_table_is_total_and_an_involution() -> None:
    assert set(_FLIPPED_COMPARISON) == set(ComparisonOp)
    # Flipping which side the literal sits on is its own inverse: flip twice is a
    # no-op, so a literal-on-left form and its rewrite agree.
    assert all(_FLIPPED_COMPARISON[_FLIPPED_COMPARISON[op]] is op for op in ComparisonOp)
