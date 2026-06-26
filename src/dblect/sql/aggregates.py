"""Aggregate classification on two orthogonal axes, in one registry.

Both axes are facts about the same fold, keyed on the sqlglot expression *type* (the key
the propagator's aggregate dispatch already uses, dialect-neutral: bigquery
``min_by``/``max_by`` arrive as ``ArgMin``/``ArgMax``, duckdb ``median``/``quantile`` have
dedicated nodes). They are recorded together rather than in two parallel tables.

**Magnitude (result domain).** What domain the result lives in, which decides the
currency-coherence obligation (``docs/design/domain-type-algebra.md``):

* **COMBINE** synthesizes a new value out of many (``sum``, ``avg``, a spread, a middle).
  A per-row companion that varies within the group corrupts the result, so a combining
  reduction carries the coherence obligation.
* **SELECT** returns one of the input values (``min``, ``max``, ``arg_min``). The value is
  real, so the operation does not fail; only its tag is uncertain, because the comparison
  that chose it was tag-blind. The result widens to top.
* **COUNT** ignores the magnitude and yields a tag-free cardinality.
* ``None`` is "no magnitude obligation": the boolean, bitwise, and collection folds live
  on non-magnitude domains, so the lenient default reads them as having no obligation
  rather than guessing.

**Multiplicity (duplicate-sensitivity).** Whether a fan-out that duplicates input rows
distorts the result (``docs/design/hazard-algebra.md``): ``sum`` doubles, ``max`` is
unchanged. Each aggregate is safe under duplication for one of two reasons, and the field
records the outcome rather than the reason. ``min``, ``max``, the boolean folds, and
bitwise-and/or are safe because their combine is idempotent (``x ⊕ x = x``); ``sum``,
``avg``, ``count``, ``array_agg``, and ``bit_xor`` are sensitive because they count
multiplicity. The two axes agree on the common arithmetic aggregates and come apart where
it matters: ``bit_xor`` synthesizes a value yet is sensitive, the boolean folds are
non-magnitude yet safe, and ``count`` leaves the domain but a ``DISTINCT`` on its input
makes a given call safe (see :func:`strips_duplicates`).

The magnitude axis is an explicit allowlist (unclassified reads as "no obligation"); the
multiplicity axis defaults the other way, to *sensitive*, so an unclassified aggregate keeps
a fan-out finding firing rather than silently clearing it. Dialect aggregates sqlglot leaves
as ``exp.Anonymous`` (duckdb ``product``, ``geometric_mean``, ``favg``, ``mad``, ...) carry
no type to key on; an adapter names the duplicate-safe ones
(``AdapterProfile.duplicate_safe_aggregate_builtins``), the same name-keyed extension
``non_deterministic_builtins`` uses. Classifying their magnitude by name is the separate gap
tracked in #119.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto

import sqlglot.expressions as exp

__all__ = [
    "AGGREGATE_BEHAVIORS",
    "AggregateBehavior",
    "aggregate_behavior",
    "duplicate_sensitive",
    "strips_duplicates",
]


class AggregateBehavior(Enum):
    """How an aggregate treats the values it folds on the magnitude axis: see the docstring."""

    COMBINE = auto()
    SELECT = auto()
    COUNT = auto()


@dataclass(frozen=True, slots=True)
class AggregateProfile:
    """Both axes for one aggregate type. ``behavior`` is ``None`` when the fold lives on a
    non-magnitude domain (boolean, bitwise, collection); ``duplicate_sensitive`` is always
    definite, since duplication is meaningful for every fold."""

    behavior: AggregateBehavior | None
    duplicate_sensitive: bool


# One entry per aggregate type, both axes together. ``duplicate_sensitive=True`` means a
# fan-out that duplicates rows distorts the result (``sum``, ``count``, ``bit_xor``,
# ``array_agg``); ``False`` means it does not (``max``, ``bool_or``, ``bit_and``).
_REGISTRY: Mapping[type[exp.AggFunc], AggregateProfile] = {
    # COMBINE: synthesize a new value out of many. A duplicated row is folded twice, so the
    # result moves: sensitive.
    exp.Sum: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Avg: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Stddev: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.StddevPop: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.StddevSamp: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Variance: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.VariancePop: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Kurtosis: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Skewness: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Median: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Mode: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.Quantile: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.ApproxQuantile: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.PercentileCont: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    exp.PercentileDisc: AggregateProfile(AggregateBehavior.COMBINE, duplicate_sensitive=True),
    # SELECT: return one of the input values. A duplicated row does not change which value is
    # extremal or selected (the combine is idempotent), so not sensitive.
    exp.Min: AggregateProfile(AggregateBehavior.SELECT, duplicate_sensitive=False),
    exp.Max: AggregateProfile(AggregateBehavior.SELECT, duplicate_sensitive=False),
    exp.ArgMin: AggregateProfile(AggregateBehavior.SELECT, duplicate_sensitive=False),
    exp.ArgMax: AggregateProfile(AggregateBehavior.SELECT, duplicate_sensitive=False),
    exp.AnyValue: AggregateProfile(AggregateBehavior.SELECT, duplicate_sensitive=False),
    exp.First: AggregateProfile(AggregateBehavior.SELECT, duplicate_sensitive=False),
    exp.Last: AggregateProfile(AggregateBehavior.SELECT, duplicate_sensitive=False),
    # COUNT: yield a cardinality. Plain count moves with multiplicity (sensitive); an
    # approximate *distinct* count deduplicates by nature, so it is safe.
    exp.Count: AggregateProfile(AggregateBehavior.COUNT, duplicate_sensitive=True),
    exp.CountIf: AggregateProfile(AggregateBehavior.COUNT, duplicate_sensitive=True),
    exp.ApproxDistinct: AggregateProfile(AggregateBehavior.COUNT, duplicate_sensitive=False),
    # Non-magnitude folds (no coherence obligation), classified on the multiplicity axis.
    # Boolean and bitwise-and/or have an idempotent combine (``x AND x = x``, ``x & x = x``),
    # so they are safe; bitwise xor cancels a duplicated row (``x ^ x = 0``) and collection
    # folds gather every row into a container, so both are sensitive.
    exp.LogicalAnd: AggregateProfile(None, duplicate_sensitive=False),
    exp.LogicalOr: AggregateProfile(None, duplicate_sensitive=False),
    exp.BitwiseAndAgg: AggregateProfile(None, duplicate_sensitive=False),
    exp.BitwiseOrAgg: AggregateProfile(None, duplicate_sensitive=False),
    exp.BitwiseXorAgg: AggregateProfile(None, duplicate_sensitive=True),
    exp.ArrayAgg: AggregateProfile(None, duplicate_sensitive=True),
    exp.GroupConcat: AggregateProfile(None, duplicate_sensitive=True),
}


# The magnitude-axis view, derived so the two axes stay one source of truth. Holds only the
# types that carry a magnitude obligation, exactly as the standalone table did.
AGGREGATE_BEHAVIORS: Mapping[type[exp.AggFunc], AggregateBehavior] = {
    agg_type: profile.behavior
    for agg_type, profile in _REGISTRY.items()
    if profile.behavior is not None
}


def _profile(agg: exp.Func) -> AggregateProfile | None:
    """The registry entry for ``agg``, walking the MRO so a rule on a base aggregate catches
    its subclasses (matching the propagator's aggregate dispatch). An ``exp.Anonymous`` UDF
    has no registry type, so it returns ``None``."""
    for cls in type(agg).__mro__:
        profile = _REGISTRY.get(cls)
        if profile is not None:
            return profile
    return None


def aggregate_behavior(agg: exp.AggFunc) -> AggregateBehavior | None:
    """The magnitude behavior class of ``agg``, or ``None`` if it carries no obligation."""
    profile = _profile(agg)
    return profile.behavior if profile is not None else None


def strips_duplicates(agg: exp.Func) -> bool:
    """True if ``agg`` deduplicates its input before folding (``sum(distinct x)``,
    ``count(distinct x)``). sqlglot wraps the argument in ``exp.Distinct`` at the
    aggregate's ``this``, the same node whether the fold is a count or a sum, so this one
    node-level read serves every bucket."""
    return isinstance(agg.this, exp.Distinct)


def duplicate_sensitive(agg: exp.Func, *, safe_builtins: frozenset[str] = frozenset()) -> bool:
    """True if a fan-out that duplicates ``agg``'s input rows would distort its result.

    The duplicate-sensitivity predicate of the hazard algebra. A ``DISTINCT`` on the input
    removes the duplicates before the fold, so any aggregate becomes safe
    (``count(distinct x)``). Otherwise the answer is the aggregate type's registry fact;
    a ``max`` or ``bool_or`` is safe, a ``sum`` or ``array_agg`` is sensitive. An
    ``exp.Anonymous`` UDF has no registry type, so it is sensitive unless the adapter names
    it in ``safe_builtins`` (case-insensitive): the firewall posture keeps an unknown fold
    firing rather than clearing a fan-out on a guess."""
    if strips_duplicates(agg):
        return False
    profile = _profile(agg)
    if profile is not None:
        return profile.duplicate_sensitive
    is_declared_safe = (
        isinstance(agg, exp.Anonymous)
        and isinstance(agg.this, str)
        and agg.this.lower() in safe_builtins
    )
    return not is_declared_safe
