"""Recognizing a hash id that determines the columns it hashes.

``h = HASH(enc(x1..xn))`` supports ``h -> {x1..xn}`` under one named assumption, the
one practitioners make whenever a hash serves as an id: **hashed ids are injective**
(no two distinct input tuples collide). Any deterministic hash family and any encoding of
the inputs qualify, so ``md5`` over ``dbt_utils.generate_surrogate_key``'s string, a
64-bit ``FARM_FINGERPRINT``, or an escaped stable-id join are treated alike. The
opposite direction, ``{x1..xn} -> h``, is plain determinism and needs no assumption.

The recognizer therefore keeps only the boundaries that cause wrong answers:

* A hash that is reduced before it becomes the column is a bucket, not an id: two
  distinct inputs share ``hash(x) % 4`` by pigeonhole. The wrappers around the digest
  are a closed table: a case change, a hex rendering, a parenthesis, or a plain text
  cast keep the full digest and are accepted; everything else (modulo, ``SUBSTR`` /
  ``LEFT`` / ``RIGHT``, bit masks and shifts, division, ``ABS``, a narrower or sized
  cast, an unfamiliar function) is treated as reducing and declined.
* The pre-image must be exactly columns and constants joined by value-preserving
  structure (concat, casts, coalesce, replace, null-tagging case). A function of a column
  (``lower(x)``) pins only its own value, a condition (``case when x > 5``, ``x is null``)
  pins only its truth value, and anything non-deterministic or non-scalar
  (``random()``, ``uuid()``, ``current_timestamp``, a subquery, an aggregate, a window)
  is outside the structure table, so the hash identifies no column.
"""

from __future__ import annotations

from typing import cast

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg
from dblect.sql.vocab import SURROGATE_HASH_FUNCTIONS

__all__ = ["injective_hash_inputs"]

# Hash families sqlglot leaves as plain function calls (SURROGATE_HASH_FUNCTIONS covers the
# typed ones), matched by lowercase name.
_HASH_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "hash",
        "hash64",
        "xxhash32",
        "xxhash64",
        "xxh3_64",
        "murmur_hash3_32",
        "murmurhash3",
        "murmur3_hash",
        "crc32",
        "crc32c",
        "hashtext",
        "hashtextextended",
        "cityhash64",
        "siphash64",
        "fnv_hash",
        "farm_fingerprint",
        "fingerprint",
    }
)

# Wrappers that keep the full digest: a case change, a hex rendering, parentheses.
_PRESERVING_WRAPPERS: tuple[type[Expr], ...] = (exp.Lower, exp.Upper, exp.Hex, exp.Paren)

# Structure that carries its columns' values through without collapsing any of them.
_VALUE_PRESERVING_STRUCTURE: tuple[type[Expr], ...] = (
    exp.Paren,
    exp.Cast,
    exp.Coalesce,
    exp.Concat,
    exp.ConcatWs,
    exp.DPipe,
    exp.Replace,
)

_MAX_WRAPPERS = 6


def _children(node: Expr) -> list[Expr]:
    """The ``Expr`` children of ``node``, flattening list-valued args and dropping flags and
    a cast's target type."""
    out: list[Expr] = []
    for value in node.args.values():
        if isinstance(value, exp.DataType):
            continue
        if isinstance(value, Expr):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in cast("list[object]", value) if isinstance(v, Expr))
    return out


def _is_full_text_cast(node: Expr) -> bool:
    """A cast of the digest to an unsized text type renders it whole; a sized type
    (``VARCHAR(8)``) truncates and any other type reduces."""
    target = node.args.get("to")
    return (
        type(node) is exp.Cast
        and isinstance(target, exp.DataType)
        and target.is_type(*exp.DataType.TEXT_TYPES)
        and not target.expressions
    )


def _is_hash_call(node: Expr) -> bool:
    if isinstance(node, SURROGATE_HASH_FUNCTIONS):
        return True
    return isinstance(node, exp.Anonymous) and node.name.lower() in _HASH_FUNCTION_NAMES


