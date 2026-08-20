"""The declared-grain not-established emitter.

A declared grain is trusted downstream (assume-guarantee) and judged once, at the
declaring model: do the upstream contracts plus this model's SQL re-derive
uniqueness on the declared columns? The verdict vocabulary is
``docs/design/refutation-and-verdicts.md``. Uniqueness reconciles declared and
inferred keys by meet, so the flow value always contains the declaration and can
never answer that question; the emitter reads the pre-reconcile record instead,
the inferred keys as the SQL derived them.

The finding requires a witnessed defeater. The key walk is conservative and drops
keys at shapes it does not model, so a declared key's absence from the inferred set
is routinely the walk's own silence; firing on absence would flag every model with
an unmodeled construct. The witness is a strictly finer key surviving to the output
with no collapse to the declared grain, and coverage runs through the functional
dependency closure so a non-minimal grain does not false-fire (the same hardening
``detect_join_fanout`` uses). The severity is a hazard, never an error: every
extensional claim holds vacuously on the empty relation, so the honest wording is
"declared but not established", not "violated".
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
)
from dblect.lineage.graph import SourceRef
from dblect.lineage.properties.functional_dependency import FDSet, NO_FDS, determines
from dblect.lineage.properties.uniqueness import CandidateKeySet, Key
from dblect.manifest import Manifest


def grain_established(declared: Key, inferred: frozenset[Key], fds: FDSet) -> bool:
    """Whether the construction re-derives uniqueness on ``declared``: some inferred
    key lies within the declared columns' closure under ``fds``. A relation unique
    on ``K`` is unique on any superset, and closure extends that through the
    dependencies (unique on ``(order_id, region)`` plus ``order_id -> region`` is
    unique on ``(order_id)``). With no dependencies this reduces to ``K`` within the
    declared columns."""
    return any(all(determines(fds, declared, col) for col in key) for key in inferred)


def grain_witness(declared: Key, inferred: frozenset[Key]) -> Key | None:
    """The witnessed defeater: a strictly finer surviving key (a strict superset of
    the declared columns), or ``None`` when nothing witnesses. A key that does not
    contain the declared grain says nothing about rows per declared tuple (every
    order may have exactly one line), so it never witnesses. The smallest finer key
    is returned so the finding is deterministic."""
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
    """One finding per declared key the construction fails to establish, with the
    defeater witnessed.

    ``key_facts`` is the same bucket the uniqueness property grounds from, so the
    emitter and the propagation can never disagree on what was declared.
    ``inferred`` is the pre-reconcile record. A scope absent from it is a leaf or an
    unbuilt model: no derivation, no entailment to judge (coverage reports the
    unbuilt case). A provisional inferred value rests on an upstream contradiction
    the propagator recovered from, so it is not treated as a witness.
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
            # Every provenance of the closed union is decided here. A Declared key is
            # a claim about this SELECT's output, so it is judged. A NativeConstraint
            # is discharged (or not) by the warehouse's write path, and the
            # advisory-unenforced case is the unenforced-constraint finding's (#48). A
            # CompileValue key is an incremental ``unique_key`` under a deduplicating
            # strategy: the merge collapses on write, so the SELECT is expected to
            # carry finer rows (the incremental-grain stream, #7, owns that write
            # path).
            match fact.provenance:
                case Declared():
                    pass
                case NativeConstraint() | CompileValue():
                    continue
                case other:
                    assert_never(other)
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
