"""SQL-grammar vocabulary shared across the analysis layers.

These are dialect-independent ``exp.*`` facts (the parser picks the concrete
class per dialect, but the class itself is not dialect-specific), so they live in
the ``sql`` layer rather than inside whichever property consumes them. The
uniqueness property reads the surrogate-hash grammar to recognise a hash of a
structural column combination as a key.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg

# The clock-point cast targets a timestamp bound wears: the ``TIMESTAMP '...'`` literal and the
# ``'...'::timestamp`` cast both parse to a string literal under one of these.
_TIMESTAMP_TYPES = (
    exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPTZ,
    exp.DataType.Type.TIMESTAMPLTZ,
    exp.DataType.Type.TIMESTAMPNTZ,
    exp.DataType.Type.DATETIME,
)


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


def generator_provably_nonempty(expr: Expr) -> bool:
    """True when ``expr`` is a series or date-spine generator whose literal bounds make the
    produced range non-empty, so an ``UNNEST`` of it drops no parent row.

    Covers ``GENERATE_SERIES``/``GENERATE_ARRAY`` over numeric literals and the calendar and clock
    spines over literal dates and timestamps in either spelling: ``GENERATE_DATE_ARRAY`` /
    ``GENERATE_TIMESTAMP_ARRAY`` and the Postgres/Redshift ``generate_series`` over temporal-cast
    bounds with an interval step. All map into one comparable domain (dates via their calendar
    ordinal, timestamps parsed to real instants), so a single test serves them. Decidable only
    from the call, so a non-literal bound (a ``CAST(n AS INT64)`` count that can be ``0``, a column
    start/end that can invert) leaves the range possibly empty and keeps the caller firing. A
    timestamp spine is proved when its bounds order soundly; only a naive/aware literal mix, whose
    two zones cannot be compared, is deferred. The generator analog of
    :func:`array_literal_nonempty`: silence only on a positive proof."""
    bounds = _generator_bounds(expr)
    if bounds is None:
        return False
    start, end, step, exclusive = bounds
    if step == 0:  # a zero step has no well-defined range
        return False
    # A positive step needs a low-to-high range, a negative step high-to-low; an exclusive end
    # (some dialects' half-open form) rules out the single-point range.
    if step > 0:
        return start < end if exclusive else start <= end
    return start > end if exclusive else start >= end


def _generator_bounds(expr: Expr) -> tuple[float, float, float, bool] | None:
    """The ``(start, end, step, exclusive-end)`` of a generator whose bounds and step are all
    literals, or ``None`` when any is not one we can read. Numeric series, date spines, and
    timestamp spines reduce to the same ordered scalar domain, chosen from the bounds rather than
    the function name: a ``generate_series`` carries either numeric bounds
    (``generate_series(0, 23)``) or temporal-cast bounds (the Postgres/Redshift calendar spine
    ``generate_series(d1, d2, interval '1 day')``), the same idiom as ``GENERATE_DATE_ARRAY`` and
    ``GENERATE_TIMESTAMP_ARRAY``. The step value carries only a sign, never a magnitude that
    affects non-emptiness (an inclusive range always holds its start)."""
    if not isinstance(
        expr, (exp.GenerateSeries, exp.GenerateDateArray, exp.GenerateTimestampArray)
    ):
        return None
    start_arg, end_arg, step_arg = (expr.args.get(k) for k in ("start", "end", "step"))
    exclusive = bool(expr.args.get("is_end_exclusive"))
    start = _numeric_literal(start_arg)
    end = _numeric_literal(end_arg)
    if start is not None and end is not None:
        step = 1.0 if step_arg is None else _numeric_literal(step_arg)
    else:
        start_t = _temporal_scalar(start_arg)
        end_t = _temporal_scalar(end_arg)
        # Comparable only when both bounds are the same temporal kind (a naive/aware timestamp
        # mix, most sharply, has no sound order); the step is an interval whose sign we read.
        if start_t is None or end_t is None or start_t[0] != end_t[0]:
            return None
        start, end = start_t[1], end_t[1]
        step = 1.0 if step_arg is None else _interval_sign(step_arg)
    if step is None:  # an unreadable step magnitude leaves the sign, and the range, unproven
        return None
    return start, end, step, exclusive


def _numeric_literal(expr: Expr | None) -> float | None:
    # A negative literal parses as exp.Neg wrapping a positive one, so look through it.
    if isinstance(expr, exp.Neg):
        inner = _numeric_literal(expr.this)
        return None if inner is None else -inner
    if isinstance(expr, exp.Literal) and not expr.args.get("is_string"):
        return float(expr.this)
    return None


def _date_ordinal(expr: Expr | None) -> float | None:
    # A date bound is a bare date string the generator coerces or a DATE-typed CAST of one.
    # Parsing to a real date makes the comparison chronological and rejects any spelling that
    # is not a calendar point, which stays unproven rather than guessed at.
    if isinstance(expr, (exp.Cast, exp.TryCast)) and expr.to.is_type(exp.DataType.Type.DATE):
        expr = expr.this
    if not (isinstance(expr, exp.Literal) and expr.args.get("is_string")):
        return None
    try:
        return float(date.fromisoformat(expr.this).toordinal())
    except ValueError:
        return None


def _temporal_scalar(expr: Expr | None) -> tuple[str, float] | None:
    """A literal date or timestamp bound as a ``(kind, comparable-scalar)`` pair, or ``None`` when
    it is not a literal calendar or clock point. The kind partitions bounds that can be soundly
    ordered against one another: a date, a naive timestamp (ordered by civil value under the
    session zone), and an offset-aware timestamp (ordered by absolute instant). Two bounds of the
    same kind compare correctly through their scalar (aware instants across differing offsets
    included, since each reduces to its epoch), so only a cross-kind pair, most sharply a
    naive/aware timestamp mix, is left unproven."""
    date_ord = _date_ordinal(expr)
    if date_ord is not None:
        return "date", date_ord
    moment = _timestamp_literal(expr)
    if moment is None:
        return None
    if moment.tzinfo is None:
        # A civil-value scalar, monotonic in the naive datetime and free of the DST folds that
        # ``datetime.timestamp()`` would introduce for a naive value.
        return "naive", (moment - datetime.min).total_seconds()
    return "aware", moment.timestamp()


def _timestamp_literal(expr: Expr | None) -> datetime | None:
    # A timestamp bound is a string literal under a TIMESTAMP/DATETIME cast (the ``TIMESTAMP '...'``
    # literal and the ``'...'::timestamp`` cast both parse this way). Parsing to a real datetime
    # orders the bounds chronologically and rejects any spelling that is not a clock point.
    if isinstance(expr, (exp.Cast, exp.TryCast)) and expr.to.is_type(*_TIMESTAMP_TYPES):
        expr = expr.this
    if not (isinstance(expr, exp.Literal) and expr.args.get("is_string")):
        return None
    try:
        return datetime.fromisoformat(expr.this)
    except ValueError:
        return None


def _interval_sign(expr: Expr | None) -> float | None:
    # Only the step's direction matters, so read the sign off the interval's magnitude and leave
    # a non-literal or compound magnitude unproven. The unit (DAY, MONTH, ...) never affects
    # non-emptiness. Two literal spellings reach here: the structured form (``INTERVAL 1 MONTH``,
    # ``interval '1 day'``), where sqlglot splits the magnitude into its own literal, and the
    # raw-string cast (``'1 day'::interval``), where the whole ``'<n> <unit>'`` string is one
    # literal.
    if isinstance(expr, (exp.Cast, exp.TryCast)) and expr.to.is_type(exp.DataType.Type.INTERVAL):
        expr = expr.this
    if isinstance(expr, exp.Interval) and isinstance(expr.this, exp.Literal):
        magnitude = expr.this.this
    elif isinstance(expr, exp.Literal) and expr.args.get("is_string"):
        magnitude = expr.this
    else:
        return None
    # A single-component magnitude is a signed number, optionally followed by one unit word
    # ('1', '-1', '1 day'). A compound interval ('1 mon -1 day') has an ambiguous net direction,
    # so it is left unproven rather than read off its leading term.
    tokens = magnitude.split()
    if len(tokens) > 2:
        return None
    try:
        return float(tokens[0])
    except (ValueError, IndexError):
        return None


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
