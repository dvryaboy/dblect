"""The judging policy the declared-versus-derived checks share: which declarations
are claims about the SELECT at all, and which models have a derived record worth
comparing against. The record is what the SQL derived before the declaration was
folded in, since the combined value contains the declaration and would vouch for
itself. Each check supplies its own notion of established and its own witnessed
defeater."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Protocol, TypeVar, assert_never

from dblect.lineage.facts.model import (
    Annotation,
    CompileValue,
    Declared,
    Fact,
    NativeConstraint,
    Provenance,
)
from dblect.lineage.graph import SourceRef


class _Bounded(Protocol):
    """A lattice value that knows when it is the formal bottom."""

    @property
    def is_bottom(self) -> bool: ...


V = TypeVar("V", bound=_Bounded)


def judged_provenance(provenance: Provenance) -> bool:
    """Whether this provenance is a person's claim about what the SELECT produces,
    the only kind worth judging. A native constraint is enforced by the warehouse's
    write path, not the query (#48 asks whether that enforcement is real). A compile
    value is minted by the toolchain, not asserted by a person: a deduplicating
    incremental's ``unique_key`` is enforced by the merge on write, so its SELECT is
    expected to produce finer rows, and flagging it would be wrong (#7)."""
    match provenance:
        case Declared():
            return True
        case NativeConstraint() | CompileValue():
            return False
    assert_never(provenance)


def judged_declarations(
    facts: Mapping[SourceRef, tuple[Fact[V, SourceRef], ...]],
    derived: Mapping[SourceRef, Annotation[V]],
) -> Iterator[tuple[SourceRef, Fact[V, SourceRef], V]]:
    """Each declaration worth judging, with what its model's SQL derived on its
    own, in a stable order. Passed over: a model absent from ``derived`` (no SQL of
    its own to judge; coverage surfaces build failures), a provisional value
    (evidence resting on a known upstream contradiction), the formal bottom (it
    carries every claim), and conditional facts (claims over a row filter, which
    activation owns)."""
    for scope, bucket in sorted(facts.items(), key=lambda kv: kv[0].unique_id):
        ann = derived.get(scope)
        if ann is None or ann.provisional or ann.value.is_bottom:
            continue
        for fact in bucket:
            if fact.condition is not None or not judged_provenance(fact.provenance):
                continue
            yield scope, fact, ann.value