def _hash_arguments(node: Expr) -> list[Expr]:
    """What the hash reads: its value argument(s), never a trailing digest-length literal."""
    args: list[Expr] = []
    first = node.args.get("this")
    if isinstance(first, Expr):
        args.append(first)
    args.extend(
        e for e in cast("list[object]", node.args.get("expressions") or []) if isinstance(e, Expr)
    )
    return args


def _digest_arguments(e: Expr) -> list[Expr] | None:
    """The arguments of the hash call under ``e``, if every wrapper around it keeps the full
    digest; ``None`` when ``e`` is not a hash or a wrapper reduces it."""
    node: Expr = e
    for _ in range(_MAX_WRAPPERS):
        if _is_hash_call(node):
            return _hash_arguments(node)
        inner = node.this if isinstance(node.this, Expr) else None
        if inner is None or not (
            isinstance(node, _PRESERVING_WRAPPERS) or _is_full_text_cast(node)
        ):
            return None
        node = inner
    return None


def _null_tested(cond: object) -> exp.Expr | None:
    """What ``cond`` tests for NULL (``x IS NULL``, ``NOT x IS NULL``), or ``None``."""
    if isinstance(cond, exp.Not):
        cond = cond.this
    if isinstance(cond, exp.Is) and isinstance(cond.expression, exp.Null):
        return cond.this if isinstance(cond.this, Expr) else None
    return None


def _key(c: exp.Column) -> tuple[str, str]:
    return ((sg.column_table(c) or "").lower(), sg.column_name(c).lower())


def _null_tag_columns(e: exp.Case | exp.If) -> list[exp.Column] | None:
    """The columns of a null-tagging ``CASE``/``IF``: every condition is a null test of a
    constant or of a column the branch values also carry, so the tag and the value together
    keep it. Any other condition buckets the column it reads."""
    if isinstance(e, exp.Case):
        if e.this is not None:
            return None  # simple CASE compares its operand to each WHEN value
        arms = cast("list[exp.If]", e.args.get("ifs") or [])
        conditions = [arm.this for arm in arms]
        values = [arm.args.get("true") for arm in arms] + [e.args.get("default")]
    else:
        conditions = [e.this]
        values = [e.args.get("true"), e.args.get("false")]
    out: list[exp.Column] = []
    for value in values:
        if value is None:
            continue  # a missing ELSE is NULL
        cols = _columns(value)
        if cols is None:
            return None
        out.extend(cols)
    carried = {_key(c) for c in out}
    for cond in conditions:
        tested = _null_tested(cond)
        if isinstance(tested, exp.Literal | exp.Null):
            continue
        if not isinstance(tested, exp.Column) or _key(tested) not in carried:
            return None
    return out


def _columns(e: Expr) -> list[exp.Column] | None:
    """The columns of ``e`` when it is built only from columns and constants through
    value-preserving structure or null tagging; ``None`` when anything else appears."""
    if isinstance(e, exp.Column):
        return None if isinstance(e.this, exp.Star) else [e]
    if isinstance(e, exp.Literal | exp.Boolean | exp.Null):
        return []
    if isinstance(e, exp.Case | exp.If):
        return _null_tag_columns(e)
    if not isinstance(e, _VALUE_PRESERVING_STRUCTURE):
        return None
    out: list[exp.Column] = []
    for child in _children(e):
        sub = _columns(child)
        if sub is None:
            return None
        out.extend(sub)
    return out


def injective_hash_inputs(e: Expr) -> tuple[exp.Column, ...] | None:
    """The columns ``e`` determines when it is a full-width hash of columns and constants,
    in order of first appearance; ``None`` when ``e`` is not one, is reduced, or hashes no
    column (a hash of constants determines nothing)."""
    args = _digest_arguments(e)
    if args is None:
        return None
    found: dict[tuple[str, str], exp.Column] = {}
    for arg in args:
        cols = _columns(arg)
        if cols is None:
            return None
        for c in cols:
            found.setdefault(_key(c), c)
    return tuple(found.values()) or None
