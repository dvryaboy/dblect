"""SQL-grammar vocabulary shared across the analysis layers.

These are dialect-independent ``exp.*`` facts (the parser picks the concrete
class per dialect, but the class itself is not dialect-specific), so they live in
the ``sql`` layer rather than inside whichever property consumes them. The
uniqueness property reads the surrogate-hash grammar to recognise a hash of a
structural column combination as a key.
"""

from __future__ import annotations

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg


def array_literal_nonempty(expr: Expr) -> bool:
    """True when ``expr`` is an array constructor with one or more elements that are each
    guaranteed present, so the array cannot be empty.

    A bracket array literal (``[e1, ..., en]`` / ``ARRAY[e1, ..., en]``) lists each element
    explicitly, so a literal, struct, or other scalar element always contributes one item. A
    parenthesised scalar subquery (``[(SELECT AS STRUCT ...), ...]``, the wide-to-long pivot
    idiom) also contributes exactly one item, because a ``SELECT`` with no ``FROM`` returns
    exactly one row.

    The form that can still be empty is a *set-returning* subquery element: the
    ``ARRAY(<query>)`` array-subquery function, or a parenthesised subquery that reads a
    ``FROM`` (``ARRAY((SELECT AS STRUCT ... FROM unnest(...) WHERE ...))``), each of which may
    return zero rows. A query element is therefore treated as guaranteed only when it has no
    ``FROM``; any element that can be absent disqualifies the array. Used by both the
    ``array_nonemptiness`` property and the inner-flatten detector so the two read the
    literal-array idiom the same way."""
    if not isinstance(expr, exp.Array) or not expr.expressions:
        return False
    return all(_array_element_present(e) for e in expr.expressions)


def _array_element_present(element: Expr) -> bool:
    """Whether one array-constructor element is guaranteed to contribute an item.

    A non-query scalar (literal, struct, column, expression) always does. A query element
    (a bare ``SELECT``/``UNION`` or one wrapped in ``exp.Subquery``) does only when it has no
    ``FROM``: a ``FROM``-bearing subquery is set-returning and may yield zero rows, so the
    array carries no non-emptiness guarantee."""
    inner = element.this if isinstance(element, exp.Subquery) else element
    if isinstance(inner, exp.Query):
        return isinstance(inner, exp.Select) and sg.from_of(inner) is None
    return True


# --- surrogate-hash grammar --------------------------------------------------
#
# The typed-node vocabulary for recognizing a surrogate-hash key: a hash of a
# structural combination of columns. An adapter that hashes via a function
# sqlglot parses to `exp.Anonymous` would compose a name set on top, as the
# non-determinism builtins do; nothing demands that yet.
#
# These are tuples, not frozensets, because membership is tested with
# `isinstance`, whose subclass-awareness is load-bearing: `TO_HEX(...)` parses to
# `exp.LowerHex`, a subclass of `exp.Hex`, so listing `Hex` looks through the hex
# wrapper. A hash's hex and raw-digest spellings, though, are siblings, not in a
# subclass relation (`MD5`/`MD5Digest`, `SHA2`/`SHA2Digest`), so both are listed
# explicitly. Resolved by name for tolerance across sqlglot versions.
SURROGATE_HASH_FUNCTIONS: tuple[type[Expr], ...] = tuple(
    getattr(exp, n)
    for n in ("MD5", "MD5Digest", "SHA", "SHA1Digest", "SHA2", "SHA2Digest", "FarmFingerprint")
    if hasattr(exp, n)
)
# Single-argument wrappers that do not change which tuple is hashed, looked through
# to reach the hash (e.g. `TO_HEX(MD5(...))`, `LOWER(...)`).
SURROGATE_HASH_PASSTHROUGH: tuple[type[Expr], ...] = tuple(
    getattr(exp, n) for n in ("Hex", "Lower", "Upper") if hasattr(exp, n)
)
# Structural combinators that assemble columns into the hashed value without making
# the input anything other than those columns.
SURROGATE_HASH_STRUCTURAL: tuple[type[Expr], ...] = tuple(
    getattr(exp, n)
    for n in ("Concat", "DPipe", "Cast", "TryCast", "Coalesce", "Lower", "Upper", "Trim", "Paren")
    if hasattr(exp, n)
)
