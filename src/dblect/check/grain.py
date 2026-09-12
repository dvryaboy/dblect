"""Check a declared grain against the SQL that is supposed to produce it.

Downstream checks trust a declared grain. This one asks whether the model's own
SQL establishes it. The comparison runs against the keys the SQL alone derives,
recorded before the declaration is merged in; the merged set always contains the
declaration, so comparing against it would let the declaration vouch for itself.

The finding fires only on positive evidence: a strictly finer key survives to the
output. A declared key that is merely absent from the derived set is not evidence,
because key derivation returns nothing for SQL it cannot model. Even when it fires,
the data may still satisfy the grain (every order might have exactly one line), so
the finding says "not established" rather than "violated" and warns rather than
errors. ``docs/design/refutation-and-verdicts.md`` has the vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import assert_never

from dblect.adapters import AdapterProfile
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
from dblect.lineage.properties.uniqueness import CandidateKeySet, Key, model_dedups_on_write
from dblect.manifest import Manifest


def grain_established(declared: Key, inferred: frozenset[Key], fds: FDSet) -> bool:
    """True if the SQL establishes the declared grain: some derived key is a subset
    of the declared columns, closed under ``fds``. Unique per order is also unique
    per (order, region)."""
    return any(all(determines(fds, declared, col) for col in key) for key in inferred)


def grain_witness(declared: Key, inferred: frozenset[Key]) -> Key | None:
    """The smallest derived key that is a strict superset of the declared grain, or
    ``None``. A disjoint key (``line_id`` against a grain of ``order_id``) says
    nothing about rows per order, so it is not a witness."""
    finer = [key for key in inferred if declared < key]
    if not finer:
        return None
    return min(finer, key=lambda key: (len(key), tuple(sorted(key))))


def declared_grain_findings(
    manifest: Manifest,
    profile: AdapterProfile,
    key_facts: Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
    inferred: Mapping[SourceRef, Annotation[CandidateKeySet]],
    fd: Mapping[SourceRef, Annotation[FDSet]],
) -> list[CheckFinding]:
    """One finding per declared grain the model's SQL does not establish.

    ``key_facts`` is every declared key, from the same source propagation grounds
    on. ``inferred`` is the pre-merge derived key set per relation. Skipped: models
    absent from ``inferred`` (no SQL to judge), provisional derivations (they rest
    on a contradiction), and models whose write path dedups on its own
    (:func:`model_dedups_on_write`), whose SELECT is expected to carry finer rows.
    """
    out: list[CheckFinding] = []
    judged: set[tuple[SourceRef, Key]] = set()
    for scope, bucket in sorted(key_facts.items(), key=lambda kv: kv[0].unique_id):
        node = manifest.nodes.get(scope.unique_id)
        if node is not None and model_dedups_on_write(node.config, profile):
            continue
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
    """Whether a key from this source claims something about the SELECT itself. A
    native constraint is enforced on write, not by the query (#48 covers the
    unenforced case); a compile-time value is config, not an assertion."""
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
