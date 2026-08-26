"""Checking a declared grain against the SQL that is supposed to produce it.

A user who writes ``grain(per=order_id)`` is telling us the model has one row per
order. Everything downstream takes that at its word. This module asks the one
question nobody else asks: does this model's own SQL actually produce it?

Comparing against the keys we store for the model would answer yes every time,
because a declared key is merged into what we know about a model as soon as it is
read, so the declaration ends up checking itself. What we compare against instead
is the set of keys the SQL alone implies, which the propagator records separately
before the merge.

Two rules keep the answer honest.

*We report only when we can point at a specific finer key.* Failing to find the
declared key among the derived ones is weak evidence, because key derivation gives
up on SQL it cannot model and returns nothing rather than guessing. Treating "we
found nothing" as "the grain is wrong" would flag a large share of real models. So
we speak up only when the SQL demonstrably carries a *finer* key all the way to the
output, one that keeps the rows the declared grain says are collapsed.

*The SQL is allowed to be stricter than the declaration.* Unique per order is also
unique per (order, region), and a declared dependency can close a gap the columns
alone leave open, so we ask whether the declared columns cover a derived key through
the functional-dependency closure rather than looking for an exact match. This is the
same reasoning ``detect_join_fanout`` uses to decide a join key covers a key.

Even when we do report, the SQL failing to guarantee the grain is not proof that the
data breaks it: every order may happen to have exactly one line. So the finding says
the grain is not established rather than violated, and it warns rather than errors.
``docs/design/refutation-and-verdicts.md`` works through why that distinction holds
for claims about rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import assert_never

from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.lineage.facts.model import (
    Annotation,
    CompileValue,
    Declared,
    Fact,
    NativeConstraint,
    Provenance,
)
from dblect.lineage.graph import SourceRef
from dblect.lineage.properties.functional_dependency import NO_FDS, FDSet, determines
from dblect.lineage.properties.uniqueness import CandidateKeySet, Key
from dblect.manifest import Manifest


def grain_established(declared: Key, inferred: frozenset[Key], fds: FDSet) -> bool:
    """Whether the SQL already gives us the declared grain.

    It does when one of the keys it derives sits inside the declared columns, since
    a table unique on some columns is also unique on any larger set of them: unique
    per order means unique per (order, region). Declared dependencies widen what the
    declared columns reach, so unique on (order_id, region) plus ``order_id ->
    region`` counts as unique on (order_id) too. With no dependencies in play this
    is plain containment."""
    return any(all(determines(fds, declared, col) for col in key) for key in inferred)


def grain_witness(declared: Key, inferred: frozenset[Key]) -> Key | None:
    """The finer key we can point at as evidence the grain is not met, if there is one.

    A key counts as evidence only when it contains every declared column and at
    least one more: the SQL is keeping rows apart that the declared grain says are
    one row. A key that is merely different, say a key on ``line_id`` against a grain
    of ``order_id``, tells us nothing about how many rows an order gets, so it is not
    evidence. Returns the smallest such key, so the finding reads the same on every
    run."""
    finer = [key for key in inferred if declared < key]
    if not finer:
        return None
    return min(finer, key=lambda key: (len(key), tuple(sorted(key))))


def declared_grain_findings(
    manifest: Manifest,
    key_facts: Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
    inferred: Mapping[SourceRef, Annotation[CandidateKeySet]],
    fd: Mapping[SourceRef, Annotation[FDSet]],
) -> list[CheckFinding]:
    """One finding per declared grain this model's SQL does not deliver.

    ``key_facts`` holds every key a user declared, read from the same place the
    propagation reads them so the two cannot disagree about what was claimed.
    ``inferred`` holds the keys each model's SQL implies on its own, recorded before
    declared keys were merged in.

    A model missing from ``inferred`` has no SQL of its own to judge, a source or a
    model that failed to build, and is passed over; the coverage report is what
    surfaces the ones that failed to build. We also stay quiet when the keys we
    derived rest on contradictory upstream declarations, since evidence drawn from a
    known contradiction is not worth reporting.
    """
    out: list[CheckFinding] = []
    judged: set[tuple[SourceRef, Key]] = set()
    for scope, bucket in sorted(key_facts.items(), key=lambda kv: kv[0].unique_id):
        inferred_ann = inferred.get(scope)
        if inferred_ann is None or inferred_ann.provisional:
            continue
        derived = inferred_ann.value
        if derived.is_bottom:
            continue  # the formal universal element already carries every key
        fd_ann = fd.get(scope)
        fds = fd_ann.value if fd_ann is not None else NO_FDS
        for fact in bucket:
            if not _judged_provenance(fact.provenance):
                continue
            if fact.condition is not None:
                continue  # a conditional key holds only over a row filter; activation owns it
            for authored in fact.value.keys:
                declared = frozenset(col.lower() for col in authored)
                if (scope, declared) in judged:
                    continue
                judged.add((scope, declared))
                if grain_established(declared, derived.keys, fds):
                    continue
                witness = grain_witness(declared, derived.keys)
                if witness is None:
                    continue
                out.append(_finding(manifest, scope, fact, authored, witness))
    return out


def _judged_provenance(provenance: Provenance) -> bool:
    """Whether a key from this source is a claim about what the SELECT itself
    produces, deciding every kind we can receive.

    A key someone wrote down, in a contract or a dbt test, is such a claim, so we
    check it. A native warehouse constraint is enforced when the table is written
    rather than by the query, and whether the warehouse really enforces it is #48's
    question. An incremental model's ``unique_key`` under a merge strategy is the
    same story: the merge collapses duplicates on write, so its SELECT is expected to
    produce finer rows and flagging that would be wrong (#7 covers the write path)."""
    match provenance:
        case Declared():
            return True
        case NativeConstraint() | CompileValue():
            return False
    assert_never(provenance)


def _finding(
    manifest: Manifest,
    scope: SourceRef,
    fact: Fact[CandidateKeySet, SourceRef],
    declared: Key,
    witness: Key,
) -> CheckFinding:
    declared_cols = ", ".join(sorted(declared))
    witness_cols = ", ".join(sorted(witness))
    attribution = f" (declared by {fact.detail})" if fact.detail else ""
    node = manifest.nodes.get(scope.unique_id)
    single = next(iter(declared)) if len(declared) == 1 else None
    return CheckFinding(
        kind=CheckFindingKind.GRAIN_NOT_ESTABLISHED,
        message=(
            f"declared grain ({declared_cols}){attribution} is not established: the "
            f"construction carries the strictly finer key ({witness_cols}) to the "
            "output with no collapse to the declared grain. Aggregate to the "
            "declared grain, or correct the declaration to the grain the SQL "
            "produces."
        ),
        model_unique_id=scope.unique_id,
        file_path=node.original_file_path if node is not None else None,
        column=single,
    )
